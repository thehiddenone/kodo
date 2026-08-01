"""Path resolution for the native file-I/O and shell tools.

One resolver, shared by both workflow modes: :class:`LogicalPathResolver`.
Relative paths are *logical*: the first segment is a bound root's name — a
VS Code workspace-folder name, or a project created via
``scaffold_new_project`` — that anchors the remainder to that
root's real physical path (which may live anywhere on disk). Absolute paths
are taken as-is, already unrestricted, so the OS temp directory and a
session's private scratch directory (``kodo.project.session_temp_dir``) are
reachable with no special-casing.

Exposes a :pyattr:`~LogicalPathResolver.default_cwd` used by ``run_command``
when the agent does not pass an explicit working directory, and
:func:`root_for`, the "which bound root does this resolved path belong to"
lookup shared by every ``kodo.guided_state`` caller now that a session may
have more than one bound root.

Also exposes :func:`resolve_within`, a standalone containment resolver used
by :meth:`~kodo.tools.Tool.resolve_path`'s ``temporary=True`` branch to
confine a path under a session's private scratch directory
(``kodo.project.session_temp_dir``) — unrelated to which workflow-mode
resolver is active, since scratch-directory access is mode-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kodo.common import system_temp_roots
from kodo.project import SessionWorkspace

if TYPE_CHECKING:
    from ._context import RootPath

__all__ = [
    "LogicalPathResolver",
    "NoWorkspaceError",
    "PathResolver",
    "resolve_logical",
    "resolve_within",
    "root_for",
]


class NoWorkspaceError(Exception):
    """Raised when :attr:`LogicalPathResolver.default_cwd` is read with no
    workspace/project bound.

    A caller that cannot guard with ``has_workspace`` first (or that wants a
    single try/except instead of a pre-check) can catch this directly.
    """


def _within_roots(resolved: Path, roots: tuple[Path, ...]) -> bool:
    """Whether *resolved* sits at or below one of *roots*."""
    return any(resolved == root or root in resolved.parents for root in roots)


def _within_system_temp(resolved: Path) -> bool:
    """Whether *resolved* sits at or below one of ``system_temp_roots()``."""
    return _within_roots(resolved, tuple(Path(root) for root in system_temp_roots()))


def resolve_within(root: Path, path: str, *, extra_roots: tuple[Path, ...] = ()) -> Path:
    """Resolve *path* against *root*, rejecting anything outside it.

    Relative paths are resolved against *root*; absolute paths are taken
    as-is.  Either way the result must live inside *root*, under the OS
    temp directory (``kodo.common.system_temp_roots()`` — scratch files
    there are expected agent territory, not an escape), or under one of
    *extra_roots*, or a :class:`PermissionError` is raised (path-traversal
    guard). Symlinks are resolved by ``Path.resolve()`` before either check,
    so a symlinked temp dir (macOS's ``/tmp`` -> ``/private/tmp``) matches
    regardless of which spelling *path* uses.

    Args:
        root: The directory every path is confined to.
        path: User/agent-supplied path (relative or absolute).
        extra_roots: Additional resolved roots an absolute *path* may also
            live under.

    Returns:
        Path: The resolved, in-bounds absolute path.

    Raises:
        PermissionError: If the resolved path escapes *root*, the OS temp
            directory, and every entry in *extra_roots*.
    """
    candidate = Path(path)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        if not _within_system_temp(resolved) and not _within_roots(resolved, extra_roots):
            raise PermissionError(f"Path {path!r} is outside {str(root)!r}") from None
    return resolved


def resolve_logical(folders: dict[str, Path], path: str) -> Path:
    """Resolve a *logical* path against the workspace-folder map.

    A relative logical path begins with a workspace-folder name (its first
    segment); that name is looked up in *folders* and the remainder resolves
    beneath the folder's physical path.  Absolute paths are taken as-is so an
    agent can still address anything on the real filesystem.

    Args:
        folders: Logical name → physical path of every open workspace folder.
        path: Agent-supplied path (logical-relative or absolute).

    Returns:
        Path: The resolved absolute path.

    Raises:
        PermissionError: The path is empty or its first segment is not a known
            workspace-folder name.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    parts = candidate.parts
    if not parts:
        raise PermissionError("Empty path")
    name = parts[0]
    base = folders.get(name)
    if base is None:
        known = ", ".join(sorted(folders)) or "(none)"
        raise PermissionError(
            f"Path {path!r} must start with a workspace-folder name; known folders: {known}"
        )
    rest = Path(*parts[1:]) if len(parts) > 1 else Path()
    return (base / rest).resolve()


def root_for(roots: tuple[RootPath, ...], path: Path) -> RootPath | None:
    """The longest bound root that contains *path*, or ``None``.

    Used by every ``kodo.guided_state`` caller (``guided_dev_status``,
    ``record_guided_revision``, the engine's critic-verdict recording, and
    document finalization) to recover which of a session's N bound
    roots an already-resolved absolute path falls under — the replacement for
    the single implicit project root each of those used to read straight off
    the old singular binding. *path* must already be resolved (as every
    caller's is, via :class:`LogicalPathResolver`); this performs no
    filesystem I/O itself beyond resolving each candidate root once.

    Mirrors :meth:`~kodo.runtime._checkpoints.RootMirrorManager._root_for`'s
    longest-prefix-match algorithm, but keyed on :class:`RootPath` (so callers
    keep the root's ``name``, not just its path) rather than a bare
    ``list[Path]`` — left as a separate, un-shared implementation since the
    two operate on different input shapes and neither is a subset of the
    other's callers.
    """
    best: RootPath | None = None
    best_resolved: Path | None = None
    for root in roots:
        resolved_root = Path(root.path).resolve()
        contains = path == resolved_root or resolved_root in path.parents
        more_specific = best_resolved is None or len(str(resolved_root)) > len(str(best_resolved))
        if contains and more_specific:
            best = root
            best_resolved = resolved_root
    return best


@runtime_checkable
class PathResolver(Protocol):
    """Resolves an agent-supplied path and supplies a default working directory."""

    def resolve(self, path: str) -> Path:
        """Resolve *path* to an absolute filesystem path."""
        ...

    @property
    def default_cwd(self) -> Path:
        """Working directory used when ``run_command`` omits an explicit cwd."""
        ...


class LogicalPathResolver:
    """Address every bound root by name — shared by both workflow modes.

    Holds the live :class:`~kodo.project.SessionWorkspace` itself rather than
    a snapshot of its folder map: ``SessionWorkspace.folders`` reads the
    engine's current state on every access (updated in-process the instant
    ``scaffold_new_project`` scaffolds a directory, and again
    whenever the extension pushes a real ``workspace.folders`` change — e.g.
    the user adding a folder to the VS Code window by hand). Resolving a
    logical path against a resolver built earlier in the same turn therefore
    still sees a project bound moments ago, with no re-construction needed.
    """

    def __init__(self, workspace: SessionWorkspace) -> None:
        self.__workspace = workspace

    def resolve(self, path: str) -> Path:
        return resolve_logical(self.__workspace.folders, path)

    @property
    def default_cwd(self) -> Path:
        """The workspace's physical root.

        Callers should guard this with :meth:`~kodo.runtime._engine._core.
        EngineCore._has_workspace` (or the equivalent ``ToolContext.
        has_workspace``) first — unlike ``root_paths``, this has no empty
        state to fall back to. The ``requires_project`` dispatch gate already
        refuses every ``requires_project`` tool with no workspace bound, but
        a *non*-``requires_project`` tool (``run_command``,
        ``scaffold_new_project``, ``ask_user``, ...) can legitimately dispatch
        with no workspace at all.

        Rather than have each of those guard by hand, prefer
        :attr:`kodo.tools.ToolContext.command_cwd`: it returns this property
        when a workspace is bound and the session's private scratch directory
        when none is, which is what both ``run_command``'s handler and
        :class:`~kodo.tools.ToolDispatcher`'s security gate use (they must
        agree on one directory — see doc/SECURITY.md §3.1a).

        Raises:
            NoWorkspaceError: No workspace/project is bound yet. A caller
                that cannot pre-check ``has_workspace`` (e.g. because it
                shares a code path with callers that pass an explicit
                ``working_dir`` instead) should catch this rather than let it
                propagate — a silent ``None`` here would otherwise flow into
                a real subprocess cwd or security-rule path matching as the
                string ``"None"``.
        """
        root = self.__workspace.physical_root
        if root is None:
            raise NoWorkspaceError("default_cwd read before a workspace/project exists")
        return root
