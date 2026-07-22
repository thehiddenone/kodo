"""Per-root checkpoint coordination over the generic shadow-git mirror.

Bridges the low-level :class:`kodo.mirror.ShadowMirror` (which knows only
``(work_tree, git_dir)`` paths) to Kōdo's conventions: every root the agent may
touch gets its own independent mirror at ``<root>/.kodo/checkpoints``, created
**lazily** the first time a file-mutating tool writes under that root, at which
point ``<root>/.kodo/`` and a ``kodo.md`` marker are scaffolded.

Also hosts :func:`command_may_mutate` — the caller-side mutation heuristic over
a :class:`kodo.shellparser.ParsedCommand`. The parser stays judgement-free; this
is where ``run_command`` decides whether a checkpoint is worth attempting.

Each root additionally gets a small persisted :class:`CheckpointState` —
``<root>/.kodo/checkpoints/state.json``, a sibling of the mirror's own
``.git`` and never itself tracked — recording the flat, append-only,
chronological list of checkpoints plus a ``current_index`` pointer. This is
what lets the UI distinguish "undo this change" from "re-do this change"
(per-entry ``undone`` flag) and "Rollback to this state" from "Roll forward
to this state" (whether an entry sits behind or ahead of ``current_index``).
See :class:`CheckpointState` for the exact semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kodo.mirror import ShadowMirror
from kodo.project import ProjectLayout
from kodo.shellparser import ParsedCommand

__all__ = [
    "CheckpointEntry",
    "CheckpointRef",
    "CheckpointState",
    "MirrorDirtyError",
    "RootMirrorManager",
    "UnsafeCheckpointRootError",
    "command_may_mutate",
]

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


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string, for :class:`CheckpointEntry.ts`."""
    return datetime.now(UTC).isoformat()


def _is_unsafe_checkpoint_root(root: Path) -> bool:
    """Whether *root* is too broad ever to be a shadow-git mirror target.

    Belt-and-braces: independent of how a root was resolved (a session with
    no workspace, a race, a future bug), a checkpoint mirror must never be
    rooted at the user's entire home directory or a filesystem root — either
    would make ``git add -A`` sweep arbitrary user data. ``root`` is expected
    already-resolved (see :meth:`RootMirrorManager._ensure`).
    """
    return root == Path.home() or root.parent == root


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


class MirrorDirtyError(Exception):
    """Raised when an op would silently discard uncommitted/untracked edits.

    Edits made to the work tree outside of Kodo (the mirror auto-commits
    after every Kodo-driven mutation, so any leftover diff is external) are
    never overwritten without the caller explicitly choosing a *resolution*
    (``"stash"`` or ``"discard"``) and retrying.
    """


class UnsafeCheckpointRootError(Exception):
    """Raised instead of ever mirroring a root at ``$HOME`` or a filesystem root.

    A hard failure, not a silent skip or a fallback: reaching
    :meth:`RootMirrorManager._ensure` with such a root means some caller
    upstream resolved (or defaulted) to an unreasonably broad directory —
    never a legitimate project root, and never something ``git add -A``
    should ever run against.
    """


