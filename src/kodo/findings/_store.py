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
    mint_finding_id,
    new_finding,
    normalize_locations,
    review_round_entry,
)

__all__ = [
    "apply_findings",
    "close_findings_for_paths",
    "last_round_timestamp",
    "outstanding_findings",
    "read_findings",
    "read_jsonl",
    "record_user_feedback",
]

# ``kind`` for the finding minted from a user's rejection comment at the
# document-review gate (doc/FINDINGS.md §3). Deliberately outside every critic's
# vocabulary — no critic raises it, and an author can tell it apart at a glance.
USER_FEEDBACK_KIND = "user_feedback"
USER_FEEDBACK_REPORTER = "user"

# ``reported_by`` on the auto-close line written when a file leaves a work
# product. Distinct from a critic and from the user, so the log says plainly
# that nobody judged this finding fixed — its subject simply went away.
_REMOVED_FILE_REPORTER = "engine:file_removed"


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


def read_findings(findings_dir: Path, key: str) -> list[Finding]:
    """Every finding recorded under *key*, in the order they were opened.

    Args:
        findings_dir: This session's ``findings/`` directory.
        key: The work product's id (:func:`kodo.workproducts.work_product_id`).

    Returns:
        list[Finding]: Current state of each finding; ``[]`` when the work
            product has no log (never reviewed this session) or *key* is
            unusable.
    """
    path = findings_log_path(findings_dir, key)
    if path is None:
        return []
    return list(_replay(read_jsonl(path)).values())


def outstanding_findings(findings: list[Finding]) -> list[Finding]:
    """The subset of *findings* still in the ``outstanding`` state."""
    return [f for f in findings if f["state"] == STATE_OUTSTANDING]


def last_round_timestamp(findings_dir: Path, key: str) -> str:
    """ISO-8601 timestamp of the most recent ``review_round``, or ``""``.

    Consumed by :func:`kodo.tools.document_status` to answer "has this document
    been reviewed since its last revision?" — the one question the retired
    ``feedback`` entry used to answer from the document's own log.
    """
    path = findings_log_path(findings_dir, key)
    if path is None:
        return ""
    stamp = ""
    for entry in read_jsonl(path):
        if entry.get("type") == ENTRY_REVIEW_ROUND:
            stamp = str(entry.get("timestamp", "")) or stamp
    return stamp


def apply_findings(
    findings_dir: Path,
    key: str,
    *,
    reviewer: str,
    updates: list[dict[str, object]],
    project: str = "",
    agent: str = "",
    responsibility_code: str = "",
) -> RoundSummary:
    """Apply one critic round's findings and close the round.

    An update with no ``id`` (or an ``id`` this backlog has never seen) creates
    a new finding, ``outstanding``, under a freshly minted id. An update
    carrying a known ``id`` patches that finding with whichever of
    :data:`~kodo.findings.FINDING_FIELDS` it names, leaving the rest alone. A
    finding the round does not mention is left exactly as it was — silence never
    closes anything (doc/FINDINGS.md §3).

    A ``review_round`` line is appended last, whether or not any finding
    changed: the round happened, and status derivation depends on knowing that.

    Args:
        findings_dir: This session's ``findings/`` directory.
        key: The work product's id — the backlog's identity (doc/FINDINGS.md
            §2). Until 2026-09-04 this was a single document's logical path;
            a reviewable unit is now a whole set of files.
        reviewer: Agent name recorded as the reporter of anything created here.
        updates: The critic's returned ``findings`` list.
        project: Bound root folder name, for id minting.
        agent: The *authoring* agent whose work product this is — not the
            reviewer. Ids describe the subject, so every finding against one
            work product shares a prefix regardless of which critic raised it.
        responsibility_code: Component codename, or ``""``.

    Returns:
        RoundSummary: ``outstanding``/``opened``/``closed`` for this round.

    Raises:
        ValueError: *key* cannot be mapped to a findings log.
    """
    path = findings_log_path(findings_dir, key)
    if path is None:
        raise ValueError(f"{key!r} is not a usable findings key")

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
            # Minted from the *new* finding's own locations, so the id
            # describes what it points at. `taken` spans the whole backlog,
            # including ids minted earlier in this same round.
            finding_id = mint_finding_id(
                project=project,
                agent=agent or reviewer,
                responsibility_code=responsibility_code,
                locations=normalize_locations(changes.get("locations")),
                taken=set(current),
            )
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


def close_findings_for_paths(findings_dir: Path, key: str, paths: tuple[str, ...]) -> list[str]:
    """Auto-close every outstanding finding that points only at *paths*.

    Called when files leave a work product's membership. A finding against a
    file that is no longer part of the work product can never be verified
    fixed — nothing will re-read it — so leaving it outstanding would block
    the review loop forever on work nobody can do.

    A finding spanning several files is closed **only** when every one of its
    locations names a removed file: a cross-file finding whose other side is
    still present is still actionable, and silently closing it would discard
    exactly the defect the multi-file model exists to catch.

    No ``review_round`` line is written — this is bookkeeping, not a review.

    Returns:
        list[str]: The ids closed, for the caller to log.
    """
    log_path = findings_log_path(findings_dir, key)
    if log_path is None or not paths:
        return []
    removed = set(paths)
    current = _replay(read_jsonl(log_path))
    closed: list[str] = []
    lines: list[dict[str, object]] = []
    for finding_id, finding in current.items():
        if finding["state"] != STATE_OUTSTANDING:
            continue
        locations = finding["locations"]
        if not locations or not all(loc["path"] in removed for loc in locations):
            continue
        closed.append(finding_id)
        lines.append(
            finding_entry(
                finding_id=finding_id,
                reported_by=_REMOVED_FILE_REPORTER,
                changes={"state": STATE_FIXED},
            )
        )
    if lines:
        _append(log_path, lines)
    return closed


def record_user_feedback(
    findings_dir: Path,
    key: str,
    comment: str,
    *,
    path: str = "",
    project: str = "",
    agent: str = "",
    responsibility_code: str = "",
) -> str:
    """Mint the user's rejection comment as one outstanding finding.

    The user's objection reaches the author through the same ``get_findings``
    call as every critic finding — one backlog, one procedure (doc/FINDINGS.md
    §3). No ``review_round`` line is written: the user is not a critic round.

    Args:
        findings_dir: This session's ``findings/`` directory.
        key: The work product's id.
        comment: The user's feedback text.
        path: The member file the user was looking at when they objected, so
            the author knows which one to revisit. Empty for feedback about
            the set as a whole.
        project: Bound root folder name, for id minting.
        agent: The authoring agent whose work product this is.
        responsibility_code: Component codename, or ``""``.

    Returns:
        str: The minted finding's id, or ``""`` when nothing was recorded
            (empty comment, or an unusable key).
    """
    text = comment.strip()
    if not text:
        return ""
    log_path = findings_log_path(findings_dir, key)
    if log_path is None:
        return ""
    current = _replay(read_jsonl(log_path))
    locations = (
        [{"path": path, "first_line": None, "last_line": None, "excerpt": ""}] if path else []
    )
    finding_id = mint_finding_id(
        project=project,
        agent=agent or USER_FEEDBACK_REPORTER,
        responsibility_code=responsibility_code,
        locations=normalize_locations(locations),
        taken=set(current),
    )
    _append(
        log_path,
        [
            finding_entry(
                finding_id=finding_id,
                reported_by=USER_FEEDBACK_REPORTER,
                changes={
                    "kind": USER_FEEDBACK_KIND,
                    "description": text,
                    "locations": locations,
                    "state": STATE_OUTSTANDING,
                },
            )
        ],
    )
    return finding_id
