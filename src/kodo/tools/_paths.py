"""Project-root path guard shared by the native file-I/O and shell tools."""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_within"]


def resolve_within(root: Path, path: str) -> Path:
    """Resolve *path* against *root*, rejecting anything outside it.

    Relative paths are resolved against *root*; absolute paths are taken
    as-is.  Either way the result must live inside *root* or a
    :class:`PermissionError` is raised (path-traversal guard).

    Args:
        root: The project root every tool path is confined to.
        path: User/agent-supplied path (relative or absolute).

    Returns:
        Path: The resolved, in-bounds absolute path.

    Raises:
        PermissionError: If the resolved path escapes *root*.
    """
    candidate = Path(path)
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(f"Path {path!r} is outside the project root {str(root)!r}") from None
    return resolved
