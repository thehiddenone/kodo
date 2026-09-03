"""The two append-only entry types in a document's findings log, and the merge rule.

A **finding** is one defect a critic raised against one document, with an
identity (``id``) that outlives the round it was raised in. A findings log holds
two kinds of line:

* ``finding`` — the first line carrying a given ``id`` *creates* that finding;
  every later line carrying the same ``id`` *patches* it. Fields absent from a
  line are unchanged, which is the "omitted fields remain the same" rule applied
  at the storage layer rather than in the engine.
* ``review_round`` — one per completed critic round, carrying that round's
  progress counters. It is also what makes "has this document been reviewed
  since its last revision?" answerable (see ``kodo.tools.document_status``).

Current state is always a replay of the file — there is no index. See
doc/FINDINGS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict

__all__ = [
    "ENTRY_FINDING",
    "ENTRY_REVIEW_ROUND",
    "FINDING_FIELDS",
    "STATE_FIXED",
    "STATE_OUTSTANDING",
    "Finding",
    "FindingState",
    "RoundSummary",
    "finding_entry",
    "merge_finding",
    "new_finding",
    "review_round_entry",
]

STATE_OUTSTANDING = "outstanding"
STATE_FIXED = "fixed"

FindingState = Literal["outstanding", "fixed"]

ENTRY_FINDING = "finding"
ENTRY_REVIEW_ROUND = "review_round"

# The mutable fields of a finding — the exact set a critic may patch. ``id`` is
# not here: it is the identity, minted by the engine and never rewritten.
FINDING_FIELDS: tuple[str, ...] = (
    "kind",
    "description",
    "excerpt",
    "first_line",
    "last_line",
    "state",
)


class Finding(TypedDict):
    """One finding's current state, as replayed from a findings log.

    ``kind``/``description``/``excerpt`` deliberately keep the names the retired
    ``concern_item`` shape used, so the critics' existing vocabulary sections did
    not have to be rewritten; ``id`` and ``state`` are the new half.
    """

    id: str
    kind: str
    description: str
    excerpt: str
    first_line: int | None
    last_line: int | None
    state: str
    reported_by: str


@dataclass(frozen=True)
class RoundSummary:
    """What one critic round did to a document's backlog.

    Attributes:
        outstanding: Findings still ``outstanding`` after the round's updates.
        opened: Findings this round created.
        closed: Findings this round moved from ``outstanding`` to ``fixed``.
    """

    outstanding: int
    opened: int
    closed: int

    @property
    def stalled(self) -> bool:
        """Whether the round made no progress at all — closed nothing, found nothing.

        The loop's stall detector (doc/FINDINGS.md §4). Distinct from "the count
        did not drop": a round that closes two findings and opens two more did
        real work and is not stalled.
        """
        return self.opened == 0 and self.closed == 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_finding(finding_id: str, reported_by: str) -> Finding:
    """A blank finding with *finding_id*, before any field is patched into it."""
    return Finding(
        id=finding_id,
        kind="",
        description="",
        excerpt="",
        first_line=None,
        last_line=None,
        state=STATE_OUTSTANDING,
        reported_by=reported_by,
    )


def merge_finding(current: Finding, update: dict[str, object]) -> Finding:
    """Apply one ``finding`` log line's changed fields onto *current*.

    Only keys in :data:`FINDING_FIELDS` are honoured, and only when present:
    an omitted field leaves the existing value alone. ``state`` is coerced to
    one of the two legal values, so a critic that invents a third cannot put the
    store into a state nothing can read.

    Args:
        current: The finding's state before this line.
        update: The raw log line (or a critic's update object).

    Returns:
        Finding: A new dict with the line's fields applied.
    """
    merged: Finding = dict(current)  # type: ignore[assignment]
    for field in FINDING_FIELDS:
        if field not in update:
            continue
        value = update[field]
        if field in ("first_line", "last_line"):
            merged[field] = value if isinstance(value, int) else None  # type: ignore[literal-required]
        elif field == "state":
            merged["state"] = STATE_FIXED if str(value) == STATE_FIXED else STATE_OUTSTANDING
        else:
            merged[field] = "" if value is None else str(value)  # type: ignore[literal-required]
    return merged


def finding_entry(
    *, finding_id: str, reported_by: str, changes: dict[str, object]
) -> dict[str, object]:
    """One ``finding`` log line: an id plus only the fields that changed."""
    entry: dict[str, object] = {
        "type": ENTRY_FINDING,
        "timestamp": _now(),
        "id": finding_id,
        "reported_by": reported_by,
    }
    for field in FINDING_FIELDS:
        if field in changes:
            entry[field] = changes[field]
    return entry


def review_round_entry(*, reviewer: str, summary: RoundSummary) -> dict[str, object]:
    """One ``review_round`` log line, closing a critic round."""
    return {
        "type": ENTRY_REVIEW_ROUND,
        "timestamp": _now(),
        "reviewer": reviewer,
        "outstanding": summary.outstanding,
        "opened": summary.opened,
        "closed": summary.closed,
    }
