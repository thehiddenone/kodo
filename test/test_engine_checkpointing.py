"""Unit tests for ``kodo.runtime._engine._checkpointing.CheckpointCoordinator``.

``RootMirrorManager`` itself (the shadow-git engine) is already covered by
``test_checkpoints.py``; these tests exercise the coordinator layer built on
top of it — path resolution, labels, the prepare/commit cycle, guided-state
revision recording, and the undo/redo/rollback/state broadcast wrappers —
against a real mirror manager rooted at a temp directory.
"""

from __future__ import annotations

from pathlib import Path

from kodo.runtime._checkpoints import CheckpointRef
from kodo.runtime._engine._checkpointing import CheckpointCoordinator
from kodo.runtime._session import SessionState
from kodo.state import TransientStore
from kodo.tools import RootPath


class _FakeResolver:
    """Resolves paths under one fixed root, like ``ProjectPathResolver``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def resolve(self, path: str) -> Path:
        if path.startswith("BAD"):
            raise PermissionError("no")
        return (self._root / path).resolve()

    @property
    def default_cwd(self) -> Path:
        return self._root


class _FakeHost:
    def __init__(self, root: Path, *, current_project: dict[str, str] | None = None) -> None:
        self._root = root
        self._session = SessionState(session_id="s1")
        self._current_project = current_project
        self._orch_session_id = "s1"
        self._transient = TransientStore(root / ".kodo-transient")

    def _make_resolver(self, session_id: str) -> _FakeResolver:
        return _FakeResolver(self._root)

    def _root_paths(self) -> tuple[RootPath, ...]:
        return (RootPath(name="root", path=str(self._root)),)


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, env: object) -> None:
        self.sent.append(env)


def _make_coordinator(tmp_path: Path, *, current_project: dict[str, str] | None = None):
    host = _FakeHost(tmp_path, current_project=current_project)
    sink = _FakeSink()
    coordinator = CheckpointCoordinator(host, sink=sink)  # type: ignore[arg-type]
    return coordinator, host, sink


# ---------------------------------------------------------------------------
# mutation_paths
# ---------------------------------------------------------------------------


def test_mutation_paths_edit_file(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = coordinator.mutation_paths("edit_file", {"path": "a.txt"})
    assert paths == [(tmp_path / "a.txt").resolve()]


def test_mutation_paths_edit_file_missing_path_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mutation_paths("edit_file", {}) == []


def test_mutation_paths_edit_file_unresolvable_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mutation_paths("edit_file", {"path": "BAD"}) == []


def test_mutation_paths_filesystem_prefers_destination_then_path_then_source(
    tmp_path: Path,
) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = coordinator.mutation_paths(
        "filesystem", {"destination": "d.txt", "path": "p.txt", "source": "s.txt"}
    )
    assert paths == [
        (tmp_path / "d.txt").resolve(),
        (tmp_path / "p.txt").resolve(),
        (tmp_path / "s.txt").resolve(),
    ]


def test_mutation_paths_filesystem_only_path(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = coordinator.mutation_paths("filesystem", {"path": "p.txt"})
    assert paths == [(tmp_path / "p.txt").resolve()]


def test_mutation_paths_run_command_non_mutating_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mutation_paths("run_command", {"command": "ls -la"}) == []


def test_mutation_paths_run_command_blank_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mutation_paths("run_command", {"command": "   "}) == []


def test_mutation_paths_run_command_mutating_uses_default_cwd(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = coordinator.mutation_paths("run_command", {"command": "rm -rf dist"})
    assert paths == [tmp_path]


def test_mutation_paths_run_command_uses_working_dir_when_given(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    (tmp_path / "sub").mkdir()
    paths = coordinator.mutation_paths("run_command", {"command": "rm -rf x", "working_dir": "sub"})
    assert paths == [(tmp_path / "sub").resolve()]


def test_mutation_paths_run_command_falls_back_on_bad_working_dir(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = coordinator.mutation_paths("run_command", {"command": "rm -rf x", "working_dir": "BAD"})
    assert paths == [tmp_path]


def test_mutation_paths_unknown_tool_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mutation_paths("some_other_tool", {}) == []


# ---------------------------------------------------------------------------
# label
# ---------------------------------------------------------------------------


def test_label_run_command_truncates_to_80_chars() -> None:
    long_cmd = "x" * 200
    label = CheckpointCoordinator.label("run_command", {"command": long_cmd})
    assert label == f"run_command: {'x' * 80}"


def test_label_filesystem_uses_operation_and_path() -> None:
    label = CheckpointCoordinator.label("filesystem", {"operation": "write", "path": "a.txt"})
    assert label == "filesystem write: a.txt"


def test_label_filesystem_falls_back_to_destination() -> None:
    label = CheckpointCoordinator.label("filesystem", {"operation": "move", "destination": "b.txt"})
    assert label == "filesystem move: b.txt"


def test_label_default_tool_uses_path() -> None:
    label = CheckpointCoordinator.label("edit_file", {"path": "c.txt"})
    assert label == "edit_file: c.txt"


# ---------------------------------------------------------------------------
# prepare / commit (drives the real RootMirrorManager)
# ---------------------------------------------------------------------------


async def test_prepare_non_mutating_tool_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert await coordinator.prepare("read_file", {"path": "a.txt"}) == []


async def test_prepare_mutating_tool_with_no_resolvable_path_returns_empty(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert await coordinator.prepare("edit_file", {}) == []


async def test_prepare_skips_temporary_call(tmp_path: Path) -> None:
    # A `temporary: true` call is scoped to the session's scratch directory,
    # never the project — it must never earn a mirror checkpoint.
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert await coordinator.prepare("edit_file", {"path": "a.txt", "temporary": True}) == []
    assert (
        await coordinator.prepare(
            "filesystem", {"operation": "delete_dir", "path": "a", "temporary": True}
        )
        == []
    )


async def test_prepare_and_commit_round_trip(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)

    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    assert paths == [(tmp_path / "a.txt").resolve()]

    (tmp_path / "a.txt").write_text("hello\n")
    ref = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)

    assert ref is not None
    assert isinstance(ref, CheckpointRef)


async def test_commit_with_no_paths_returns_none(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert await coordinator.commit("edit_file", {}, []) is None


async def test_commit_run_command_sweeps_other_mirrors(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    paths = await coordinator.prepare("run_command", {"command": "touch a.txt"})
    (tmp_path / "a.txt").write_text("x\n")

    ref = await coordinator.commit("run_command", {"command": "touch a.txt"}, paths)

    assert ref is not None


async def test_commit_locks_the_committed_root(tmp_path: Path) -> None:
    coordinator, host, _sink = _make_coordinator(tmp_path)
    assert host._transient.workspace_locked_paths == frozenset()

    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    (tmp_path / "a.txt").write_text("hello\n")
    ref = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)

    assert ref is not None
    assert host._transient.workspace_locked_paths == frozenset({ref.root})


async def test_commit_with_no_paths_does_not_lock_anything(tmp_path: Path) -> None:
    coordinator, host, _sink = _make_coordinator(tmp_path)
    await coordinator.commit("edit_file", {}, [])
    assert host._transient.workspace_locked_paths == frozenset()


async def test_noop_commit_does_not_lock_anything(tmp_path: Path) -> None:
    coordinator, host, _sink = _make_coordinator(tmp_path)
    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    (tmp_path / "a.txt").write_text("hello\n")
    ref = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)
    assert ref is not None

    # Same content again → no-op commit; the root is already locked from the
    # first real commit above, so this just confirms nothing new is added.
    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    ref2 = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)
    assert ref2 is None
    assert host._transient.workspace_locked_paths == frozenset({ref.root})


# ---------------------------------------------------------------------------
# record_guided_revision
# ---------------------------------------------------------------------------


async def test_record_guided_revision_noop_without_current_project(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path, current_project=None)
    ref = CheckpointRef(root=str(tmp_path), sha="deadbeef", parent="parent-sha")

    # Should not raise even though there's no project bound.
    await coordinator.record_guided_revision("edit_file", {"path": "specs/a.md"}, ref, "guide")


async def test_record_guided_revision_noop_for_untracked_path(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(
        tmp_path, current_project={"root": str(tmp_path), "name": "proj"}
    )
    ref = CheckpointRef(root=str(tmp_path), sha="deadbeef", parent="parent-sha")

    await coordinator.record_guided_revision("edit_file", {"path": "outside.md"}, ref, "guide")

    assert not (tmp_path / ".kodo" / "guided_dev_state").exists()


async def test_record_guided_revision_writes_for_tracked_path(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("content\n")
    coordinator, _host, _sink = _make_coordinator(
        tmp_path, current_project={"root": str(tmp_path), "name": "proj"}
    )
    ref = CheckpointRef(root=str(tmp_path), sha="deadbeef", parent="parent-sha")

    await coordinator.record_guided_revision("edit_file", {"path": "specs/a.md"}, ref, "guide")

    jsonl = tmp_path / ".kodo" / "guided_dev_state" / "specs" / "a.md.jsonl"
    assert jsonl.exists()
    assert "deadbeef" in jsonl.read_text()


# ---------------------------------------------------------------------------
# undo / redo / rollback / roll_forward / state_for / push_state
# ---------------------------------------------------------------------------


async def test_undo_redo_rollback_roll_forward_and_state_for(tmp_path: Path) -> None:
    coordinator, _host, sink = _make_coordinator(tmp_path)

    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    (tmp_path / "a.txt").write_text("one\n")
    ref1 = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)
    assert ref1 is not None

    paths = await coordinator.prepare("edit_file", {"path": "a.txt"})
    (tmp_path / "a.txt").write_text("two\n")
    ref2 = await coordinator.commit("edit_file", {"path": "a.txt"}, paths)
    assert ref2 is not None

    state = await coordinator.undo(ref1.root, ref1.sha)
    assert not (tmp_path / "a.txt").exists()

    state = await coordinator.redo(ref1.root, ref1.sha)
    assert (tmp_path / "a.txt").read_text() == "one\n"

    state = await coordinator.rollback(ref2.root, ref2.sha)
    assert (tmp_path / "a.txt").read_text() == "two\n"

    state = await coordinator.roll_forward(ref2.root, ref2.sha)
    assert isinstance(state.current_index, int)

    fetched = await coordinator.state_for(ref1.root)
    assert fetched.current_index == state.current_index

    # Every operation above pushed a checkpoint.state event.
    assert len(sink.sent) >= 4
    assert all(env.payload["type"] == "checkpoint.state" for env in sink.sent)
    assert sink.sent[-1].payload["root"] == ref1.root


async def test_push_state_payload_shape(tmp_path: Path) -> None:
    from kodo.runtime._checkpoints import CheckpointEntry, CheckpointState

    coordinator, _host, sink = _make_coordinator(tmp_path)
    state = CheckpointState(
        current_index=0,
        entries=[
            CheckpointEntry(
                sha="abc123", parent="parent-sha", label="x", kind="tool_call", undone=False
            )
        ],
    )

    await coordinator.push_state("root1", state)

    assert sink.sent[0].payload == {
        "type": "checkpoint.state",
        "root": "root1",
        "current_index": 0,
        "entries": [{"sha": "abc123", "undone": False}],
    }


# ---------------------------------------------------------------------------
# sync_roots / mirrors property
# ---------------------------------------------------------------------------


def test_mirrors_property_returns_manager(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    assert coordinator.mirrors is coordinator._mirrors


async def test_sync_roots_updates_manager_roots(tmp_path: Path) -> None:
    coordinator, _host, _sink = _make_coordinator(tmp_path)
    coordinator.sync_roots()
    # The manager now knows about tmp_path as a root: a path under it commits.
    await coordinator._mirrors.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("x\n")
    ref = await coordinator._mirrors.commit_for_path(tmp_path / "a.txt", "x")
    assert ref is not None
