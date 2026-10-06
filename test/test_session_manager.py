"""Behavioral tests for kodo.server.SessionManager — server-authoritative
single-window ownership, the disconnect grace window, and session listing.

No LLM calls are made; engines start their worker (idle on an empty queue) and
are torn down via ``manager.shutdown()``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

import kodo.agents
from kodo.agents import AgentRegistry
from kodo.llms import LLMGateway
from kodo.project import WorkspaceLayout
from kodo.server import SessionManager
from kodo.server._session_manager import Session
from kodo.transport import Connection

_AGENTS_DIR = Path(kodo.agents.__file__).parent
_SETTINGS: dict[str, object] = {"mode": "local", "models": {"local": "llamacpp-qwen36-27b"}}


class _FakeWS:
    """Minimal stand-in for an aiohttp WebSocketResponse."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


def _conn() -> Connection:
    return Connection(_FakeWS())  # type: ignore[arg-type]


@pytest.fixture
async def manager_factory(
    tmp_path: Path,
) -> AsyncGenerator[object, None]:
    created: list[SessionManager] = []

    def make(grace: float = 100.0) -> SessionManager:
        layout = WorkspaceLayout(tmp_path / "home")
        layout.init()
        mgr = SessionManager(
            registry=AgentRegistry(_AGENTS_DIR),
            gateway=LLMGateway(cloud_concurrency=lambda: 2),
            get_settings=lambda: dict(_SETTINGS),
            layout=layout,
            grace_seconds=grace,
        )
        created.append(mgr)
        return mgr

    yield make
    for mgr in created:
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_window_rejected_while_live_owner(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)

    # A different window cannot open the live session.
    assert await mgr.open(session.id, "windowB") is None

    # Explicit release frees it immediately.
    mgr.release(session.id)
    reopened = await mgr.open(session.id, "windowB")
    assert reopened is not None and reopened.id == session.id


@pytest.mark.asyncio
async def test_grace_blocks_others_then_frees(manager_factory) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    mgr: SessionManager = manager_factory(grace=0.05)
    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)
    mgr.drop_connection(conn)

    # During the grace window the session is still reserved for window A.
    assert await mgr.open(session.id, "windowB") is None

    # Poll for grace expiry rather than sleeping a fixed margin past it: a
    # loaded CI runner's event-loop scheduling jitter can easily exceed a
    # tight fixed margin (e.g. 0.12s past a 0.05s grace), which flaked this
    # test without any actual bug in SessionManager.
    reopened = None
    for _ in range(100):
        reopened = await mgr.open(session.id, "windowB")
        if reopened is not None:
            break
        await asyncio.sleep(0.05)
    assert reopened is not None and reopened.id == session.id


@pytest.mark.asyncio
async def test_same_window_reclaims_within_grace(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory(grace=100.0)
    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)
    mgr.drop_connection(conn)

    # The same window reloads within grace and reclaims its session.
    reclaimed = await mgr.open(session.id, "windowA")
    assert reclaimed is not None and reclaimed.id == session.id


@pytest.mark.asyncio
async def test_open_unknown_id_creates_fresh(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    fresh = await mgr.open("does-not-exist", "windowA")
    assert fresh is not None and fresh.id != "does-not-exist"


# ---------------------------------------------------------------------------
# A disconnect (unlike genuine teardown) never loses a pending server-
# initiated request — the doc/SECURITY.md §7 "dangling security alert" fix's
# other half (see kodo.transport._connection, ConnectionRegistry.run_ws).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_connection_does_not_cancel_a_pending_gate_future(
    manager_factory,
) -> None:  # type: ignore[no-untyped-def]
    """SessionManager.drop_connection only detaches + starts the grace
    window; unlike the pre-fix Connection.cancel_pending(), it must not
    touch any future registered on the session's channel."""
    import asyncio

    mgr: SessionManager = manager_factory(grace=100.0)
    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    session.channel.register_response_future("req-1", future)

    mgr.drop_connection(conn)

    assert not future.done()
    assert not future.cancelled()


@pytest.mark.asyncio
async def test_replay_backlog_also_replays_pending_requests(manager_factory) -> None:  # type: ignore[no-untyped-def]
    """SessionManager.replay_backlog is the single call site _app.py invokes
    on reconnect (after the base layer); it must fan out to both the
    disconnect-buffered Outbox and any still-unanswered server-initiated
    request, so a reconnecting window re-renders an outstanding prompt panel
    it has no in-memory record of."""
    import asyncio

    from kodo.common import Envelope

    mgr: SessionManager = manager_factory(grace=100.0)
    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    session.channel.register_response_future("req-1", future)
    await session.channel.send(
        Envelope(kind="request", id="req-1", payload={"type": "prompt.permission"})
    )

    mgr.drop_connection(conn)
    conn2 = _conn()
    await mgr.bind_connection(session, conn2)

    await mgr.replay_backlog(session)

    assert conn2.ws.sent  # type: ignore[attr-defined]
    replayed = Envelope.from_json(conn2.ws.sent[0])  # type: ignore[attr-defined]
    assert replayed.id == "req-1"


