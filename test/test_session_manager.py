"""Behavioral tests for kodo.server.SessionManager — server-authoritative
single-window ownership, the disconnect grace window, and session listing.

No LLM calls are made; engines start their worker (idle on an empty queue) and
are torn down via ``manager.shutdown()``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

import kodo.subagents
from kodo.llms import LLMGateway
from kodo.project import WorkspaceLayout
from kodo.server import SessionManager
from kodo.server._session_manager import Session
from kodo.subagents import AgentRegistry
from kodo.transport import Connection

_AGENTS_DIR = Path(kodo.subagents.__file__).parent
_SETTINGS: dict[str, object] = {"mode": "local", "models": {"local": "llamacpp-qwen36-27b"}}


class _FakeWS:
    """Minimal stand-in for an aiohttp WebSocketResponse."""

    closed = False

    async def send_str(self, _data: str) -> None:
        return None


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

    await asyncio.sleep(0.12)  # let grace expire

    reopened = await mgr.open(session.id, "windowB")
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
# Listing / classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_problem_solving_session(manager_factory) -> None:  # type: ignore[no-untyped-def]
    mgr: SessionManager = manager_factory()
    session: Session = await mgr.create("windowA")
    await mgr.bind_connection(session, _conn())

    listing = mgr.list_sessions()
    entry = next(s for s in listing if s["id"] == session.id)
    assert entry["taken"] is True
    assert entry["project_root"] is None  # no guided work ⇒ openable anywhere


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
    source = (
        Path(kodo.subagents.__file__).parents[1] / "server" / "_session_manager.py"
    ).read_text()
    assert "_connection_registry" not in source
    assert "ConnectionRegistry" not in source
