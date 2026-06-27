"""Async shadow-git wrapper over an explicit ``(work_tree, git_dir)`` pair.

Every git invocation runs with ``GIT_DIR``/``GIT_WORK_TREE`` set in the
environment, so the repository metadata lives at ``git_dir`` while the tracked
files are the real ones under ``work_tree`` — no duplicated working copy.  All
calls use ``asyncio.create_subprocess_exec`` so they never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CommitInfo", "ShadowMirror", "ShadowMirrorError"]

_log = logging.getLogger(__name__)

_GIT_USER_NAME = "Kodo"
_GIT_USER_EMAIL = "kodo@localhost"


class ShadowMirrorError(Exception):
    """Raised when a git subprocess exits non-zero."""


@dataclass(frozen=True)
class CommitInfo:
    """Metadata for a single mirror commit.

    Attributes:
        sha: Full commit SHA.
        message: Commit subject line.
        timestamp: ISO-8601 commit timestamp.
    """

    sha: str
    message: str
    timestamp: str


class ShadowMirror:
    """Version a work tree in place via an out-of-tree git directory.

    Args:
        work_tree: The directory whose files are tracked (the real project root).
        git_dir: Where the git metadata lives (e.g. ``<root>/.kodo/checkpoints/.git``).
    """

    def __init__(self, work_tree: Path, git_dir: Path) -> None:
        self.__work_tree = work_tree.resolve()
        self.__git_dir = git_dir.resolve()
        self.__branch: str | None = None

    @property
    def work_tree(self) -> Path:
        """The tracked work-tree root."""
        return self.__work_tree

    @property
    def git_dir(self) -> Path:
        """The out-of-tree git directory."""
        return self.__git_dir

    def is_initialized(self) -> bool:
        """Return ``True`` if the git directory already holds a repository."""
        return (self.__git_dir / "HEAD").exists()

    async def init(self, excludes: tuple[str, ...] = ()) -> None:
        """Initialise the repository and capture a baseline commit.

        Writes *excludes* (gitignore-style patterns) into the repo's
        ``info/exclude`` before the first ``add`` so they never enter history,
        then snapshots the current tree as the root commit. Snapshotting the
        existing files (rather than an empty root) means undoing the first
        tool-call commit restores files to their real pre-Kōdo state.

        Args:
            excludes: Ignore patterns to seed ``info/exclude`` with (in addition
                to the work tree's own ``.gitignore`` files, which git honours).

        Raises:
            ShadowMirrorError: Any git command fails.
        """
        self.__git_dir.parent.mkdir(parents=True, exist_ok=True)
        await self.__git("init")
        await self.__git("config", "user.email", _GIT_USER_EMAIL)
        await self.__git("config", "user.name", _GIT_USER_NAME)
        # Belt-and-braces: the env already sets the work tree, but recording it
        # in config keeps ad-hoc `git --git-dir=...` calls correct too.
        await self.__git("config", "core.worktree", str(self.__work_tree))

        if excludes:
            info_dir = self.__git_dir / "info"
            info_dir.mkdir(parents=True, exist_ok=True)
            (info_dir / "exclude").write_text("\n".join(excludes) + "\n", encoding="utf-8")

        await self.__git("add", "-A")
        await self.__commit_or_allow_empty("init: kodo mirror baseline")
        _log.info(
            "Shadow mirror initialised: git_dir=%s work_tree=%s",
            self.__git_dir,
            self.__work_tree,
        )

    async def commit(self, label: str) -> str:
        """Stage every change and commit, returning the resulting SHA.

        If the index is clean the commit is skipped and the current HEAD is
        returned — no-op checkpoints are valid (e.g. a read-only command that
        slipped past the caller's mutation gate).

        Args:
            label: Commit message.

        Returns:
            str: The commit SHA (new, or existing HEAD on a no-op).
        """
        await self.__git("add", "-A")
        if await self.__index_clean():
            _log.debug("Shadow mirror: nothing to commit for %r", label)
            return await self.head_sha()
        await self.__git("commit", "-m", label)
        return await self.head_sha()

    async def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""
        return (await self.__git("rev-parse", "HEAD")).strip()

    async def paths_changed(self, sha: str) -> list[str]:
        """Return the work-tree-relative paths a commit changed.

        Args:
            sha: Commit SHA.

        Returns:
            list[str]: Paths touched by *sha* (added, modified, or deleted).
        """
        out = await self.__git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)
        return [line for line in out.splitlines() if line]

    async def undo(self, sha: str) -> str:
        """Undo only *sha*: restore the files it touched to their pre-*sha* state.

        Files changed by *sha* are reset to the parent commit (``sha^``),
        discarding *sha*'s effect on them **and any later edits to those same
        files** — exactly the "surgically remove this change" semantics. Other
        files are left as they are. The result is recorded as a new commit
        (append-only), so this is itself undoable.

        Args:
            sha: The commit to undo.

        Returns:
            str: SHA of the new commit recording the undo.
        """
        paths = await self.paths_changed(sha)
        if not paths:
            return await self.head_sha()
        # Restore each touched path to its state at sha's parent. A path that
        # did not exist at sha^ (sha created it) is removed from the work tree.
        parent = f"{sha}^"
        existed = set(await self.__tree_paths(parent))
        to_restore = [p for p in paths if p in existed]
        to_delete = [p for p in paths if p not in existed]
        if to_restore:
            await self.__git("checkout", parent, "--", *to_restore)
        for rel in to_delete:
            (self.__work_tree / rel).unlink(missing_ok=True)
        return await self.commit(f"undo {sha[:8]}")

    async def redo(self, sha: str) -> str:
        """Redo *sha*: restore the files it touched to their state at *sha* itself.

        The inverse of :meth:`undo` — re-applies the file contents *sha*
        introduced (discarding any later edits to those same files). Other
        files are left as they are. The result is recorded as a new commit
        (append-only), so this is itself undoable (via another ``undo``).

        Args:
            sha: The commit to redo.

        Returns:
            str: SHA of the new commit recording the redo.
        """
        paths = await self.paths_changed(sha)
        if not paths:
            return await self.head_sha()
        # Restore each touched path to its state at sha. A path that does not
        # exist at sha (sha deleted it) is removed from the work tree.
        existed = set(await self.__tree_paths(sha))
        to_restore = [p for p in paths if p in existed]
        to_delete = [p for p in paths if p not in existed]
        if to_restore:
            await self.__git("checkout", sha, "--", *to_restore)
        for rel in to_delete:
            (self.__work_tree / rel).unlink(missing_ok=True)
        return await self.commit(f"redo {sha[:8]}")

    async def rollback(self, sha: str) -> str:
        """Move the current branch to point at *sha*, never leaving a detached HEAD.

        Used for both "rollback" (sha behind the tip) and "roll forward" (sha
        ahead, or on a diverged branch) — the direction is purely a caller-side
        bookkeeping concept; the git mechanics are identical and symmetric.

        If the branch's current tip is not an ancestor reachable after the
        move (i.e. it isn't *sha* itself), it is preserved by branching it off
        under ``rollback_<unix-ts>`` *before* the reset, so it stays reachable
        and is never garbage-collected as a dangling commit. We stay on the
        same branch throughout — only ``git reset --hard`` moves where it
        points — so this can never produce a detached HEAD.

        Args:
            sha: The commit the current branch should now point at.

        Returns:
            str: The new HEAD SHA (``sha`` itself; a no-op returns the
            unchanged tip when *sha* is already the tip).
        """
        tip = await self.head_sha()
        if sha == tip:
            return tip
        await self.__create_rollback_branch(tip)
        await self.__git("reset", "--hard", sha)
        return await self.head_sha()

    async def __create_rollback_branch(self, tip: str) -> str:
        """Create a uniquely-named ``rollback_<unix-ts>`` branch at *tip*.

        Two rollbacks within the same wall-clock second would otherwise
        collide on the same name; on an "already exists" error this retries
        with a ``-2``, ``-3``, ... suffix rather than failing the rollback.
        """
        base = f"rollback_{int(time.time())}"
        name = base
        attempt = 1
        while True:
            try:
                await self.create_branch(name, tip)
                return name
            except ShadowMirrorError as exc:
                if "already exists" not in str(exc):
                    raise
                attempt += 1
                name = f"{base}-{attempt}"

    async def is_dirty(self) -> bool:
        """Return ``True`` if the work tree has changes not yet committed here.

        Catches both modified-tracked and untracked files. Used to detect
        edits made to the work tree outside of Kodo (the mirror auto-commits
        after every Kodo-driven mutation, so any leftover diff is external)
        before an undo/redo/rollback would otherwise silently overwrite it.
        """
        out = await self.__git("status", "--porcelain")
        return bool(out.strip())

    async def stash_push(self) -> bool:
        """Stash uncommitted/untracked changes; return ``False`` if nothing to stash."""
        if not await self.is_dirty():
            return False
        await self.__git("stash", "push", "-u", "-m", "kodo: pre-checkpoint-op stash")
        return True

    async def stash_pop(self) -> None:
        """Reapply and drop the most recently pushed stash."""
        await self.__git("stash", "pop")

    async def branch_name(self) -> str:
        """The mirror's current branch name (cached after the first read)."""
        if self.__branch is None:
            self.__branch = (await self.__git("symbolic-ref", "--short", "HEAD")).strip()
        return self.__branch

    async def create_branch(self, name: str, sha: str) -> None:
        """Create branch *name* pointing at *sha* (does not check it out)."""
        await self.__git("branch", name, sha)

    async def log(self) -> list[CommitInfo]:
        """Return all commits newest-first."""
        out = await self.__git("log", "--format=%H|%s|%ci")
        results: list[CommitInfo] = []
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:  # noqa: PLR2004
                results.append(CommitInfo(sha=parts[0], message=parts[1], timestamp=parts[2]))
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def __tree_paths(self, ref: str) -> list[str]:
        """Paths present in *ref*'s tree (empty if the ref does not resolve)."""
        try:
            out = await self.__git("ls-tree", "-r", "--name-only", ref)
        except ShadowMirrorError:
            return []
        return [line for line in out.splitlines() if line]

    async def __index_clean(self) -> bool:
        """Return ``True`` when nothing is staged for commit."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--cached",
            "--quiet",
            cwd=str(self.__work_tree),
            env=self.__env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def __commit_or_allow_empty(self, label: str) -> None:
        if await self.__index_clean():
            await self.__git("commit", "--allow-empty", "-m", label)
        else:
            await self.__git("commit", "-m", label)

    def __env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_DIR"] = str(self.__git_dir)
        env["GIT_WORK_TREE"] = str(self.__work_tree)
        return env

    async def __git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.__work_tree),
            env=self.__env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ShadowMirrorError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}"
            )
        return stdout.decode()
