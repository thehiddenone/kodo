"""Per-root checkpoint coordination over the generic shadow-git mirror.

Bridges the low-level :class:`kodo.mirror.ShadowMirror` (which knows only
``(work_tree, git_dir)`` paths) to Kōdo's conventions: every root the agent may
touch gets its own independent mirror at ``<root>/.kodo/checkpoints``, created
**lazily** the first time a file-mutating tool writes under that root, at which
point ``<root>/.kodo/`` and a ``kodo.md`` marker are scaffolded.

Also hosts :func:`command_may_mutate` — the caller-side mutation heuristic over
a :class:`kodo.shellparser.ParsedCommand`. The parser stays judgement-free; this
is where ``run_command`` decides whether a checkpoint is worth attempting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kodo.mirror import ShadowMirror
from kodo.project import ProjectLayout
from kodo.shellparser import ParsedCommand

__all__ = ["CheckpointRef", "RootMirrorManager", "command_may_mutate"]

_log = logging.getLogger(__name__)

# Patterns seeded into each mirror's git ``info/exclude``. The work tree's own
# ``.gitignore`` files are honoured by git on top of these; this list keeps
# heavyweight, derived, or Kōdo-internal trees out of checkpoints regardless of
# whether the project has a .gitignore.
_KODO_EXCLUDES: tuple[str, ...] = (
    ".kodo/",
    ".git/",
    "node_modules/",
    ".venv/",
    "venv/",
    "env/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".DS_Store",
)

# Output redirections write a file; input ones (`<`, `<<`, `<<<`) do not.
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})

# Commands known to be read-only. Anything not listed is treated as possibly
# mutating (default-to-True): a needless commit is just a no-op, while a missed
# one would lose a checkpoint. Deliberately conservative — e.g. `sed` is omitted
# because `sed -i` mutates, so any `sed` errs toward a (harmless) checkpoint.
_READONLY_COMMANDS = frozenset(
    {
        "echo",
        "printf",
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "find",
        "fd",
        "which",
        "whoami",
        "id",
        "date",
        "env",
        "printenv",
        "uname",
        "hostname",
        "true",
        "false",
        "test",
        "[",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "stat",
        "file",
        "du",
        "df",
        "ps",
        "tree",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "cut",
        "column",
        "tac",
        "nl",
        "seq",
        "expr",
        "sleep",
        "yes",
    }
)


def command_may_mutate(parsed: ParsedCommand) -> bool:
    """Heuristic: could this command modify the filesystem?

    Returns ``True`` for any output redirection or any executable not on the
    read-only allow-list (default-to-mutating when uncertain). Used only to skip
    pointless ``git add -A`` sweeps after plainly read-only commands; git's own
    change detection remains the source of truth, so a false negative at worst
    folds the change into the next mutating call's checkpoint.

    Args:
        parsed: The structural parse of the command line.

    Returns:
        bool: ``True`` if the command might have written to disk.
    """
    if any(r.operator in _OUTPUT_REDIRECTS for r in parsed.redirections):
        return True
    execs = parsed.executables
    if not execs:
        return False
    return any(_command_name(exe) not in _READONLY_COMMANDS for exe in execs)


def _command_name(executable: str) -> str:
    """The leaf name of an executable token (``/usr/bin/rm`` → ``rm``)."""
    return executable.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class CheckpointRef:
    """A checkpoint commit the UI can act on.

    Attributes:
        root: Absolute path of the mirror's root (which ``.kodo`` it belongs to).
        sha: The commit recording this tool call.
        parent: The commit immediately before it (HEAD prior to the call).
    """

    root: str
    sha: str
    parent: str


class RootMirrorManager:
    """Owns one :class:`ShadowMirror` per touched root, created on demand.

    Args:
        roots: The roots the session may operate within (from ``get_root_paths``).
            Used to map a mutated path to its enclosing root; extra roots can be
            added later via :meth:`set_roots`.
    """

    def __init__(self, roots: Sequence[Path] = ()) -> None:
        self.__roots: list[Path] = []
        self.__mirrors: dict[Path, ShadowMirror] = {}
        self.__lock = asyncio.Lock()
        self.set_roots(roots)

    def set_roots(self, roots: Sequence[Path]) -> None:
        """Replace the known-roots set (keeps already-created mirrors)."""
        self.__roots = sorted({Path(r).resolve() for r in roots})

    async def prepare(self, path: Path) -> None:
        """Initialise the mirror of *path*'s root **before** a tool mutates it.

        Must be called pre-dispatch so the mirror's baseline captures the tree as
        it is *before* the change; the matching :meth:`commit_for_path` afterward
        then records the change as its own commit (rather than absorbing the
        first-ever change into the baseline snapshot). No-op when *path* is
        outside every known root.

        Args:
            path: A path the tool is about to write (need not exist yet).
        """
        root = self._root_for(path)
        if root is None:
            return
        async with self.__lock:
            await self._ensure(root)

    async def commit_for_path(self, path: Path, label: str) -> CheckpointRef | None:
        """Checkpoint the mirror of the root enclosing *path*.

        Lazily scaffolds the root's ``.kodo`` + mirror on first touch. Returns
        ``None`` when *path* lies outside every known root, or when the commit
        was a no-op (nothing actually changed — no checkpoint to surface).

        Args:
            path: The file the tool wrote (resolved absolute path).
            label: Commit message.

        Returns:
            CheckpointRef | None: The checkpoint, or ``None``.
        """
        root = self._root_for(path)
        if root is None:
            return None
        async with self.__lock:
            mirror = await self._ensure(root)
            parent = await mirror.head_sha()
            sha = await mirror.commit(label)
        if sha == parent:
            return None
        return CheckpointRef(root=str(root), sha=sha, parent=parent)

    async def sweep_initialized(self, label: str) -> None:
        """Commit every already-created mirror (no-op when clean).

        Catches writes a command made outside the cwd's root (any root that
        already has a mirror). Roots never touched before stay untracked — the
        lazy-creation contract.

        Args:
            label: Commit message for any non-empty sweep.
        """
        async with self.__lock:
            for mirror in self.__mirrors.values():
                await mirror.commit(label)

    async def undo(self, root: str, sha: str) -> str:
        """Undo only *sha* in *root*'s mirror; return the new commit SHA."""
        async with self.__lock:
            mirror = await self._ensure(Path(root))
            return await mirror.undo(sha)

    async def rollback(self, root: str, sha: str) -> str:
        """Restore *root*'s tree to *sha*; return the new commit SHA."""
        async with self.__lock:
            mirror = await self._ensure(Path(root))
            return await mirror.rollback(sha)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _root_for(self, path: Path) -> Path | None:
        """The longest known root that contains *path* (or ``None``)."""
        resolved = path.resolve()
        best: Path | None = None
        for root in self.__roots:
            contains = resolved == root or root in resolved.parents
            if contains and (best is None or len(str(root)) > len(str(best))):
                best = root
        return best

    async def _ensure(self, root: Path) -> ShadowMirror:
        """Return *root*'s mirror, scaffolding ``.kodo`` + initialising on first use."""
        root = root.resolve()
        cached = self.__mirrors.get(root)
        if cached is not None:
            return cached
        layout = ProjectLayout(root)
        git_dir = layout.checkpoints_dir / ".git"
        mirror = ShadowMirror(root, git_dir)
        if not mirror.is_initialized():
            await asyncio.to_thread(layout.scaffold_kodo_dir)
            await mirror.init(_KODO_EXCLUDES)
            _log.info("Checkpoint mirror created for root %s", root)
        self.__mirrors[root] = mirror
        return mirror