# ---------------------------------------------------------------------------
# Listing / classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_the_sessions_top_agent(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    await mgr.bind_connection(session, _conn())

    listing = mgr.list_sessions()
    entry = next(s for s in listing if s["id"] == session.id)
    assert entry["taken"] is True
    # The picker row carries the resolved name *and* its label, so the client
    # renders it without a mapping of its own.
    assert entry["agent"] == "kodo_problem_solver"
    assert entry["agent_label"] == "Problem Solver"
    assert entry["workspace"] is None  # no workspace.folders ever pushed
    # A freshly created session reports timestamps, seeded equal at creation.
    assert entry["created_at"]
    assert entry["last_modified"] == entry["created_at"]


@pytest.mark.asyncio
async def test_list_reports_no_workspace_until_a_folder_is_locked(
    manager_factory, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A session that only ever had `workspace.folders` pushed to it — no
    commit, no lock — must stay unbound: this is the exploratory-session
    case (every session in a window gets the live push, per
    `WorkflowEngine.handle_workspace_folders`) that must NOT silently drag
    the session back into a workspace it never earned after a restart."""
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    await mgr.bind_connection(session, _conn())

    folder = tmp_path / "myproj"
    folder.mkdir()
    await session.engine.handle_workspace_folders(
        str(tmp_path), {"myproj": str(folder)}, str(tmp_path / "dev.code-workspace")
    )

    listing = mgr.list_sessions()
    entry = next(s for s in listing if s["id"] == session.id)
    assert entry["workspace"] is None


@pytest.mark.asyncio
async def test_list_reports_remembered_workspace_shape_once_locked(
    manager_factory, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    await mgr.bind_connection(session, _conn())

    folder = tmp_path / "myproj"
    folder.mkdir()
    await session.engine.handle_workspace_folders(
        str(tmp_path), {"myproj": str(folder)}, str(tmp_path / "dev.code-workspace")
    )
    session.engine._transient.lock_workspace_path(str(folder.resolve()))

    # No candidate workspace passed in ⇒ nothing to compare against, so a
    # locked session's `compatible` reports False by default.
    listing = mgr.list_sessions()
    entry = next(s for s in listing if s["id"] == session.id)
    assert entry["workspace"] == {
        "physical_root": str(tmp_path),
        "folders": {"myproj": str(folder)},
        "code_workspace_file": str(tmp_path / "dev.code-workspace"),
        "locked": True,
        "compatible": False,
    }

    # A candidate matching the remembered shape (root + the locked folder
    # present) reports compatible.
    matching = mgr.list_sessions(physical_root=str(tmp_path), folders={"myproj": str(folder)})
    matching_entry = next(s for s in matching if s["id"] == session.id)
    assert matching_entry["workspace"]["compatible"] is True

    # A candidate with the right root but missing the locked folder does not.
    other_folders = {"other": str(tmp_path / "other")}
    mismatched = mgr.list_sessions(physical_root=str(tmp_path), folders=other_folders)
    mismatched_entry = next(s for s in mismatched if s["id"] == session.id)
    assert mismatched_entry["workspace"]["compatible"] is False


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_files_and_frees_ownership(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    await mgr.bind_connection(session, _conn())

    # The session directory is present before deletion.
    assert session.id in {s["id"] for s in mgr.list_sessions()}

    await mgr.delete(session.id)

    # In-memory tracking is gone, the engine is no longer held, and the on-disk
    # session directory has been removed (so it drops out of the listing).
    assert mgr.get(session.id) is None
    assert session.id not in {s["id"] for s in mgr.list_sessions()}

    # Opening the (now nonexistent) id yields a brand-new, empty session object
    # rather than the deleted one — nothing remains on disk to resume.
    reopened = await mgr.open(session.id, "windowB")
    assert reopened is not None and reopened is not session


# ---------------------------------------------------------------------------
# Abstraction-boundary guard
# ---------------------------------------------------------------------------


def test_session_manager_does_not_import_connection_registry() -> None:
    """SessionManager must not depend on the ConnectionRegistry (one-way edge)."""
    source = (Path(kodo.agents.__file__).parents[1] / "server" / "_session_manager.py").read_text()
    assert "_connection_registry" not in source
    assert "ConnectionRegistry" not in source


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def _make_manager(layout: WorkspaceLayout) -> SessionManager:
    return SessionManager(
        registry=AgentRegistry(_AGENTS_DIR),
        gateway=LLMGateway(cloud_concurrency=lambda: 2),
        get_settings=lambda: dict(_SETTINGS),
        layout=layout,
    )


@pytest.mark.asyncio
async def test_lookup_reflects_loaded_and_bound_sessions(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    assert isinstance(mgr.registry, AgentRegistry)
    assert mgr.live_sessions() == []

    session: Session = await mgr.create("windowA")
    conn = _conn()
    await mgr.bind_connection(session, conn)

    assert mgr.live_sessions() == [session]
    assert mgr.get(session.id) is session
    assert mgr.session_for_connection(conn.id) is session
    assert mgr.session_for_connection("unknown-conn") is None
    assert mgr.any_running() is False

    mgr.drop_connection(conn)
    assert mgr.session_for_connection(conn.id) is None


@pytest.mark.asyncio
async def test_dropping_an_unbound_connection_is_a_no_op(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")

    mgr.drop_connection(_conn())

    # Nothing is live and no grace window was started, so another window may
    # take the session over immediately.
    assert await mgr.open(session.id, "windowB") is session


@pytest.mark.asyncio
async def test_create_never_reuses_an_existing_session_directory(manager_factory) -> None:  # type: ignore[no-untyped-def]
    """Session ids are second-resolution timestamps; a collision with an
    existing directory gets a numeric suffix instead of clobbering it."""
    import time

    mgr: SessionManager = manager_factory()
    first: Session = await mgr.create("windowA")
    sessions_dir = Path(first.transient.session_dir).parent
    now = int(time.time())
    taken = {str(now + offset) for offset in range(-1, 10)}
    for name in taken:
        (sessions_dir / name).mkdir(exist_ok=True)

    second: Session = await mgr.create("windowA")

    assert second.id not in taken
    assert second.id.rsplit("-", 1)[0] in taken


# ---------------------------------------------------------------------------
# Resuming a session that is on disk but not in memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_resumes_a_persisted_session_in_a_fresh_manager(
    manager_factory,  # type: ignore[no-untyped-def]
) -> None:
    first: SessionManager = manager_factory()
    original: Session = await first.create("windowA")
    await first.shutdown()

    second: SessionManager = manager_factory()
    assert second.get(original.id) is None

    resumed = await second.open(original.id, "windowB")

    assert resumed is not None
    assert resumed.id == original.id
    assert second.get(original.id) is resumed


# ---------------------------------------------------------------------------
# Listing edge cases (on-disk data written directly)
# ---------------------------------------------------------------------------


def test_list_sessions_is_empty_without_a_sessions_dir(tmp_path: Path) -> None:
    mgr = _make_manager(WorkspaceLayout(tmp_path / "uninitialised"))
    assert mgr.list_sessions() == []


def test_list_sessions_tolerates_missing_and_corrupt_session_files(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    sessions_dir = layout.sessions_dir
    (sessions_dir / "stray-file.txt").write_text("not a session", encoding="utf-8")

    bare = sessions_dir / "100-bare"
    bare.mkdir()

    corrupt = sessions_dir / "200-corrupt"
    corrupt.mkdir()
    (corrupt / "meta.json").write_text("{not json", encoding="utf-8")
    (corrupt / "transient.json").write_text("{not json", encoding="utf-8")

    legacy = sessions_dir / "300-legacy"
    legacy.mkdir()
    (legacy / "meta.json").write_text(
        json.dumps({"session_name": "Legacy", "created_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    (legacy / "transient.json").write_text(
        json.dumps({"workflow_mode": "kodo_problem_solver", "workspace_locked_paths": []}),
        encoding="utf-8",
    )

    listing = {row["id"]: row for row in _make_manager(layout).list_sessions()}

    assert set(listing) == {"100-bare", "200-corrupt", "300-legacy"}
    for sid in ("100-bare", "200-corrupt"):
        row = listing[sid]
        assert row["name"] == sid
        assert row["created_at"] == ""
        assert row["last_modified"] == ""
        assert row["agent"] is None
        assert row["agent_label"] is None
        assert row["workspace"] is None
        assert row["taken"] is False

    legacy_row = listing["300-legacy"]
    assert legacy_row["name"] == "Legacy"
    # last_modified falls back to created_at for pre-field sessions.
    assert legacy_row["last_modified"] == "2026-01-01T00:00:00"
    # The pre-rename `workflow_mode` key still yields the agent row.
    assert legacy_row["agent"] == "kodo_problem_solver"
    assert legacy_row["agent_label"] == "Problem Solver"
    # Nothing locked ⇒ no workspace claim.
    assert legacy_row["workspace"] is None


def test_list_sessions_reports_a_locked_workspace_without_code_file(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    session_dir = layout.sessions_dir / "400-locked"
    session_dir.mkdir()
    root = str(tmp_path / "proj")
    (session_dir / "transient.json").write_text(
        json.dumps(
            {
                "workspace_locked_paths": [f"{root}/a", 7],
                "workspace_physical_root": root,
                "workspace_folders": ["not", "a", "dict"],
                "workspace_code_file": "",
            }
        ),
        encoding="utf-8",
    )

    (row,) = _make_manager(layout).list_sessions()

    workspace = row["workspace"]
    assert isinstance(workspace, dict)
    assert workspace["physical_root"] == root
    assert workspace["folders"] == {}
    assert workspace["code_workspace_file"] is None
    assert workspace["locked"] is True


# ---------------------------------------------------------------------------
# Per-session security rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_rules_on_a_loaded_session_round_trip(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    session.transient.add_security_rule("git", "push")
    session.transient.add_security_rule("git", "commit")
    session.transient.add_security_path_rule("rm", "/tmp/scratch")

    assert mgr.list_session_security_rules(session.id) == [
        {"kind": "command", "executable": "git", "value": "commit"},
        {"kind": "command", "executable": "git", "value": "push"},
        {"kind": "path", "executable": "rm", "value": "/tmp/scratch"},
    ]

    remaining = mgr.delete_session_security_rules(
        session.id,
        [
            {"kind": "command", "executable": "git", "value": "push"},
            {"kind": "path", "executable": "rm", "value": "/tmp/scratch"},
            {"kind": "command", "executable": "", "value": "ignored"},
            {"kind": "command", "executable": "nope", "value": "unknown"},
        ],
    )

    assert remaining == [{"kind": "command", "executable": "git", "value": "commit"}]
    assert session.transient.security_rules == frozenset({("git", "commit")})
    assert session.transient.security_path_rules == frozenset()


def _write_transient(layout: WorkspaceLayout, session_id: str, data: object) -> Path:
    session_dir = layout.sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "transient.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_security_rules_of_an_unloaded_session_are_read_and_patched_on_disk(
    tmp_path: Path,
) -> None:
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    path = _write_transient(
        layout,
        "500-disk",
        {
            "stage": "IDLE",
            "security_rules": [["git", "push"], ["git", "commit"], ["malformed"], "x"],
            "security_path_rules": [["rm", "/tmp/scratch"]],
        },
    )
    mgr = _make_manager(layout)

    assert mgr.list_session_security_rules("500-disk") == [
        {"kind": "command", "executable": "git", "value": "commit"},
        {"kind": "command", "executable": "git", "value": "push"},
        {"kind": "path", "executable": "rm", "value": "/tmp/scratch"},
    ]

    remaining = mgr.delete_session_security_rules(
        "500-disk",
        [
            {"kind": "command", "executable": "git", "value": "push"},
            {"kind": "path", "executable": "rm", "value": "/tmp/scratch"},
            {"kind": "path", "executable": "rm", "value": ""},
        ],
    )

    assert remaining == [{"kind": "command", "executable": "git", "value": "commit"}]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["security_rules"] == [["git", "commit"]]
    assert on_disk["security_path_rules"] == []
    assert on_disk["stage"] == "IDLE"  # untouched fields survive the patch


@pytest.mark.parametrize("content", [None, "{broken", "[]", "{}"])
def test_security_rules_of_an_unloaded_session_without_usable_state(
    tmp_path: Path, content: str | None
) -> None:
    """No file, an unreadable file, a non-object, or an empty object: nothing
    to list, and a delete is a no-op that leaves the file as it was."""
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    session_dir = layout.sessions_dir / "600-empty"
    session_dir.mkdir()
    path = session_dir / "transient.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    mgr = _make_manager(layout)

    assert mgr.list_session_security_rules("600-empty") == []
    rule = {"kind": "command", "executable": "git", "value": "push"}
    assert mgr.delete_session_security_rules("600-empty", [rule]) == []

    if content is None:
        assert not path.exists()
    else:
        assert path.read_text(encoding="utf-8") == content


def test_security_rule_list_ignores_non_list_rule_fields(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    _write_transient(
        layout, "700-odd", {"security_rules": "git push", "security_path_rules": {"a": 1}}
    )
    assert _make_manager(layout).list_session_security_rules("700-odd") == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_security_rule_delete_survives_an_unwritable_transient_file(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "home")
    layout.init()
    path = _write_transient(layout, "800-ro", {"security_rules": [["git", "push"]]})
    path.chmod(0o444)
    mgr = _make_manager(layout)
    try:
        rule = {"kind": "command", "executable": "git", "value": "push"}
        # The write fails and is logged; the caller still gets the (unchanged)
        # on-disk rule set rather than an exception.
        remaining = mgr.delete_session_security_rules("800-ro", [rule])
    finally:
        path.chmod(0o644)

    assert remaining == [rule]
