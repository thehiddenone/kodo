"""Tests for the checkpoint coordinator + mutation heuristic.

Covers :class:`kodo.runtime._checkpoints.RootMirrorManager` and
:func:`command_may_mutate`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.runtime._checkpoints import RootMirrorManager, command_may_mutate
from kodo.shellparser import parse_command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hi", False),
        ("ls -la", False),
        ("grep x f | sort", False),
        ("cat foo", False),
        ("/usr/bin/cat foo", False),
        ("cat > f", True),
        ("echo x >> log", True),
        ("sed -i s/a/b/ f", True),
        ("rm -rf dist", True),
        ("a && b > f", True),
        ("npm install", True),
        ("./build.sh", True),
        ("python script.py", True),
        ("", False),
    ],
)
def test_command_may_mutate(command: str, expected: bool) -> None:
    assert command_may_mutate(parse_command(command)) is expected


async def test_first_change_after_prepare_is_a_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "preexist.txt").write_text("keep\n")
    mgr = RootMirrorManager([tmp_path])
    # prepare BEFORE the write so the baseline excludes the change.
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref is not None
    assert ref.root == str(tmp_path.resolve())
    assert (tmp_path / ".kodo" / "kodo.md").exists()
    assert (tmp_path / ".kodo" / "checkpoints" / ".git" / "HEAD").exists()


async def test_undo_and_rollback_roundtrip(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    ref1 = await mgr.commit_for_path(tmp_path / "a.txt", "create a")
    assert ref1 is not None

    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("two\n")
    ref2 = await mgr.commit_for_path(tmp_path / "a.txt", "edit a")
    assert ref2 is not None

    # Undo the creation: a.txt disappears, and the original entry flips undone.
    state = await mgr.undo(ref1.root, ref1.sha)
    assert not (tmp_path / "a.txt").exists()
    assert state.entries[state.index_of(ref1.sha)].undone is True

    # Rollback to the second checkpoint: a.txt == "two", current_index moves there.
    state = await mgr.rollback(ref2.root, ref2.sha)
    assert (tmp_path / "a.txt").read_text() == "two\n"
    assert state.current_index == state.index_of(ref2.sha)


async def test_path_outside_roots_yields_no_checkpoint(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path / "inside"])
    (tmp_path / "inside").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    assert await mgr.commit_for_path(outside, "x") is None


async def test_noop_commit_returns_none(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    assert await mgr.commit_for_path(tmp_path / "a.txt", "create") is not None
    # No change since the last commit → no checkpoint.
    assert await mgr.commit_for_path(tmp_path / "a.txt", "noop") is None


async def test_two_roots_map_to_independent_mirrors(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    mgr = RootMirrorManager([root_a, root_b])

    await mgr.prepare(root_a / "f.txt")
    (root_a / "f.txt").write_text("a\n")
    ref_a = await mgr.commit_for_path(root_a / "f.txt", "a")

    await mgr.prepare(root_b / "f.txt")
    (root_b / "f.txt").write_text("b\n")
    ref_b = await mgr.commit_for_path(root_b / "f.txt", "b")

    assert ref_a is not None and ref_b is not None
    assert ref_a.root == str(root_a.resolve())
    assert ref_b.root == str(root_b.resolve())
    assert (root_a / ".kodo" / "checkpoints").exists()
    assert (root_b / ".kodo" / "checkpoints").exists()


async def test_sweep_initialized_returns_only_roots_that_actually_committed(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    mgr = RootMirrorManager([root_a, root_b])

    await mgr.prepare(root_a / "f.txt")
    (root_a / "f.txt").write_text("a\n")
    assert await mgr.commit_for_path(root_a / "f.txt", "a") is not None

    await mgr.prepare(root_b / "f.txt")
    (root_b / "f.txt").write_text("b\n")
    assert await mgr.commit_for_path(root_b / "f.txt", "b") is not None

    # root_a is clean since its last commit; root_b picked up a further write
    # that was never explicitly committed — the sweep should catch only it.
    (root_b / "f.txt").write_text("b2\n")

    committed = await mgr.sweep_initialized("sweep")
    assert committed == [root_b.resolve()]


async def test_sweep_initialized_returns_empty_when_clean(tmp_path: Path) -> None:
    mgr = RootMirrorManager([tmp_path])
    await mgr.prepare(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("one\n")
    assert await mgr.commit_for_path(tmp_path / "a.txt", "create") is not None
    assert await mgr.sweep_initialized("sweep") == []
