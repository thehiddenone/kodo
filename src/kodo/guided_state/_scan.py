"""Scans ``.kodo/guided_dev_state/`` for every tracked document's status.

Backs the ``guided_dev_status`` tool — the replacement for the old
artifact-index-based ``query_frontier``. There is no in-memory index: every
call re-walks the on-disk ``.jsonl`` logs.
"""

from __future__ import annotations

from pathlib import Path

from ._records import last_revision_timestamp
from ._store import read_jsonl

__all__ = ["scan_tracked_files"]

_JSONL_SUFFIX = ".jsonl"


def scan_tracked_files(project_root: Path) -> list[dict[str, object]]:
    """Every tracked document's raw status inputs, sorted by path.

    Each entry is ``{path, last_entry, last_revision_ts, last_event}`` with
    ``path`` relative to *project_root*. It stops short of a derived status on
    purpose: since findings moved to their own session-scoped store, a status is
    a function of two logs, and the merge belongs to
    :func:`kodo.tools.document_status`. This walk owns only the project half.
    """
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
                "path": real_rel.as_posix(),
                "last_entry": last,
                "last_revision_ts": last_revision_timestamp(history),
                "last_event": str(last.get("timestamp", "")),
            }
        )
    return results
