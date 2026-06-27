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


def _branches(mirror: ShadowMirror) -> list[str]:
    out = subprocess.run(
        [
            "git",
            f"--git-dir={mirror.git_dir}",
            f"--work-tree={mirror.work_tree}",
            "branch",
            "--format=%(refname:short)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [b for b in out.stdout.split() if b]


def _commits_reachable_from(mirror: ShadowMirror, ref: str) -> set[str]:
    out = subprocess.run(
        [
            "git",
            f"--git-dir={mirror.git_dir}",
            f"--work-tree={mirror.work_tree}",
            "log",
            "--format=%H",
            ref,
        ],
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


async def test_redo_reapplies_only_touched_files(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha_a = await mirror.commit("create a")
    (tmp_path / "b.txt").write_text("b\n")
    await mirror.commit("create b")

    await mirror.undo(sha_a)
    assert not (tmp_path / "a.txt").exists()

    await mirror.redo(sha_a)
    # a.txt is back to the content sha_a introduced; b.txt is untouched.
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"


async def test_rollback_moves_branch_without_detaching_head(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha1 = await mirror.commit("c1")
    (tmp_path / "a.txt").write_text("two\n")
    (tmp_path / "b.txt").write_text("b\n")
    sha2 = await mirror.commit("c2")
    branch = await mirror.branch_name()

    await mirror.rollback(sha1)

    assert await mirror.head_sha() == sha1
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert not (tmp_path / "b.txt").exists()  # file added after sha1 is dropped
    # Never detached: still on the same named branch, just repointed — this is
    # what `branch_name()` proves, since it raises on a detached HEAD.
    assert await mirror.branch_name() == branch

    # sha2 is preserved (not garbage) on the rollback_<ts> branch git created
    # to avoid orphaning it, even though it's no longer on `branch`'s history.
    preserved = [b for b in _branches(mirror) if b.startswith("rollback_")]
    assert len(preserved) == 1
    assert sha2 in _commits_reachable_from(mirror, preserved[0])
    assert sha2 not in {c.sha for c in await mirror.log()}

    # Rolling forward to sha2 is the very same primitive, in the other direction.
    await mirror.rollback(sha2)
    assert await mirror.head_sha() == sha2
    assert (tmp_path / "a.txt").read_text() == "two\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"
    assert await mirror.branch_name() == branch


async def test_rollback_to_current_tip_is_a_noop(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    sha = await mirror.commit("c1")
    before = _branches(mirror)

    assert await mirror.rollback(sha) == sha
    # No spurious rollback_<ts> branch when there's nothing to preserve.
    assert _branches(mirror) == before


async def test_is_dirty_and_stash_round_trip(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    await mirror.commit("c1")
    assert not await mirror.is_dirty()

    # An edit Kodo didn't commit, plus an untracked file — both count as dirty.
    (tmp_path / "a.txt").write_text("edited-outside-kodo\n")
    (tmp_path / "untracked.txt").write_text("new\n")
    assert await mirror.is_dirty()

    assert await mirror.stash_push() is True
    assert not await mirror.is_dirty()
    assert (tmp_path / "a.txt").read_text() == "one\n"
    assert not (tmp_path / "untracked.txt").exists()

    await mirror.stash_pop()
    assert (tmp_path / "a.txt").read_text() == "edited-outside-kodo\n"
    assert (tmp_path / "untracked.txt").read_text() == "new\n"


async def test_stash_push_is_noop_on_clean_tree(tmp_path: Path) -> None:
    mirror = await _new_mirror(tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    await mirror.commit("c1")
    assert await mirror.stash_push() is False


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
