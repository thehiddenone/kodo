"""Append/read a document's ``.jsonl`` evolution log.

``append_feedback`` is gone along with the ``feedback`` entry type: a critic's
verdict is no longer review content written here, it is a set of identified,
stateful findings in :mod:`kodo.findings` (doc/FINDINGS.md).

All functions are synchronous file I/O; callers on a hot async path wrap
them in ``asyncio.to_thread`` (the same convention
:mod:`kodo.runtime._checkpoints` uses for its own state file).
"""

from __future__ import annotations

import json
from pathlib import Path

from ._paths import shadow_path
from ._records import (
    accepted_entry,
    last_revision_timestamp,
    new_revision_entry,
    review_result_entry,
)

__all__ = [
    "append_accepted",
    "append_new_revision",
    "append_review_result",
    "read_document_state",
    "read_history",
    "read_jsonl",
]


def _append(jsonl_path: Path, entry: dict[str, object]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def append_new_revision(
    real_path: Path,
    project_root: Path,
    *,
    commit_hash: str,
    author: str,
    tool: str,
    summary: str,
    workflow: str,
) -> None:
    """Record an author's revision. No-op when *real_path* is untracked."""
    path = shadow_path(real_path, project_root)
    if path is None:
        return
    _append(
        path,
        new_revision_entry(
            commit_hash=commit_hash, author=author, tool=tool, summary=summary, workflow=workflow
        ),
    )


def append_review_result(
    real_path: Path, project_root: Path, *, decision: str, comment: str
) -> None:
    """Record the user's review decision. Engine-only — never via a tool.

    Raises:
        ValueError: *real_path* is not a tracked guided-dev document.
    """
    path = shadow_path(real_path, project_root)
    if path is None:
        raise ValueError(f"{real_path} is not a tracked guided-dev document")
    _append(path, review_result_entry(decision=decision, comment=comment))


def append_accepted(real_path: Path, project_root: Path) -> None:
    """Record the acceptance marker. Engine-only — never via a tool.

    ``commit_hash`` is read from the most recent ``new_revision`` entry —
    acceptance never produces a new commit.

    Raises:
        ValueError: *real_path* is not a tracked guided-dev document.
    """
    path = shadow_path(real_path, project_root)
    if path is None:
        raise ValueError(f"{real_path} is not a tracked guided-dev document")
    commit_hash = ""
    for entry in reversed(read_jsonl(path)):
        if entry.get("type") == "new_revision":
            commit_hash = str(entry.get("commit_hash", ""))
            break
    _append(path, accepted_entry(commit_hash=commit_hash))


def read_jsonl(jsonl_path: Path) -> list[dict[str, object]]:
    """Parse every line of a ``.jsonl`` file, or ``[]`` if it doesn't exist."""
    if not jsonl_path.exists():
        return []
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_history(real_path: Path, project_root: Path) -> list[dict[str, object]]:
    """Full append-only history for *real_path*, or ``[]`` if untracked/empty."""
    path = shadow_path(real_path, project_root)
    if path is None:
        return []
    return read_jsonl(path)


def read_document_state(real_path: Path, project_root: Path) -> dict[str, object] | None:
    """The inputs :func:`derive_status` needs from *real_path*'s own log.

    Returns ``{"last_entry", "last_revision_ts", "last_event"}``, or ``None``
    when the path is untracked or its log is empty. It deliberately stops short
    of a status: the findings half of the answer lives in another store, and the
    merge belongs to :func:`kodo.tools.document_status`, not here.
    """
    history = read_history(real_path, project_root)
    if not history:
        return None
    last = history[-1]
    return {
        "last_entry": last,
        "last_revision_ts": last_revision_timestamp(history),
        "last_event": str(last.get("timestamp", "")),
    }
