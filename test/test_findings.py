"""Unit tests for :mod:`kodo.findings` — the shared author/critic backlog.

The rules these cover are the ones the whole design rests on (doc/FINDINGS.md):
an update carries only what changed, silence changes nothing, ids are minted
per document and never reissued, and current state is always a replay of the
log rather than an index that could drift from it.
"""

from __future__ import annotations

from pathlib import Path

from kodo.findings import (
    STATE_FIXED,
    STATE_OUTSTANDING,
    USER_FEEDBACK_KIND,
    apply_findings,
    findings_log_path,
    last_round_timestamp,
    outstanding_findings,
    read_findings,
    record_user_feedback,
)

_DOC = "proj/specs/architecture.md"


def _states(findings_dir: Path, logical: str = _DOC) -> dict[str, str]:
    return {f["id"]: f["state"] for f in read_findings(findings_dir, logical)}


# ---------------------------------------------------------------------------
# findings_log_path
# ---------------------------------------------------------------------------


def test_log_path_mirrors_the_logical_path_under_the_session_findings_dir(
    tmp_path: Path,
) -> None:
    assert (
        findings_log_path(tmp_path, "billing-service/specs/design/auth.md")
        == tmp_path / "billing-service" / "specs" / "design" / "auth.md.jsonl"
    )


def test_log_path_refuses_anything_that_could_escape_the_findings_dir(tmp_path: Path) -> None:
    """The logical path is agent-supplied, so a traversal must not resolve to a
    file outside the store."""
    for bad in ("", "   ", "/etc/passwd", "../../secrets.md", "proj/../../x.md", ".."):
        assert findings_log_path(tmp_path, bad) is None, bad
    # A leading "./" is not an escape — it normalises away and stays inside.
    inside = findings_log_path(tmp_path, "./x.md")
    assert inside is not None and inside.is_relative_to(tmp_path)


def test_log_path_sanitises_an_unsafe_root_segment(tmp_path: Path) -> None:
    """The first segment is a workspace-folder display name, which is not
    guaranteed filesystem-safe — it must be sanitised, not rejected."""
    path = findings_log_path(tmp_path, "my:proj*name/specs/a.md")
    assert path is not None
    assert path.is_relative_to(tmp_path)
    assert ":" not in path.parts[-3] and "*" not in path.parts[-3]


# ---------------------------------------------------------------------------
# apply_findings — creating, patching, and the round summary
# ---------------------------------------------------------------------------


def test_a_finding_with_no_id_is_created_with_a_freshly_minted_one(tmp_path: Path) -> None:
    summary = apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[
            {"kind": "gap", "description": "a", "first_line": 3, "last_line": 5, "excerpt": "x"},
            {"kind": "orphan", "description": "b"},
        ],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 2, 0)
    assert summary.stalled is False
    findings = read_findings(tmp_path, _DOC)
    assert [f["id"] for f in findings] == ["F1", "F2"]
    assert findings[0]["kind"] == "gap"
    assert findings[0]["description"] == "a"
    assert findings[0]["excerpt"] == "x"
    assert findings[0]["first_line"] == 3
    assert findings[0]["last_line"] == 5
    assert findings[0]["state"] == STATE_OUTSTANDING
    assert findings[0]["reported_by"] == "architect_critic"


def test_an_update_carries_only_what_changed(tmp_path: Path) -> None:
    """The rule the whole protocol rests on: omitted fields keep their values,
    so ``{"id": "F1", "state": "fixed"}`` is a complete, correct close."""
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[
            {
                "kind": "gap",
                "description": "the settlement path is unowned",
                "excerpt": "…",
                "first_line": 10,
                "last_line": 12,
            }
        ],
    )

    summary = apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F1", "state": STATE_FIXED}]
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (0, 0, 1)
    finding = read_findings(tmp_path, _DOC)[0]
    assert finding["state"] == STATE_FIXED
    # Everything the update did not name survived verbatim.
    assert finding["kind"] == "gap"
    assert finding["description"] == "the settlement path is unowned"
    assert finding["excerpt"] == "…"
    assert (finding["first_line"], finding["last_line"]) == (10, 12)


def test_an_update_can_revise_wording_and_span_without_a_new_identity(tmp_path: Path) -> None:
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "old", "first_line": 1, "last_line": 1}],
    )
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[
            {"id": "F1", "description": "still wrong, now for another reason", "last_line": 9}
        ],
    )

    findings = read_findings(tmp_path, _DOC)
    assert len(findings) == 1  # still one finding, not two
    assert findings[0]["description"] == "still wrong, now for another reason"
    assert (findings[0]["first_line"], findings[0]["last_line"]) == (1, 9)
    assert findings[0]["state"] == STATE_OUTSTANDING


def test_a_finding_the_round_never_mentions_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    """Silence closes nothing — the cost of a critic overlooking a finding is a
    wasted round, never a defect recorded as fixed."""
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )

    summary = apply_findings(tmp_path, _DOC, reviewer="architect_critic", updates=[])

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 0, 0)
    assert summary.stalled is True
    assert _states(tmp_path) == {"F1": STATE_OUTSTANDING, "F2": STATE_OUTSTANDING}


def test_a_round_that_closes_and_opens_in_equal_numbers_is_not_stalled(tmp_path: Path) -> None:
    """The exact case the retired count heuristic got wrong."""
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )

    summary = apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[
            {"id": "F1", "state": STATE_FIXED},
            {"id": "F2", "state": STATE_FIXED},
            {"kind": "gap", "description": "c"},
            {"kind": "gap", "description": "d"},
        ],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 2, 2)
    assert summary.stalled is False


