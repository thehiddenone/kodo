"""Path resolution for the native file-I/O and shell tools.

Two resolvers, picked per agent run by the engine from the active workflow mode
(see the ``project-kodo`` memory, WorkspaceLayout two-root model):

* :class:`ProjectPathResolver` — **Guided** mode.  Relative paths resolve under
  the locked current project's root; the result must stay inside that root —
  except the OS temp directory (``kodo.common.system_temp_roots()``), always
  reachable regardless of mode, and the session's private scratch directory
  (``kodo.project.session_temp_dir``), passed in as ``extra_roots`` by the
  engine (see :func:`resolve_within`).
* :class:`LogicalPathResolver` — **Problem Solver** mode.  Relative paths are
  *logical*: the first segment is a VS Code workspace-folder name that anchors
  the remainder to that folder's real physical path (which may live anywhere on
  disk).  Absolute paths are taken as-is — already unrestricted, so the temp
  directory was already reachable here.

Both expose a :pyattr:`default_cwd` used by ``run_command`` when the agent does
not pass an explicit working directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from kodo.common import system_temp_roots

__all__ = [
    "LogicalPathResolver",
    "PathResolver",
    "ProjectPathResolver",
    "resolve_logical",
    "resolve_within",
]


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
    there are expected agent territory, not a project escape), or under one
    of *extra_roots* (e.g. the session's private scratch directory, see
    :class:`ProjectPathResolver`), or a :class:`PermissionError` is raised
    (path-traversal guard). Symlinks are resolved by ``Path.resolve()``
    before either check, so a symlinked temp dir (macOS's ``/tmp`` ->
    ``/private/tmp``) matches regardless of which spelling *path* uses.

    Args:
        root: The project root every tool path is confined to.
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
            raise PermissionError(
                f"Path {path!r} is outside the project root {str(root)!r}"
            ) from None
    return resolved


def resolve_logical(folders: dict[str, Path], path: str) -> Path:
    """Resolve a *logical* path against the workspace-folder map.

    A relative logical path begins with a workspace-folder name (its first
    segment); that name is looked up in *folders* and the remainder resolves
    beneath the folder's physical path.  Absolute paths are taken as-is so a
    Problem Solver agent can still address anything on the real filesystem.

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


class ProjectPathResolver:
    """Guided-mode resolver: confine every path to one project root.

    ``extra_roots`` additionally admits absolute paths under other specific
    directories — used to let the session's private scratch directory
    (``kodo.project.session_temp_dir``, reported by ``get_root_paths`` with
    ``temporary: true``) through as a ``run_command`` working directory even
    though it lives outside the project root.
    """

    def __init__(self, root: Path, *, extra_roots: tuple[Path, ...] = ()) -> None:
        self.__root = root.resolve()
        self.__extra_roots = tuple(r.resolve() for r in extra_roots)

    def resolve(self, path: str) -> Path:
        return resolve_within(self.__root, path, extra_roots=self.__extra_roots)

    @property
    def default_cwd(self) -> Path:
        return self.__root


class LogicalPathResolver:
    """Problem-Solver-mode resolver: address every workspace folder by name."""

    def __init__(self, folders: dict[str, Path], physical_root: Path) -> None:
        self.__folders = {name: Path(p).resolve() for name, p in folders.items()}
        self.__physical_root = physical_root.resolve()

    def resolve(self, path: str) -> Path:
        return resolve_logical(self.__folders, path)

    @property
    def default_cwd(self) -> Path:
        return self.__physical_root
