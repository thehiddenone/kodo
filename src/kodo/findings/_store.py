"""Append/replay one document's per-session findings log.

All functions are synchronous file I/O; callers on a hot async path wrap them in
``asyncio.to_thread`` (the same convention :mod:`kodo.guided_state` uses).

There is no index: :func:`read_findings` replays the whole log every time, which
is what makes "omitted fields remain the same" true by construction rather than
by the engine remembering to preserve them.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._paths import findings_log_path
from ._records import (
    ENTRY_FINDING,
    ENTRY_REVIEW_ROUND,
    FINDING_FIELDS,
    STATE_FIXED,
    STATE_OUTSTANDING,
    Finding,
    RoundSummary,
    finding_entry,
    merge_finding,
    new_finding,
    review_round_entry,
)

__all__ = [
    "apply_findings",
    "last_round_timestamp",
    "outstanding_findings",
    "read_findings",
    "read_jsonl",
    "record_user_feedback",
]

_ID_PREFIX = "F"

# ``kind`` for the finding minted from a user's rejection comment at the
# document-review gate (doc/FINDINGS.md §3). Deliberately outside every critic's
# vocabulary — no critic raises it, and an author can tell it apart at a glance.
USER_FEEDBACK_KIND = "user_feedback"
USER_FEEDBACK_REPORTER = "user"


def read_jsonl(jsonl_path: Path) -> list[dict[str, object]]:
    """Parse every line of a ``.jsonl`` file, or ``[]`` if it doesn't exist."""
    if not jsonl_path.exists():
        return []
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _append(jsonl_path: Path, entries: list[dict[str, object]]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _replay(history: list[dict[str, object]]) -> dict[str, Finding]:
    """Fold a log's ``finding`` lines into ``{id: current state}``, in file order."""
    current: dict[str, Finding] = {}
    for entry in history:
        if entry.get("type") != ENTRY_FINDING:
            continue
        finding_id = str(entry.get("id", ""))
        if not finding_id:
            continue
        base = current.get(finding_id) or new_finding(finding_id, str(entry.get("reported_by", "")))
        current[finding_id] = merge_finding(base, entry)
    return current


def _next_id(current: dict[str, Finding]) -> str:
    """Mint the next per-document id: ``F1``, ``F2``, … .

    Derived from the highest numeric suffix already present rather than from the
    count, so a log that somehow skipped a number never reissues an id.
    """
    highest = 0
    for finding_id in current:
        if finding_id.startswith(_ID_PREFIX) and finding_id[len(_ID_PREFIX) :].isdigit():
            highest = max(highest, int(finding_id[len(_ID_PREFIX) :]))
    return f"{_ID_PREFIX}{highest + 1}"


def read_findings(findings_dir: Path, logical_path: str) -> list[Finding]:
    """Every finding recorded for *logical_path*, in the order they were opened.

    Args:
        findings_dir: This session's ``findings/`` directory.
        logical_path: Folder-prefixed logical document path.

    Returns:
        list[Finding]: Current state of each finding; ``[]`` when the document
            has no log (never reviewed in this session) or the path is unusable.
    """
    path = findings_log_path(findings_dir, logical_path)
    if path is None:
        return []
    return list(_replay(read_jsonl(path)).values())


def outstanding_findings(findings: list[Finding]) -> list[Finding]:
    """The subset of *findings* still in the ``outstanding`` state."""
    return [f for f in findings if f["state"] == STATE_OUTSTANDING]


def last_round_timestamp(findings_dir: Path, logical_path: str) -> str:
    """ISO-8601 timestamp of the most recent ``review_round``, or ``""``.

    Consumed by :func:`kodo.tools.document_status` to answer "has this document
    been reviewed since its last revision?" — the one question the retired
    ``feedback`` entry used to answer from the document's own log.
    """
    path = findings_log_path(findings_dir, logical_path)
    if path is None:
        return ""
    stamp = ""
    for entry in read_jsonl(path):
        if entry.get("type") == ENTRY_REVIEW_ROUND:
            stamp = str(entry.get("timestamp", "")) or stamp
    return stamp


def apply_findings(
    findings_dir: Path,
    logical_path: str,
    *,
    reviewer: str,
    updates: list[dict[str, object]],
) -> RoundSummary:
    """Apply one critic round's findings and close the round.

    An update with no ``id`` (or an ``id`` this document has never seen) creates
    a new finding, ``outstanding``, under a freshly minted id. An update
    carrying a known ``id`` patches that finding with whichever of
    :data:`~kodo.findings.FINDING_FIELDS` it names, leaving the rest alone. A
    finding the round does not mention is left exactly as it was — silence never
    closes anything (doc/FINDINGS.md §3).

    A ``review_round`` line is appended last, whether or not any finding
    changed: the round happened, and the document's status derivation depends on
    knowing that.

    Args:
        findings_dir: This session's ``findings/`` directory.
        logical_path: Folder-prefixed logical document path.
        reviewer: Agent name recorded as the reporter of anything created here.
        updates: The critic's returned ``findings`` list.

    Returns:
        RoundSummary: ``outstanding``/``opened``/``closed`` for this round.

    Raises:
        ValueError: *logical_path* cannot be mapped to a findings log.
    """
    path = findings_log_path(findings_dir, logical_path)
    if path is None:
        raise ValueError(f"{logical_path!r} is not a usable findings key")

    current = _replay(read_jsonl(path))
    lines: list[dict[str, object]] = []
    opened = 0
    closed = 0

    for update in updates:
        raw_id = update.get("id")
        finding_id = str(raw_id).strip() if isinstance(raw_id, str) else ""
        changes = {k: v for k, v in update.items() if k in FINDING_FIELDS}
        if finding_id and finding_id in current:
            was_outstanding = current[finding_id]["state"] == STATE_OUTSTANDING
            merged = merge_finding(current[finding_id], changes)
            if was_outstanding and merged["state"] == STATE_FIXED:
                closed += 1
            current[finding_id] = merged
        else:
            finding_id = _next_id(current)
            current[finding_id] = merge_finding(new_finding(finding_id, reviewer), changes)
            opened += 1
        lines.append(finding_entry(finding_id=finding_id, reported_by=reviewer, changes=changes))

    summary = RoundSummary(
        outstanding=len(outstanding_findings(list(current.values()))),
        opened=opened,
        closed=closed,
    )
    lines.append(review_round_entry(reviewer=reviewer, summary=summary))
    _append(path, lines)
    return summary


def record_user_feedback(findings_dir: Path, logical_path: str, comment: str) -> str:
    """Mint the user's rejection comment as one outstanding finding.

    The user's objection reaches the author through the same ``get_findings``
    call as every critic finding — one backlog, one procedure (doc/FINDINGS.md
    §3). No ``review_round`` line is written: the user is not a critic round.

    Args:
        findings_dir: This session's ``findings/`` directory.
        logical_path: Folder-prefixed logical document path.
        comment: The user's feedback text.

    Returns:
        str: The minted finding's id, or ``""`` when nothing was recorded
            (empty comment, or an unusable path).
    """
    text = comment.strip()
    if not text:
        return ""
    path = findings_log_path(findings_dir, logical_path)
    if path is None:
        return ""
    current = _replay(read_jsonl(path))
    finding_id = _next_id(current)
    _append(
        path,
        [
            finding_entry(
                finding_id=finding_id,
                reported_by=USER_FEEDBACK_REPORTER,
                changes={
                    "kind": USER_FEEDBACK_KIND,
                    "description": text,
                    "state": STATE_OUTSTANDING,
                },
            )
        ],
    )
    return finding_id
