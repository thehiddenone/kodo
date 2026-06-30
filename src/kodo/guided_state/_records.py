"""The four append-only entry types in a document's ``.jsonl`` evolution log.

A document's current status is always derived from the *last* line of its
log (:func:`derive_status`) — never from any in-memory index. ``new_revision``
is written by the engine after a ``filesystem``/``edit_file`` commit;
``feedback`` is written by the ``document_feedback`` tool (critics only);
``review_result`` and ``accepted`` are written by the engine alone — no
dispatched tool ever produces them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict

__all__ = [
    "ConcernItem",
    "Status",
    "accepted_entry",
    "derive_status",
    "feedback_entry",
    "new_revision_entry",
    "review_result_entry",
]

Status = Literal["pending_review", "needs_revision", "pending_acceptance", "accepted"]


class ConcernItem(TypedDict, total=False):
    """One critic concern — matches the ``concern_item`` tool-schema shape."""

    kind: str
    description: str
    first_line: int | None
    last_line: int | None
    excerpt: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_revision_entry(
    *, commit_hash: str, author: str, tool: str, summary: str, workflow: str
) -> dict[str, object]:
    """An author's revision, recorded right after its mirror commit."""
    return {
        "type": "new_revision",
        "timestamp": _now(),
        "commit_hash": commit_hash,
        "author": author,
        "tool": tool,
        "summary": summary,
        "workflow": workflow,
    }


def feedback_entry(
    *, reviewer: str, accept: bool, concerns: list[ConcernItem], summary: str
) -> dict[str, object]:
    """A critic's verdict, written by the ``document_feedback`` tool."""
    return {
        "type": "feedback",
        "timestamp": _now(),
        "reviewer": reviewer,
        "accept": accept,
        "concerns": list(concerns),
        "summary": summary,
    }


def review_result_entry(*, decision: str, comment: str) -> dict[str, object]:
    """A user's review decision (``approve``/``reject``); engine-written only."""
    return {
        "type": "review_result",
        "timestamp": _now(),
        "decision": decision,
        "comment": comment,
    }


def accepted_entry(*, commit_hash: str) -> dict[str, object]:
    """The acceptance marker; engine-written only.

    ``commit_hash`` always equals the immediately preceding ``new_revision``
    entry's hash — acceptance never produces a new commit.
    """
    return {
        "type": "accepted",
        "timestamp": _now(),
        "commit_hash": commit_hash,
    }


def derive_status(last_entry: dict[str, object] | None) -> Status:
    """Derive a document's current status from its log's last entry."""
    if last_entry is None:
        return "pending_review"
    entry_type = last_entry.get("type")
    if entry_type == "new_revision":
        return "pending_review"
    if entry_type == "feedback":
        return "pending_acceptance" if last_entry.get("accept") else "needs_revision"
    if entry_type == "review_result":
        return "needs_revision" if last_entry.get("decision") == "reject" else "pending_acceptance"
    if entry_type == "accepted":
        return "accepted"
    return "pending_review"
