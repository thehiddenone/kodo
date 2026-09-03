"""The three append-only entry types in a document's ``.jsonl`` evolution log.

``new_revision`` is written by the engine after a
``filesystem``/``edit_file``/``create_file`` commit; ``review_result`` and
``accepted`` are written by the engine alone — no dispatched tool ever produces
any of them.

There is **no ``feedback`` entry any more.** A critic's verdict used to land
here as ``{accept, concerns}``; concerns became :mod:`kodo.findings` — an
identified, stateful, session-scoped backlog — so this project-scoped log no
longer carries review content at all. A document's status is consequently a
function of *two* stores, and :func:`derive_status` takes the findings half as
arguments rather than reaching for it: this package stays a leaf, and the merge
happens in exactly one place (:func:`kodo.tools.document_status`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "Status",
    "accepted_entry",
    "derive_status",
    "last_revision_timestamp",
    "new_revision_entry",
    "review_result_entry",
]

Status = Literal["pending_review", "needs_revision", "pending_acceptance", "accepted"]


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


def derive_status(
    last_entry: dict[str, object] | None,
    *,
    reviewed: bool = False,
    outstanding: int = 0,
) -> Status:
    """Derive a document's current status from both of its stores.

    The document's own log settles the question outright once the user has had
    their say (``accepted`` / ``review_result``). Before that, the answer comes
    from the session's findings backlog for the same document, which the caller
    supplies as two plain values so this package keeps importing nothing:

    * ``outstanding > 0`` — a critic (or the user, whose rejection is minted as
      a finding) has open objections: ``needs_revision``.
    * nothing outstanding and *reviewed* — a critic round ran since the last
      revision and left the backlog empty: ``pending_acceptance``.
    * nothing outstanding and not *reviewed* — the document has been written but
      not looked at since: ``pending_review``.

    A legacy ``feedback`` entry left by an older build is not interpreted; it
    falls through to the same branch as ``new_revision``.

    Args:
        last_entry: The document log's last line, or ``None`` for an empty log.
        reviewed: Whether a ``review_round`` was recorded for this document more
            recently than its last ``new_revision``.
        outstanding: How many findings are still ``outstanding`` for it.

    Returns:
        Status: One of ``pending_review``/``needs_revision``/
            ``pending_acceptance``/``accepted``.
    """
    entry_type = last_entry.get("type") if last_entry else None
    if entry_type == "accepted":
        return "accepted"
    if entry_type == "review_result":
        rejected = (
            bool(last_entry) and last_entry is not None and last_entry.get("decision") == "reject"
        )
        return "needs_revision" if rejected else "pending_acceptance"
    if outstanding > 0:
        return "needs_revision"
    return "pending_acceptance" if reviewed else "pending_review"


def last_revision_timestamp(history: list[dict[str, object]]) -> str:
    """ISO-8601 timestamp of the most recent ``new_revision`` in *history*, or ``""``.

    Paired with :func:`kodo.findings.last_round_timestamp` to answer
    :func:`derive_status`'s ``reviewed`` question — both logs are written by the
    same process in ISO-8601 UTC, so the comparison is a plain string compare.
    """
    for entry in reversed(history):
        if entry.get("type") == "new_revision":
            return str(entry.get("timestamp", ""))
    return ""
