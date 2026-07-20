"""Smart-mode heuristic for the create_file/edit_file review gate
(WS_PROTOCOL.md §6.5b, ``ToolDispatcher.__edit_review_gate``).

Kept as one small, standalone, pure function — no state, no collaborators —
so the policy is obviously swappable later without touching dispatch logic.
Not under ``kodo.security``: ``kodo.tools`` never imports that package (see
[[feedback-tools-layer]]), and this heuristic needs no Protocol indirection.
"""

from __future__ import annotations

from pathlib import Path

from ._context import RootPath

__all__ = ["should_review_edit"]


def should_review_edit(resolved_path: Path, root_paths: tuple[RootPath, ...]) -> bool:
    """Whether Edit Control ``"smart"`` should pause for review on this path.

    True when ``src`` appears as a whole path segment of *resolved_path*,
    counted relative to whichever *root_paths* entry contains it (so
    ``mysrc/foo.py`` does NOT match, and a checkout that happens to sit
    inside a directory coincidentally named ``src`` above the project root
    does not false-positive on every file). Falls back to matching against
    the full absolute path if no root contains it — shouldn't normally
    happen, since the caller resolves the path through one of these roots.

    Swap this function's body to change the policy.
    """
    relative = resolved_path
    for root in root_paths:
        try:
            relative = resolved_path.relative_to(root.path)
        except ValueError:
            continue
        else:
            break
    return "src" in relative.parts
