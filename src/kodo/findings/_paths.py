"""Maps a logical document path to its per-session findings-log path.

Findings are **session**-scoped, not project-scoped (doc/FINDINGS.md §2): the
log lives under ``<session-dir>/findings/`` rather than beside the document's
project-scoped evolution log in ``.kodo/guided_dev_state/``. The key is the
folder-prefixed *logical* path agents already use everywhere
(``billing-service/specs/architecture.md``), so a session bound to several
projects keeps their backlogs apart without any extra bookkeeping.

The logical path is agent-supplied, so every segment is validated here: a path
that is absolute, empty, or contains a traversal segment yields ``None`` rather
than a file outside the findings directory.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

__all__ = ["findings_log_path"]

_JSONL_SUFFIX = ".jsonl"

# Anything outside this set is replaced with '_' — workspace-folder display
# names reach us as the first segment and are not guaranteed filesystem-safe.
_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]")


def _sanitize(segment: str) -> str:
    return _UNSAFE.sub("_", segment).strip() or "_"


def findings_log_path(findings_dir: Path, logical_path: str) -> Path | None:
    """The findings log for *logical_path*, or ``None`` when it is unusable.

    Args:
        findings_dir: This session's ``findings/`` directory.
        logical_path: Folder-prefixed logical document path, e.g.
            ``"billing-service/specs/architecture.md"``.

    Returns:
        Path | None: ``<findings_dir>/<sanitised segments>.jsonl``, or ``None``
            when *logical_path* is empty, absolute, or contains a ``.``/``..``
            segment.
    """
    candidate = logical_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/"):
        return None
    parts = [p for p in PurePosixPath(candidate).parts if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    safe = [_sanitize(p) for p in parts]
    safe[-1] = safe[-1] + _JSONL_SUFFIX
    return findings_dir.joinpath(*safe)
