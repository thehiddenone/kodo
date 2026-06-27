"""Tests for the persisted, stateful checkpoint model.

Covers :class:`kodo.runtime._checkpoints.CheckpointState` /
:class:`CheckpointEntry` serialization, the ``current_index``/``undone``
bookkeeping :class:`RootMirrorManager` maintains across undo/redo/rollback/
roll-forward, persistence across manager instances (the session-resume path),
and the dirty-work-tree confirmation flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.runtime._checkpoints import (
    CheckpointEntry,
    CheckpointState,
    MirrorDirtyError,
    RootMirrorManager,
)


def test_checkpoint_state_json_round_trip() -> None:
    state = CheckpointState(
        entries=[
            CheckpointEntry(sha="a" * 40, parent="0" * 40, label="create a", kind="tool_call"),
            CheckpointEntry(
                sha="b" * 40, parent="a" * 40, label="undo a", kind="undo", undone=False, ts="t1"
            ),
        ],
        current_index=1,
    )
    restored = CheckpointState.from_json(state.to_json())
    assert restored == state


def test_checkpoint_state_index_of() -> None:
    entry = CheckpointEntry(sha="x", parent="", label="", kind="tool_call")
    state = CheckpointState(entries=[entry])
    assert state.index_of("x") == 0
    assert state.index_of("missing") is None


async def test_new_checkpoints_always_append_and_advance_current_index(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref1 = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref1 is not None

    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("two\n")
    ref2 = await mgr.commit_for_path(tmp_path / "a.txt", "edit a")
    assert ref2 is not None

    state = await mgr.state_for(str(tmp_path))
    assert [e.sha for e in state.entries] == [ref1.sha, ref2.sha]
    assert state.current_index == 1


async def test_undo_then_redo_toggles_undone_flag(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref is not None

    state = await mgr.undo(ref.root, ref.sha)
    assert not (tmp_path / "a.txt").exists()
    original_index = state.index_of(ref.sha)
    assert original_index is not None
    assert state.entries[original_index].undone is True
    # The undo itself is recorded as a new, current entry.
    assert state.current_index == len(state.entries) - 1

    state = await mgr.redo(ref.root, ref.sha)
    assert (tmp_path / "a.txt").read_text() == "one\n"
    original_index = state.index_of(ref.sha)
    assert original_index is not None
    assert state.entries[original_index].undone is False
    assert state.current_index == len(state.entries) - 1


async def test_rollback_and_roll_forward_only_move_current_index(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref1 = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref1 is not None
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("two\n")
    ref2 = await mgr.commit_for_path(tmp_path / "a.txt", "edit a")
    assert ref2 is not None

    state = await mgr.rollback(ref1.root, ref1.sha)
    assert len(state.entries) == 2  # rollback never appends, only repoints
    assert state.current_index == 0
    assert (tmp_path / "a.txt").read_text() == "one\n"

    # roll_forward is the same primitive in the other direction.
    state = await mgr.roll_forward(ref2.root, ref2.sha)
    assert len(state.entries) == 2
    assert state.current_index == 1
    assert (tmp_path / "a.txt").read_text() == "two\n"


async def test_state_persists_across_manager_instances(tmp_path: Path) -> None:
    """Simulates the session-resume path: a fresh manager reloads state.json."""
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref is not None
    await mgr.undo(ref.root, ref.sha)

    fresh = RootMirrorManager([tmp_path])
    state = await fresh.state_for(str(tmp_path))
    assert len(state.entries) == 2
    assert state.entries[0].undone is True
    assert state.current_index == 1


async def test_dirty_tree_blocks_until_resolved(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref is not None

    # An edit made outside of Kodo, never committed to the mirror.
    (tmp_path / "untracked.txt").write_text("surprise\n")

    with pytest.raises(MirrorDirtyError):
        await mgr.undo(ref.root, ref.sha)
    # Still there — the failed attempt didn't touch anything.
    assert (tmp_path / "untracked.txt").read_text() == "surprise\n"

    await mgr.undo(ref.root, ref.sha, resolution="stash")
    assert not (tmp_path / "a.txt").exists()
    # Stashed change is reapplied afterwards.
    assert (tmp_path / "untracked.txt").read_text() == "surprise\n"


async def test_dirty_tree_discard_resolution_proceeds_without_stashing(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref is not None

    (tmp_path / "a.txt").write_text("edited-outside-kodo\n")
    await mgr.undo(ref.root, ref.sha, resolution="discard")
    assert not (tmp_path / "a.txt").exists()
