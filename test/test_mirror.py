"""Behavior tests for kodo.mirror._repo and ._checkpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.mirror._checkpoints import CheckpointManager
from kodo.mirror._repo import CheckpointInfo, MirrorRepo

# ---------------------------------------------------------------------------
# MirrorRepo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mirror_init_creates_git_repo(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    assert not repo.is_initialized()
    await repo.init()
    assert repo.is_initialized()
    assert (repo_dir / ".git").is_dir()


@pytest.mark.asyncio
async def test_mirror_init_creates_initial_commit(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()
    commits = await repo.log()
    assert len(commits) == 1
    assert commits[0].message == "init: kodo mirror"


@pytest.mark.asyncio
async def test_mirror_stage_and_commit_creates_commit(tmp_path: Path) -> None:
    """
    Given a file written to the mirror working tree,
    when stage_and_commit() is called,
    then a 40-char SHA is returned and the commit appears in the log.
    """
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    (repo_dir / "narrative.md").write_text("# Narrative\nHello world.", encoding="utf-8")
    sha = await repo.stage_and_commit("[narrative] approved")
    assert len(sha) == 40

    commits = await repo.log()
    assert commits[0].message == "[narrative] approved"


@pytest.mark.asyncio
async def test_mirror_stage_and_commit_noop_returns_head_sha(tmp_path: Path) -> None:
    """
    Given nothing written to the mirror working tree,
    when stage_and_commit() is called,
    then the existing HEAD SHA is returned and no new commit is created.
    """
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    init_sha = await repo.head_sha()
    returned_sha = await repo.stage_and_commit("empty")
    assert returned_sha == init_sha
    assert len(await repo.log()) == 1


@pytest.mark.asyncio
async def test_mirror_checkout_restores_working_tree(tmp_path: Path) -> None:
    """
    Given two commits with different file content,
    when checkout() is called with the first commit SHA,
    then the working tree reflects the first commit's content.
    """
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    (repo_dir / "file.md").write_text("v1", encoding="utf-8")
    sha1 = await repo.stage_and_commit("v1")
    (repo_dir / "file.md").write_text("v2", encoding="utf-8")
    await repo.stage_and_commit("v2")

    await repo.checkout(sha1)
    assert (repo_dir / "file.md").read_text(encoding="utf-8") == "v1"


@pytest.mark.asyncio
async def test_mirror_log_returns_newest_first(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    (repo_dir / "a.md").write_text("a", encoding="utf-8")
    await repo.stage_and_commit("first checkpoint")
    (repo_dir / "b.md").write_text("b", encoding="utf-8")
    await repo.stage_and_commit("second checkpoint")

    commits = await repo.log()
    assert commits[0].message == "second checkpoint"
    assert commits[1].message == "first checkpoint"


@pytest.mark.asyncio
async def test_mirror_log_entries_have_correct_type(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()
    commits = await repo.log()
    assert all(isinstance(c, CheckpointInfo) for c in commits)


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_manager_creates_correct_message(tmp_path: Path) -> None:
    """
    Given a file in the mirror working tree,
    when create_checkpoint('narrative') is called,
    then the commit message is '[narrative] approved' and a 40-char SHA is returned.
    """
    from kodo.project._layout import ProjectLayout

    layout = ProjectLayout(tmp_path)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)

    manager = CheckpointManager(layout)
    await manager.ensure_initialized()
    (layout.checkpoints_dir / "narrative.md").write_text("# Narrative", encoding="utf-8")
    sha = await manager.create_checkpoint("narrative")
    assert len(sha) == 40

    commits = await manager.list_checkpoints()
    assert commits[0].message == "[narrative] approved"


@pytest.mark.asyncio
async def test_checkpoint_manager_two_gates_two_commits(tmp_path: Path) -> None:
    """
    Given two successive files written to the mirror working tree,
    when create_checkpoint() is called after each write,
    then both commit messages appear in the log.
    """
    from kodo.project._layout import ProjectLayout

    layout = ProjectLayout(tmp_path)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)

    manager = CheckpointManager(layout)
    await manager.ensure_initialized()

    (layout.checkpoints_dir / "narrative.md").write_text("# Narrative", encoding="utf-8")
    await manager.create_checkpoint("narrative")

    (layout.checkpoints_dir / "responsibilities.md").write_text(
        "# Responsibilities", encoding="utf-8"
    )
    await manager.create_checkpoint("responsibilities")

    commits = await manager.list_checkpoints()
    messages = [c.message for c in commits]
    assert "[responsibilities] approved" in messages
    assert "[narrative] approved" in messages
