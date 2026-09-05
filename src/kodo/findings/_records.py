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

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal, TypedDict

__all__ = [
    "ENTRY_FINDING",
    "Location",
    "ENTRY_REVIEW_ROUND",
    "FINDING_FIELDS",
    "STATE_FIXED",
    "STATE_OUTSTANDING",
    "Finding",
    "FindingState",
    "RoundSummary",
    "finding_entry",
    "merge_finding",
    "mint_finding_id",
    "normalize_locations",
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
    "locations",
    "state",
)

# One place a finding points at. Replaced the flat ``excerpt``/``first_line``/
# ``last_line`` trio on 2026-09-04, when the reviewable unit became a whole
# work product rather than one document: a finding now has to say *which* file
# it is about, and the defect a multi-file review exists to catch --- "the
# signature in a.py does not match the call in b.py" --- needs to name both
# sides at once. A list, not a single location, is the whole point.
LOCATION_FIELDS: tuple[str, ...] = ("path", "first_line", "last_line", "excerpt")


class Location(TypedDict):
    """One file span a finding points at."""

    path: str
    first_line: int | None
    last_line: int | None
    excerpt: str


class Finding(TypedDict):
    """One finding's current state, as replayed from a findings log.

    ``kind``/``description`` keep the names the retired ``concern_item`` shape
    used, so the critics' vocabulary sections did not have to be rewritten;
    ``id``, ``state`` and ``locations`` are the newer halves.

    ``id`` is *derived from* the first location at creation time
    (:func:`mint_finding_id`) and then frozen. It reads like a location and is
    not one: the file is revised between rounds, so its embedded line number
    drifts within a round of being minted. Never resolve an id back to a
    position --- ``locations`` is the only current answer to "where".
    """

    id: str
    kind: str
    description: str
    locations: list[Location]
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
        locations=[],
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
        if field == "locations":
            # Wholesale replacement, not a merge: a critic that re-reports
            # locations has re-read the file and is describing where the
            # problem is *now*. Merging position-by-position would resurrect
            # spans it deliberately dropped.
            merged["locations"] = normalize_locations(value)
        elif field == "state":
            merged["state"] = STATE_FIXED if str(value) == STATE_FIXED else STATE_OUTSTANDING
        else:
            merged[field] = "" if value is None else str(value)  # type: ignore[literal-required]
    return merged


def normalize_locations(value: object) -> list[Location]:
    """Coerce a critic's raw ``locations`` into well-formed :class:`Location`s.

    Model-authored, so nothing is trusted: a non-list yields ``[]``, a
    non-dict element is skipped, and a location with no ``path`` is dropped
    (a span that cannot say which file it is in is not usable). Line numbers
    that are not integers become ``None`` rather than failing the whole
    update --- a finding with a fuzzy position still carries its description.
    """
    if not isinstance(value, list):
        return []
    locations: list[Location] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path", "")).strip()
        if not path:
            continue
        first = raw.get("first_line")
        last = raw.get("last_line")
        excerpt = raw.get("excerpt")
        locations.append(
            Location(
                path=path,
                first_line=first if isinstance(first, int) else None,
                last_line=last if isinstance(last, int) else None,
                excerpt="" if excerpt is None else str(excerpt),
            )
        )
    return locations


def _id_segment(raw: str) -> str:
    """One id segment: filesystem- and eyeball-safe, never empty.

    Underscores are **kept**, even though they are also the separator between
    segments: agent names are full of them (``requirements_author``), and
    mangling those to keep the separator unambiguous would trade the id's
    readability for a property nothing needs — an id is only ever compared,
    never parsed back into its parts.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._\-]+", "-", raw.strip()).strip("-.")
    return cleaned or "_"


def _candidate_lines(locations: list[Location]) -> list[int]:
    """Every line this finding covers, topmost first.

    The id prefers the finding's first line, and falls back to any *other*
    line it genuinely covers when that one is taken --- so two findings on
    overlapping spans still get distinct, still-meaningful ids rather than a
    counter suffix.
    """
    lines: list[int] = []
    for location in locations:
        first = location["first_line"]
        last = location["last_line"]
        if first is None:
            continue
        end = last if isinstance(last, int) and last >= first else first
        # Bounded: a finding covering a whole large file should not enumerate
        # thousands of candidates just to break a name collision.
        lines.extend(range(first, min(end, first + 200) + 1))
    return list(dict.fromkeys(lines))


def mint_finding_id(
    *,
    project: str,
    agent: str,
    responsibility_code: str,
    locations: list[Location],
    taken: set[str],
) -> str:
    """Derive a stable, self-describing id for a new finding.

    ``<project>_<agent>[_<responsibility>]_<file>_<line>`` --- readable at a
    glance and unique within the session, which the old per-log ``F1``/``F2``
    counters were not (every document restarted at ``F1``, so an id meant
    nothing outside the one log it came from).

    ``file`` is the **basename** of the first location, not its full logical
    path: the path is already recorded in ``locations``, and spelling it into
    the id would produce unreadable strings for no gain.

    Collisions are resolved by trying the finding's other covered lines before
    falling back to a numeric suffix, so an id keeps pointing at something
    real wherever possible.

    The result is frozen at creation. It reads like a location and is not one
    --- see :class:`Finding`.
    """
    prefix_parts = [_id_segment(project or "_"), _id_segment(agent or "agent")]
    if responsibility_code.strip():
        prefix_parts.append(_id_segment(responsibility_code))
    if locations:
        prefix_parts.append(_id_segment(PurePosixPath(locations[0]["path"]).name))
    prefix = "_".join(prefix_parts)

    for line in _candidate_lines(locations):
        candidate = f"{prefix}_{line}"
        if candidate not in taken:
            return candidate

    # No usable line (a finding about the file as a whole), or every line this
    # finding covers is already spoken for.
    suffix = 1
    while True:
        candidate = prefix if suffix == 1 else f"{prefix}_{suffix}"
        if candidate not in taken:
            return candidate
        suffix += 1


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