@dataclass
class CheckpointEntry:
    """One entry in a root's flat, chronological checkpoint history.

    Attributes:
        sha: The commit this entry records.
        parent: The commit immediately before it.
        label: Human-readable description (tool-call label, or "undo <sha>"/
            "redo <sha>").
        kind: ``"tool_call"``, ``"undo"``, or ``"redo"``.
        undone: Only meaningful on a ``"tool_call"`` entry — flips ``True``
            when a later ``undo`` targets this entry's ``sha``, and back to
            ``False`` on a matching ``redo``. Drives the UI's
            undo/re-do label toggle for *this* entry (it is not itself a
            separate visible button).
        ts: ISO-8601 timestamp of when this entry was recorded.
    """

    sha: str
    parent: str
    label: str
    kind: str
    undone: bool = False
    ts: str = ""

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict."""
        return {
            "sha": self.sha,
            "parent": self.parent,
            "label": self.label,
            "kind": self.kind,
            "undone": self.undone,
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> CheckpointEntry:
        """Deserialize from a dict produced by :meth:`to_json`."""
        return cls(
            sha=str(data.get("sha", "")),
            parent=str(data.get("parent", "")),
            label=str(data.get("label", "")),
            kind=str(data.get("kind", "tool_call")),
            undone=bool(data.get("undone", False)),
            ts=str(data.get("ts", "")),
        )


@dataclass
class CheckpointState:
    """A root's persisted checkpoint history: a flat list + a current pointer.

    The list is a UI bookkeeping convenience, not a literal walk of git
    ancestry: entries are always appended in creation order and
    ``current_index`` always advances to the newest entry when new work
    happens — even right after a rollback, when older "future" entries are
    still sitting later in the list. Those entries now describe an abandoned
    branch (preserved in git via a ``rollback_<ts>`` branch — see
    :meth:`kodo.mirror.ShadowMirror.rollback`) but stay visible in the UI
    forever as "Roll forward to this state", exactly mirroring how git itself
    now has a diverged branch sitting beside the current one.

    Attributes:
        entries: Chronological list of every checkpoint ever recorded.
        current_index: Index into ``entries`` of the checkpoint the work tree
            currently reflects (``-1`` for a brand-new, empty history).
    """

    entries: list[CheckpointEntry] = field(default_factory=list)
    current_index: int = -1

    def index_of(self, sha: str) -> int | None:
        """The list index of the entry recording *sha*, or ``None``."""
        for i, entry in enumerate(self.entries):
            if entry.sha == sha:
                return i
        return None

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict."""
        return {
            "current_index": self.current_index,
            "entries": [e.to_json() for e in self.entries],
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> CheckpointState:
        """Deserialize from a dict produced by :meth:`to_json`."""
        raw_entries = data.get("entries")
        entries = (
            [CheckpointEntry.from_json(e) for e in raw_entries if isinstance(e, dict)]
            if isinstance(raw_entries, list)
            else []
        )
        current_index = data.get("current_index", len(entries) - 1)
        valid_index = int(current_index) if isinstance(current_index, int) else len(entries) - 1
        return cls(entries=entries, current_index=valid_index)


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
        self.__states: dict[Path, CheckpointState] = {}
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
            await self._record(root, sha, parent, label, kind="tool_call")
        return CheckpointRef(root=str(root), sha=sha, parent=parent)

    async def sweep_initialized(self, label: str) -> list[Path]:
        """Commit every already-created mirror (no-op when clean).

        Catches writes a command made outside the cwd's root (any root that
        already has a mirror). Roots never touched before stay untracked — the
        lazy-creation contract.

        Args:
            label: Commit message for any non-empty sweep.

        Returns:
            list[Path]: The roots that actually got a non-empty commit —
            callers use this to lock those folders in (see
            :meth:`~kodo.runtime._engine._checkpointing.CheckpointCoordinator.commit`).
        """
        committed: list[Path] = []
        async with self.__lock:
            for root, mirror in self.__mirrors.items():
                parent = await mirror.head_sha()
                sha = await mirror.commit(label)
                if sha != parent:
                    await self._record(root, sha, parent, label, kind="tool_call")
                    committed.append(root)
        return committed

    async def undo(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Undo checkpoint *sha* in *root*'s mirror; return the updated state.

        Restores only the files *sha* touched to their pre-*sha* state
        (discarding later edits to those same files) as a new forward commit
        — see :meth:`kodo.mirror.ShadowMirror.undo` — and flips that entry's
        ``undone`` flag so the UI offers "re-do" next.

        Args:
            root: The mirror's root.
            sha: The checkpoint to undo.
            resolution: ``None`` on the first attempt; ``"stash"`` or
                ``"discard"`` once the caller has resolved a prior
                :class:`MirrorDirtyError`.

        Raises:
            MirrorDirtyError: The work tree has edits Kodo didn't make and
                *resolution* wasn't given.
        """
        root_path = Path(root)
        async with self.__lock:
            mirror = await self._ensure(root_path)
            stashed = await self._resolve_dirty(mirror, resolution)
            parent = await mirror.head_sha()
            new_sha = await mirror.undo(sha)
            if stashed:
                await mirror.stash_pop()
            state = await self._state(root_path)
            if new_sha != parent:
                self._mark_undone(state, sha, undone=True)
                state.entries.append(
                    CheckpointEntry(
                        sha=new_sha,
                        parent=parent,
                        label=f"undo {sha[:8]}",
                        kind="undo",
                        ts=_now_iso(),
                    )
                )
                state.current_index = len(state.entries) - 1
                await self._persist(root_path, state)
            return state

    async def redo(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Redo checkpoint *sha* in *root*'s mirror; return the updated state.

        The inverse of :meth:`undo` — re-applies the files *sha* touched to
        their state at *sha* itself (see :meth:`kodo.mirror.ShadowMirror.redo`),
        and flips that entry's ``undone`` flag back off.

        Args:
            root: The mirror's root.
            sha: The checkpoint to redo.
            resolution: See :meth:`undo`.

        Raises:
            MirrorDirtyError: See :meth:`undo`.
        """
        root_path = Path(root)
        async with self.__lock:
            mirror = await self._ensure(root_path)
            stashed = await self._resolve_dirty(mirror, resolution)
            parent = await mirror.head_sha()
            new_sha = await mirror.redo(sha)
            if stashed:
                await mirror.stash_pop()
            state = await self._state(root_path)
            if new_sha != parent:
                self._mark_undone(state, sha, undone=False)
                state.entries.append(
                    CheckpointEntry(
                        sha=new_sha,
                        parent=parent,
                        label=f"redo {sha[:8]}",
                        kind="redo",
                        ts=_now_iso(),
                    )
                )
                state.current_index = len(state.entries) - 1
                await self._persist(root_path, state)
            return state

    async def rollback(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Move *root*'s current branch to *sha*; return the updated state.

        Covers both "rollback" (sha behind the tip) and "roll forward" (sha
        ahead, or on a diverged branch left behind by an earlier rollback) —
        see :meth:`roll_forward`, which is the very same operation under a
        caller-facing name. The git mechanics are identical and symmetric
        (:meth:`kodo.mirror.ShadowMirror.rollback`); only ``current_index``
        differs by direction, and that's resolved automatically from *sha*'s
        position in the persisted list.

        Args:
            root: The mirror's root.
            sha: The checkpoint the work tree should now reflect.
            resolution: See :meth:`undo`.

        Raises:
            MirrorDirtyError: See :meth:`undo`.
        """
        root_path = Path(root)
        async with self.__lock:
            mirror = await self._ensure(root_path)
            stashed = await self._resolve_dirty(mirror, resolution)
            await mirror.rollback(sha)
            if stashed:
                await mirror.stash_pop()
            state = await self._state(root_path)
            index = state.index_of(sha)
            if index is not None:
                state.current_index = index
                await self._persist(root_path, state)
            return state

    async def roll_forward(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Move *root*'s current branch forward to *sha*. See :meth:`rollback`."""
        return await self.rollback(root, sha, resolution)

    async def state_for(self, root: str) -> CheckpointState:
        """The persisted :class:`CheckpointState` for *root* (hydration path)."""
        async with self.__lock:
            return await self._state(Path(root))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _resolve_dirty(self, mirror: ShadowMirror, resolution: str | None) -> bool:
        """Apply *resolution* to a dirty work tree; return whether it was stashed.

        Raises:
            MirrorDirtyError: The tree is dirty and *resolution* is ``None``.
            ValueError: *resolution* is neither ``None``, ``"stash"`` nor
                ``"discard"``.
        """
        if not await mirror.is_dirty():
            return False
        if resolution is None:
            raise MirrorDirtyError
        if resolution == "discard":
            return False
        if resolution == "stash":
            return await mirror.stash_push()
        raise ValueError(f"Unknown checkpoint dirty-tree resolution: {resolution!r}")

    @staticmethod
    def _mark_undone(state: CheckpointState, sha: str, *, undone: bool) -> None:
        index = state.index_of(sha)
        if index is not None:
            state.entries[index].undone = undone

    async def _record(self, root: Path, sha: str, parent: str, label: str, *, kind: str) -> None:
        """Append a new entry to *root*'s state and advance ``current_index``."""
        state = await self._state(root)
        entry = CheckpointEntry(sha=sha, parent=parent, label=label, kind=kind, ts=_now_iso())
        state.entries.append(entry)
        state.current_index = len(state.entries) - 1
        await self._persist(root, state)

    async def _state(self, root: Path) -> CheckpointState:
        """The cached/loaded :class:`CheckpointState` for *root*."""
        root = root.resolve()
        cached = self.__states.get(root)
        if cached is not None:
            return cached
        state = await asyncio.to_thread(self._load_state_sync, root)
        self.__states[root] = state
        return state

    async def _persist(self, root: Path, state: CheckpointState) -> None:
        root = root.resolve()
        self.__states[root] = state
        await asyncio.to_thread(self._save_state_sync, root, state)

    @staticmethod
    def _state_path(root: Path) -> Path:
        return ProjectLayout(root).checkpoints_dir / "state.json"

    @classmethod
    def _load_state_sync(cls, root: Path) -> CheckpointState:
        path = cls._state_path(root)
        if not path.exists():
            return CheckpointState()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _log.warning("Checkpoint state at %s is unreadable — starting fresh", path)
            return CheckpointState()
        return CheckpointState.from_json(data)

    @classmethod
    def _save_state_sync(cls, root: Path, state: CheckpointState) -> None:
        path = cls._state_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
        tmp.replace(path)

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
        """Return *root*'s mirror, scaffolding ``.kodo`` + initialising on first use.

        Raises:
            UnsafeCheckpointRootError: *root* is ``$HOME`` or a filesystem
                root — refused before ever touching git, regardless of how
                it got here.
        """
        root = root.resolve()
        if _is_unsafe_checkpoint_root(root):
            raise UnsafeCheckpointRootError(
                f"Refusing to create a checkpoint mirror rooted at {root!s} — this is "
                "the user's home directory or a filesystem root, never a legitimate "
                "project root."
            )
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
