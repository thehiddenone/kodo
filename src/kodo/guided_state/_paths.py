"""Maps a real project file to its ``.jsonl`` evolution-log path.

Convention: ``<root>/specs/foo/bar.md`` -> ``<root>/.kodo/guided_dev_state/specs/foo/bar.md.jsonl``
(``src/``, ``test/`` analogously). A path whose first segment under the
project root is not one of ``specs``, ``src``, ``test`` is untracked — no
evolution log applies.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["is_tracked", "shadow_path"]

_TRACKED_ROOTS = ("specs", "src", "test")


def shadow_path(real_path: Path, project_root: Path) -> Path | None:
    """The ``.jsonl`` evolution-log path for *real_path*, or ``None`` if untracked.

    Args:
        real_path: Absolute or relative path to the real file.
        project_root: The bound project's root.

    Returns:
        Path | None: The shadow ``.jsonl`` path, or ``None`` when *real_path*
            doesn't resolve under ``specs/``, ``src/``, or ``test/`` beneath
            *project_root*.
    """
    root = project_root.resolve()
    try:
        rel = real_path.resolve().relative_to(root)
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] not in _TRACKED_ROOTS:
        return None
    return root / ".kodo" / "guided_dev_state" / rel.with_name(rel.name + ".jsonl")


def is_tracked(real_path: Path, project_root: Path) -> bool:
    """Whether *real_path* falls under a tracked root (``specs``/``src``/``test``)."""
    return shadow_path(real_path, project_root) is not None