def test_reopening_a_closed_finding_reuses_its_identity(tmp_path: Path) -> None:
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"kind": "gap", "description": "a"}]
    )
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F1", "state": STATE_FIXED}]
    )

    summary = apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"id": "F1", "state": STATE_OUTSTANDING, "description": "the fix regressed"}],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (1, 0, 0)
    assert _states(tmp_path) == {"F1": STATE_OUTSTANDING}


def test_closing_an_already_closed_finding_is_not_counted_twice(tmp_path: Path) -> None:
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"kind": "gap", "description": "a"}]
    )
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F1", "state": STATE_FIXED}]
    )

    summary = apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F1", "state": STATE_FIXED}]
    )

    assert summary.closed == 0
    assert summary.stalled is True


def test_an_unknown_id_is_treated_as_a_new_finding_rather_than_lost(tmp_path: Path) -> None:
    """A critic that invents an id must not write into nothing — the finding is
    still recorded, under an id the store actually controls."""
    summary = apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"id": "F99", "kind": "gap", "description": "invented id"}],
    )

    assert summary.opened == 1
    assert [f["id"] for f in read_findings(tmp_path, _DOC)] == ["F1"]


def test_an_invalid_state_value_falls_back_to_outstanding(tmp_path: Path) -> None:
    """A third state would put the store somewhere nothing can read, so the
    store coerces rather than trusting the model's string."""
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "a", "state": "mostly_fixed"}],
    )
    assert _states(tmp_path) == {"F1": STATE_OUTSTANDING}


def test_ids_are_never_reissued_and_are_scoped_per_document(tmp_path: Path) -> None:
    other = "proj/specs/requirements.md"
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F1", "state": STATE_FIXED}]
    )
    # A third finding on the same document continues the sequence past the
    # closed one rather than reusing F1.
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"kind": "gap", "description": "c"}]
    )
    assert [f["id"] for f in read_findings(tmp_path, _DOC)] == ["F1", "F2", "F3"]

    # A different document starts its own sequence.
    apply_findings(
        tmp_path,
        other,
        reviewer="requirements_critic",
        updates=[{"kind": "ambiguity", "description": "z"}],
    )
    assert [f["id"] for f in read_findings(tmp_path, other)] == ["F1"]


def test_state_survives_a_fresh_read_because_it_is_replayed_from_disk(tmp_path: Path) -> None:
    apply_findings(
        tmp_path,
        _DOC,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"id": "F2", "state": STATE_FIXED}]
    )

    # Nothing in memory carries over — this is a cold read of the log.
    assert _states(tmp_path) == {"F1": STATE_OUTSTANDING, "F2": STATE_FIXED}
    assert [f["id"] for f in outstanding_findings(read_findings(tmp_path, _DOC))] == ["F1"]


def test_apply_findings_rejects_an_unusable_path(tmp_path: Path) -> None:
    try:
        apply_findings(tmp_path, "../escape.md", reviewer="x", updates=[])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a path that cannot be keyed")


# ---------------------------------------------------------------------------
# review rounds
# ---------------------------------------------------------------------------


def test_every_round_is_recorded_even_when_it_changed_nothing(tmp_path: Path) -> None:
    """The round marker is what makes "reviewed since the last revision"
    answerable, so it must be written whether or not any finding moved."""
    assert last_round_timestamp(tmp_path, _DOC) == ""

    apply_findings(tmp_path, _DOC, reviewer="architect_critic", updates=[])
    first = last_round_timestamp(tmp_path, _DOC)
    assert first

    apply_findings(tmp_path, _DOC, reviewer="architect_critic", updates=[])
    assert last_round_timestamp(tmp_path, _DOC) >= first


def test_reading_a_document_with_no_log_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_findings(tmp_path, "proj/specs/never-reviewed.md") == []
    assert last_round_timestamp(tmp_path, "proj/specs/never-reviewed.md") == ""
    assert read_findings(tmp_path, "../escape.md") == []


# ---------------------------------------------------------------------------
# the user's rejection
# ---------------------------------------------------------------------------


def test_user_feedback_becomes_an_outstanding_finding_in_the_same_backlog(
    tmp_path: Path,
) -> None:
    apply_findings(
        tmp_path, _DOC, reviewer="architect_critic", updates=[{"kind": "gap", "description": "a"}]
    )

    finding_id = record_user_feedback(tmp_path, _DOC, "  the North Star is missing  ")

    assert finding_id == "F2"
    findings = read_findings(tmp_path, _DOC)
    assert findings[1]["kind"] == USER_FEEDBACK_KIND
    assert findings[1]["reported_by"] == "user"
    assert findings[1]["description"] == "the North Star is missing"
    assert findings[1]["state"] == STATE_OUTSTANDING


def test_user_feedback_does_not_count_as_a_review_round(tmp_path: Path) -> None:
    """The user is not a critic: their rejection must not make a document look
    'reviewed since its last revision'."""
    record_user_feedback(tmp_path, _DOC, "needs work")
    assert last_round_timestamp(tmp_path, _DOC) == ""


def test_empty_user_feedback_records_nothing(tmp_path: Path) -> None:
    assert record_user_feedback(tmp_path, _DOC, "   ") == ""
    assert read_findings(tmp_path, _DOC) == []
