"""Behavior tests for kodo.mirror._repo and ._checkpoints."""

from __future__ import annotations

import json
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
async def test_mirror_sync_and_commit_copies_files(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    src = tmp_path / "src"
    src.mkdir()
    (src / "narrative.kd").write_text("# Narrative\nHello world.", encoding="utf-8")
    gen = tmp_path / "gen"
    gen.mkdir()

    sha = await repo.sync_and_commit(src, gen, "[narrative] approved")
    assert len(sha) == 40
    expected = "# Narrative\nHello world."
    assert (repo_dir / "src" / "narrative.kd").read_text(encoding="utf-8") == expected


@pytest.mark.asyncio
async def test_mirror_log_returns_newest_first(tmp_path: Path) -> None:
    repo_dir = tmp_path / "checkpoints"
    repo = MirrorRepo(repo_dir)
    await repo.init()

    src = tmp_path / "src"
    src.mkdir()
    gen = tmp_path / "gen"
    gen.mkdir()
    (src / "a.kd").write_text("a", encoding="utf-8")
    await repo.sync_and_commit(src, gen, "first checkpoint")
    (src / "b.kd").write_text("b", encoding="utf-8")
    await repo.sync_and_commit(src, gen, "second checkpoint")

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
    from kodo.project._layout import ProjectLayout

    layout = ProjectLayout(tmp_path)
    layout.src_dir.mkdir(parents=True, exist_ok=True)
    layout.gen_dir.mkdir(parents=True, exist_ok=True)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)
    (layout.src_dir / "narrative.kd").write_text("# Narrative", encoding="utf-8")

    manager = CheckpointManager(layout)
    await manager.ensure_initialized()
    sha = await manager.create_checkpoint("narrative")
    assert len(sha) == 40

    commits = await manager.list_checkpoints()
    latest = commits[0]
    assert latest.message == "[narrative] approved"


@pytest.mark.asyncio
async def test_checkpoint_manager_two_gates_two_commits(tmp_path: Path) -> None:
    from kodo.project._layout import ProjectLayout

    layout = ProjectLayout(tmp_path)
    layout.src_dir.mkdir(parents=True, exist_ok=True)
    layout.gen_dir.mkdir(parents=True, exist_ok=True)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)

    manager = CheckpointManager(layout)
    await manager.ensure_initialized()

    (layout.src_dir / "narrative.kd").write_text("# Narrative", encoding="utf-8")
    await manager.create_checkpoint("narrative")

    (layout.src_dir / "responsibilities.kd").write_text("# Responsibilities", encoding="utf-8")
    dag = {"components": [{"name": "core", "description": "Core", "depends_on": []}]}
    (layout.src_dir / "responsibilities.dag.json").write_text(json.dumps(dag), encoding="utf-8")
    await manager.create_checkpoint("responsibilities")

    commits = await manager.list_checkpoints()
    messages = [c.message for c in commits]
    assert "[responsibilities] approved" in messages
    assert "[narrative] approved" in messages
