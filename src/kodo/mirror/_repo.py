"""Git porcelain wrapper for the Kōdo mirror repository.

The mirror lives at ``<project>/.kodo/checkpoints/`` and is a plain git
repository (not a worktree).  :class:`MirrorRepo` wraps the handful of git
operations needed by the checkpoint workflow using
``asyncio.create_subprocess_exec`` so it does not block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CheckpointInfo", "MirrorRepo", "MirrorRepoError"]

_log = logging.getLogger(__name__)


class MirrorRepoError(Exception):
    """Raised when a git subprocess exits non-zero."""


@dataclass(frozen=True)
class CheckpointInfo:
    """Metadata for a single mirror commit.

    Attributes:
        sha: Full commit SHA.
        message: Commit subject line.
        timestamp: ISO-8601 commit timestamp.
    """

    sha: str
    message: str
    timestamp: str


class MirrorRepo:
    """Async git wrapper for the project mirror.

    Args:
        repo_dir: Directory that will contain (or already contains) the
            mirror git repository.
    """

    def __init__(self, repo_dir: Path) -> None:
        self.__repo_dir = repo_dir

    @property
    def repo_dir(self) -> Path:
        """Path to the mirror repository root."""
        return self.__repo_dir

    def is_initialized(self) -> bool:
        """Return ``True`` if the directory already contains a git repo."""
        return (self.__repo_dir / ".git").is_dir()

    async def init(self) -> None:
        """Initialise a new git repository with a single empty commit.

        Raises:
            MirrorRepoError: Any git command fails.
        """
        self.__repo_dir.mkdir(parents=True, exist_ok=True)
        await self.__git("init", "-b", "kodo")
        await self.__git("config", "user.email", "kodo@localhost")
        await self.__git("config", "user.name", "Kodo")
        await self.__git("commit", "--allow-empty", "-m", "init: kodo mirror")
        _log.info("Mirror initialised at %s", self.__repo_dir)

    async def sync_and_commit(
        self,
        src_dir: Path,
        gen_dir: Path,
        message: str,
    ) -> str:
        """Copy ``src/`` and ``gen/`` into the mirror and commit.

        Args:
            src_dir: Project ``src/`` directory to snapshot.
            gen_dir: Project ``gen/`` directory to snapshot.
            message: Commit message.

        Returns:
            str: The new commit SHA.

        Raises:
            MirrorRepoError: Any git command fails.
        """
        mirror_src = self.__repo_dir / "src"
        mirror_gen = self.__repo_dir / "gen"

        if src_dir.exists():
            if mirror_src.exists():
                shutil.rmtree(mirror_src)
            shutil.copytree(src_dir, mirror_src)

        gen_has_files = gen_dir.exists() and any(gen_dir.iterdir())
        if gen_has_files:
            if mirror_gen.exists():
                shutil.rmtree(mirror_gen)
            shutil.copytree(gen_dir, mirror_gen)

        await self.__git("add", "-A")

        # Check whether anything was staged before committing — git exits 1
        # with "nothing to commit" when the index is clean, which is not an
        # error for our use case (no-op checkpoints are still valid).
        diff_proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--cached",
            "--quiet",
            cwd=self.__repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await diff_proc.communicate()
        if diff_proc.returncode == 0:
            # Nothing staged — return current HEAD sha without creating a commit.
            _log.info("Mirror: nothing to commit for %r, reusing HEAD", message)
        else:
            await self.__git("commit", "--allow-empty", "-m", message)

        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=self.__repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        sha = stdout.decode().strip()
        _log.info("Mirror commit %s: %s", sha[:8], message)
        return sha

    async def log(self) -> list[CheckpointInfo]:
        """Return all commits in reverse chronological order (newest first).

        Returns:
            list[CheckpointInfo]: Commit metadata; empty if no commits yet.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "--format=%H|%s|%ci",
            cwd=self.__repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        results: list[CheckpointInfo] = []
        for line in stdout.decode().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:  # noqa: PLR2004
                results.append(
                    CheckpointInfo(sha=parts[0], message=parts[1], timestamp=parts[2])
                )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def __git(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self.__repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise MirrorRepoError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): "
                f"{stderr.decode().strip()}"
            )
        _log.debug("git %s → %s", " ".join(args), stdout.decode().strip()[:80])
