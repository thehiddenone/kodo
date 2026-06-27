"""Tests for the generic shadow-git mirror engine (``kodo.mirror``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kodo.mirror import ShadowMirror

_EXCLUDES = (".kodo/", ".git/", "node_modules/")


def _tracked(mirror: ShadowMirror) -> set[str]:
    out = subprocess.run(
        ["git", f"--git-dir={mirror.git_dir}", f"--work-tree={mirror.work_tree}", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


async def _new_mirror(root: Path) -> ShadowMirror:
    mirror = ShadowMirror(root, root / ".kodo" / "checkpoints" / ".git")
    await mirror.init(_EXCLUDES)
    return mirror


async def test_init_baselines_existing_files_and_respects_excludes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("keep\n")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("no\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("dep\n")
    (tmp_path / ".kodo").mkdir()
    (tmp_path / ".kodo" / "marker").write_text("internal\n")

    mirror = await _new_mirror(tmp_path)
    tracked = _tracked(mirror)
    assert "a.txt" in tracked
    assert ".gitignore" in tracked
    assert "ignored.txt" not in tracked  # project .gitignore honoured
    assert "node_modules/x.js" not in tracked  # Kodo exclude
    assert not any(t.startswith(".kodo/") for t in tracked)


async def test_commit_detects_changes_and_noop_is_stable(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    base = await mirror.head_sha()
    (tmp_path / "a.txt").write_text("one\n")
    sha = await mirror.commit("add a")
    assert sha != base
    assert await mirror.paths_changed(sha) == ["a.txt"]
    # Nothing changed → no new commit, HEAD unchanged.
    assert await mirror.commit("noop") == sha


async def test_undo_restores_only_touched_files(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha_a = await mirror.commit("create a")
    (tmp_path / "b.txt").write_text("b\n")
    await mirror.commit("create b")
    (tmp_path / "a.txt").write_text("one-edited\n")
    await mirror.commit("edit a")

    await mirror.undo(sha_a)
    # a.txt reverts to its pre-sha_a state (it did not exist → removed),
    # discarding the later edit; b.txt is untouched.
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text() == "b\n"


async def test_rollback_restores_tree_and_rolls_forward(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha1 = await mirror.commit("c1")
    (tmp_path / "a.txt").write_text("two\n")
    (tmp_path / "b.txt").write_text("b\n")
    sha2 = await mirror.commit("c2")

    await mirror.rollback(sha1)
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert not (tmp_path / "b.txt").exists()  # file added after sha1 is dropped

    # Append-only: rolling to the newer commit rolls forward.
    await mirror.rollback(sha2)
    assert (tmp_path / "a.txt").read_text() == "two\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"


async def test_history_is_append_only(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha1 = await mirror.commit("c1")
    (tmp_path / "a.txt").write_text("two\n")
    await mirror.commit("c2")
    # Rolling back to an *earlier* state changes the tree → a new commit.
    await mirror.rollback(sha1)
    log = await mirror.log()
    # baseline + c1 + c2 + rollback commit, newest first.
    assert len(log) >= 4
    assert log[0].sha == await mirror.head_sha()
    assert log[0].message.startswith("rollback")


async def test_is_initialized(tmp_path: Path) -> None:
    mirror = ShadowMirror(tmp_path, tmp_path / ".kodo" / "checkpoints" / ".git")
    assert not mirror.is_initialized()
    await mirror.init(_EXCLUDES)
    assert mirror.is_initialized()


@pytest.mark.parametrize("name", ["a.txt", "sub/dir/file.py"])
async def test_nested_paths_are_tracked(tmp_path: Path, name: str) -> None:
    mirror = await _new_mirror(tmp_path)
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n")
    sha = await mirror.commit("add")
    assert name in await mirror.paths_changed(sha)
