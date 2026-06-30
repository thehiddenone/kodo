"""Scans ``.kodo/guided_dev_state/`` for every tracked document's status.

Backs the ``guided_dev_status`` tool — the replacement for the old
artifact-index-based ``query_frontier``. There is no in-memory index: every
call re-walks the on-disk ``.jsonl`` logs.
"""

from __future__ import annotations

from pathlib import Path

from ._records import derive_status
from ._store import read_jsonl

__all__ = ["scan_tracked_files"]

_JSONL_SUFFIX = ".jsonl"


def scan_tracked_files(project_root: Path) -> list[dict[str, object]]:
    """Every tracked document's ``{path, status, last_event}``, sorted by path."""
    state_dir = project_root.resolve() / ".kodo" / "guided_dev_state"
    if not state_dir.exists():
        return []
    results: list[dict[str, object]] = []
    for jsonl_path in sorted(state_dir.rglob(f"*{_JSONL_SUFFIX}")):
        history = read_jsonl(jsonl_path)
        if not history:
            continue
        rel = jsonl_path.relative_to(state_dir)
        real_rel = rel.with_name(rel.name[: -len(_JSONL_SUFFIX)])
        last = history[-1]
        results.append(
            {
                "path": str(real_rel),
                "status": derive_status(last),
                "last_event": str(last.get("timestamp", "")),
            }
        )
    return results
