"""Unit tests for :mod:`kodo.findings` — the shared author/critic backlog.

The rules these cover are the ones the whole design rests on (doc/FINDINGS.md):
an update carries only what changed, silence changes nothing, ids are never
reissued, and current state is always a replay of the log rather than an index
that could drift from it.

Since 2026-09-04 the backlog is keyed on a **work product** — every file one
review loop produced — rather than on a single document, and a finding carries
a list of ``locations`` rather than one file span. Ids are minted from a
finding's own first location instead of a per-log ``F1``/``F2`` counter, so
nothing here hardcodes one: they are always read back.
"""

from __future__ import annotations

from pathlib import Path

from kodo.findings import (
    STATE_FIXED,
    STATE_OUTSTANDING,
    USER_FEEDBACK_KIND,
    apply_findings,
    close_findings_for_paths,
    findings_log_path,
    last_round_timestamp,
    outstanding_findings,
    read_findings,
    record_user_feedback,
    sort_for_display,
)

# The work product under review, and two of its member files.
_WP = "proj/architect"
_FILE = "proj/specs/architecture.md"
_OTHER = "proj/specs/design/AUTH.md"


def _loc(
    path: str = _FILE, first: int | None = None, last: int | None = None, excerpt: str = ""
) -> dict[str, object]:
    return {"path": path, "first_line": first, "last_line": last, "excerpt": excerpt}


def _apply(
    findings_dir: Path,
    updates: list[dict[str, object]],
    *,
    key: str = _WP,
    reviewer: str = "architect_critic",
):
    return apply_findings(
        findings_dir,
        key,
        reviewer=reviewer,
        updates=updates,
        project="proj",
        agent="architect",
    )


def _ids(findings_dir: Path, key: str = _WP) -> list[str]:
    return [f["id"] for f in read_findings(findings_dir, key)]


def _states(findings_dir: Path, key: str = _WP) -> dict[str, str]:
    return {f["id"]: f["state"] for f in read_findings(findings_dir, key)}


# ---------------------------------------------------------------------------
# findings_log_path
# ---------------------------------------------------------------------------


def test_log_path_mirrors_the_key_under_the_session_findings_dir(tmp_path: Path) -> None:
    assert (
        findings_log_path(tmp_path, "billing-service/coder/AUTH")
        == tmp_path / "billing-service" / "coder" / "AUTH.jsonl"
    )


def test_log_path_refuses_anything_that_could_escape_the_findings_dir(tmp_path: Path) -> None:
    """The key can reach here from agent-supplied data, so a traversal must not
    resolve to a file outside the store."""
    for bad in ("", "   ", "/etc/passwd", "../../secrets.md", "proj/../../x.md", ".."):
        assert findings_log_path(tmp_path, bad) is None, bad
    # A leading "./" is not an escape — it normalises away and stays inside.
    inside = findings_log_path(tmp_path, "./x")
    assert inside is not None and inside.is_relative_to(tmp_path)


def test_log_path_sanitises_an_unsafe_root_segment(tmp_path: Path) -> None:
    """The first segment is a workspace-folder display name, which is not
    guaranteed filesystem-safe — it must be sanitised, not rejected."""
    path = findings_log_path(tmp_path, "my:proj*name/architect/AUTH")
    assert path is not None
    assert path.is_relative_to(tmp_path)
    assert ":" not in path.parts[-3] and "*" not in path.parts[-3]


# ---------------------------------------------------------------------------
# apply_findings — creating, patching, and the round summary
# ---------------------------------------------------------------------------


def test_a_finding_with_no_id_is_created_with_a_freshly_minted_one(tmp_path: Path) -> None:
    summary = _apply(
        tmp_path,
        [
            {
                "kind": "gap",
                "description": "a",
                "locations": [_loc(first=3, last=5, excerpt="x")],
            },
            {"kind": "orphan", "description": "b"},
        ],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 2, 0)
    assert summary.stalled is False
    findings = read_findings(tmp_path, _WP)
    assert findings[0]["kind"] == "gap"
    assert findings[0]["description"] == "a"
    assert findings[0]["locations"] == [
        {"path": _FILE, "first_line": 3, "last_line": 5, "excerpt": "x"}
    ]
    assert findings[0]["state"] == STATE_OUTSTANDING
    assert findings[0]["reported_by"] == "architect_critic"
    # A finding about the work as a whole legitimately has no location.
    assert findings[1]["locations"] == []


def test_an_update_carries_only_what_changed(tmp_path: Path) -> None:
    """The rule the whole protocol rests on: omitted fields keep their values,
    so ``{"id": ..., "state": "fixed"}`` is a complete, correct close."""
    _apply(
        tmp_path,
        [
            {
                "kind": "gap",
                "description": "the settlement path is unowned",
                "locations": [_loc(first=10, last=12, excerpt="…")],
            }
        ],
    )
    finding_id = _ids(tmp_path)[0]

    summary = _apply(tmp_path, [{"id": finding_id, "state": STATE_FIXED}])

    assert (summary.outstanding, summary.opened, summary.closed) == (0, 0, 1)
    finding = read_findings(tmp_path, _WP)[0]
    assert finding["state"] == STATE_FIXED
    # Everything the update did not name survived verbatim.
    assert finding["kind"] == "gap"
    assert finding["description"] == "the settlement path is unowned"
    assert finding["locations"] == [
        {"path": _FILE, "first_line": 10, "last_line": 12, "excerpt": "…"}
    ]


def test_an_update_can_revise_wording_and_locations_without_a_new_identity(
    tmp_path: Path,
) -> None:
    _apply(
        tmp_path,
        [{"kind": "gap", "description": "old", "locations": [_loc(first=1, last=1)]}],
    )
    finding_id = _ids(tmp_path)[0]
    _apply(
        tmp_path,
        [
            {
                "id": finding_id,
                "description": "still wrong, now for another reason",
                "locations": [_loc(first=1, last=9)],
            }
        ],
    )

    findings = read_findings(tmp_path, _WP)
    assert len(findings) == 1  # still one finding, not two
    assert findings[0]["description"] == "still wrong, now for another reason"
    assert findings[0]["locations"][0]["last_line"] == 9
    assert findings[0]["state"] == STATE_OUTSTANDING
    # The id is frozen at creation and does NOT follow the moved span: it is a
    # name, not a position.
    assert findings[0]["id"] == finding_id


def test_resending_locations_replaces_the_list_rather_than_merging_it(tmp_path: Path) -> None:
    """A critic re-reporting locations has re-read the files and is describing
    where the problem is *now*; merging would resurrect spans it dropped."""
    _apply(
        tmp_path,
        [
            {
                "kind": "interface_drift",
                "description": "signature vs call site",
                "locations": [_loc(first=4), _loc(path=_OTHER, first=90)],
            }
        ],
    )
    finding_id = _ids(tmp_path)[0]

    _apply(tmp_path, [{"id": finding_id, "locations": [_loc(path=_OTHER, first=90)]}])

    assert read_findings(tmp_path, _WP)[0]["locations"] == [
        {"path": _OTHER, "first_line": 90, "last_line": None, "excerpt": ""}
    ]


def test_one_finding_can_span_several_files(tmp_path: Path) -> None:
    """The defect a work-product review exists to catch: a single problem whose
    two halves live in different files, filed as ONE finding."""
    _apply(
        tmp_path,
        [
            {
                "kind": "interface_drift",
                "description": "add_score(name, score) is called as (score, name)",
                "locations": [_loc(first=42, last=42), _loc(path=_OTHER, first=118, last=118)],
            }
        ],
    )

    findings = read_findings(tmp_path, _WP)
    assert len(findings) == 1
    assert [loc["path"] for loc in findings[0]["locations"]] == [_FILE, _OTHER]


def test_malformed_locations_degrade_rather_than_break_the_round(tmp_path: Path) -> None:
    """`locations` is model-authored: a non-list, a non-dict element, or an
    entry with no path must not take down the whole update."""
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "a", "locations": "not-a-list"},
            {"kind": "gap", "description": "b", "locations": ["nope", {"first_line": 3}]},
            {
                "kind": "gap",
                "description": "c",
                "locations": [{"path": _FILE, "first_line": "eight"}],
            },
        ],
    )

    findings = read_findings(tmp_path, _WP)
    assert findings[0]["locations"] == []
    assert findings[1]["locations"] == []  # no path → not a usable location
    assert findings[2]["locations"] == [
        {"path": _FILE, "first_line": None, "last_line": None, "excerpt": ""}
    ]


def test_a_finding_the_round_never_mentions_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    """Silence closes nothing — the cost of a critic overlooking a finding is a
    wasted round, never a defect recorded as fixed."""
    _apply(
        tmp_path,
        [{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )

    summary = _apply(tmp_path, [])

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 0, 0)
    assert summary.stalled is True
    assert set(_states(tmp_path).values()) == {STATE_OUTSTANDING}


def test_a_round_that_closes_and_opens_in_equal_numbers_is_not_stalled(tmp_path: Path) -> None:
    """The exact case the retired count heuristic got wrong."""
    _apply(
        tmp_path,
        [{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )
    first, second = _ids(tmp_path)

    summary = _apply(
        tmp_path,
        [
            {"id": first, "state": STATE_FIXED},
            {"id": second, "state": STATE_FIXED},
            {"kind": "gap", "description": "c"},
            {"kind": "gap", "description": "d"},
        ],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (2, 2, 2)
    assert summary.stalled is False


def test_reopening_a_closed_finding_reuses_its_identity(tmp_path: Path) -> None:
    _apply(tmp_path, [{"kind": "gap", "description": "a"}])
    finding_id = _ids(tmp_path)[0]
    _apply(tmp_path, [{"id": finding_id, "state": STATE_FIXED}])

    summary = _apply(
        tmp_path,
        [{"id": finding_id, "state": STATE_OUTSTANDING, "description": "the fix regressed"}],
    )

    assert (summary.outstanding, summary.opened, summary.closed) == (1, 0, 0)
    assert _states(tmp_path) == {finding_id: STATE_OUTSTANDING}


def test_closing_an_already_closed_finding_is_not_counted_twice(tmp_path: Path) -> None:
    _apply(tmp_path, [{"kind": "gap", "description": "a"}])
    finding_id = _ids(tmp_path)[0]
    _apply(tmp_path, [{"id": finding_id, "state": STATE_FIXED}])

    summary = _apply(tmp_path, [{"id": finding_id, "state": STATE_FIXED}])

    assert summary.closed == 0
    assert summary.stalled is True


def test_an_unknown_id_is_treated_as_a_new_finding_rather_than_lost(tmp_path: Path) -> None:
    """A critic that invents an id must not write into nothing — the finding is
    still recorded, under an id the store actually controls."""
    summary = _apply(tmp_path, [{"id": "made-up", "kind": "gap", "description": "invented id"}])

    assert summary.opened == 1
    assert _ids(tmp_path) != ["made-up"]
    assert len(_ids(tmp_path)) == 1


def test_an_invalid_state_value_falls_back_to_outstanding(tmp_path: Path) -> None:
    """A third state would put the store somewhere nothing can read, so the
    store coerces rather than trusting the model's string."""
    _apply(tmp_path, [{"kind": "gap", "description": "a", "state": "mostly_fixed"}])
    assert list(_states(tmp_path).values()) == [STATE_OUTSTANDING]


# ---------------------------------------------------------------------------
# id minting
# ---------------------------------------------------------------------------


def test_ids_describe_the_work_product_and_the_finding_s_first_location(
    tmp_path: Path,
) -> None:
    """``<project>_<agent>[_<responsibility>]_<file>_<line>`` — readable at a
    glance and unique session-wide, which the old per-log ``F1`` never was."""
    apply_findings(
        tmp_path,
        "proj/coder/LEADERBOARD",
        reviewer="code_critic",
        updates=[
            {
                "kind": "anti_pattern",
                "description": "x",
                "locations": [_loc(path="proj/src/leaderboard.py", first=42, last=48)],
            }
        ],
        project="proj",
        agent="coder",
        responsibility_code="LEADERBOARD",
    )

    assert _ids(tmp_path, "proj/coder/LEADERBOARD") == ["proj_coder_LEADERBOARD_leaderboard.py_42"]


def test_the_basename_is_used_not_the_whole_logical_path(tmp_path: Path) -> None:
    """The full path is already in `locations`; spelling it into the id would
    produce an unreadable string for no gain."""
    _apply(
        tmp_path,
        [{"kind": "gap", "description": "x", "locations": [_loc(path=_OTHER, first=7)]}],
    )
    assert _ids(tmp_path) == ["proj_architect_AUTH.md_7"]


def test_an_id_collision_falls_back_to_another_line_the_finding_covers(
    tmp_path: Path,
) -> None:
    """Two findings on overlapping spans still get distinct, still-meaningful
    ids rather than a counter suffix."""
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "a", "locations": [_loc(first=10, last=12)]},
            {"kind": "gap", "description": "b", "locations": [_loc(first=10, last=12)]},
            {"kind": "gap", "description": "c", "locations": [_loc(first=10, last=12)]},
        ],
    )

    assert _ids(tmp_path) == [
        "proj_architect_architecture.md_10",
        "proj_architect_architecture.md_11",
        "proj_architect_architecture.md_12",
    ]


def test_a_finding_with_no_usable_line_still_gets_a_unique_id(tmp_path: Path) -> None:
    """A finding about the work as a whole has no line to name, and two of them
    must still not collide."""
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "a"},
            {"kind": "gap", "description": "b"},
        ],
    )
    ids = _ids(tmp_path)
    assert ids == ["proj_architect", "proj_architect_2"]


def test_ids_are_never_reissued_within_a_work_product(tmp_path: Path) -> None:
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "a", "locations": [_loc(first=1)]},
            {"kind": "gap", "description": "b", "locations": [_loc(first=2)]},
        ],
    )
    first = _ids(tmp_path)[0]
    _apply(tmp_path, [{"id": first, "state": STATE_FIXED}])
    # A third finding never reuses the closed one's id, even on its line.
    _apply(tmp_path, [{"kind": "gap", "description": "c", "locations": [_loc(first=1)]}])

    ids = _ids(tmp_path)
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_two_work_products_keep_separate_backlogs(tmp_path: Path) -> None:
    _apply(tmp_path, [{"kind": "gap", "description": "a"}])
    apply_findings(
        tmp_path,
        "proj/requirements_author",
        reviewer="requirements_critic",
        updates=[{"kind": "ambiguity", "description": "z"}],
        project="proj",
        agent="requirements_author",
    )

    assert len(_ids(tmp_path)) == 1
    assert len(_ids(tmp_path, "proj/requirements_author")) == 1
    # …and the ids say which work product they belong to.
    assert _ids(tmp_path)[0].startswith("proj_architect")
    assert _ids(tmp_path, "proj/requirements_author")[0].startswith("proj_requirements_author")


def test_state_survives_a_fresh_read_because_it_is_replayed_from_disk(tmp_path: Path) -> None:
    _apply(
        tmp_path,
        [{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
    )
    first, second = _ids(tmp_path)
    _apply(tmp_path, [{"id": second, "state": STATE_FIXED}])

    # Nothing in memory carries over — this is a cold read of the log.
    assert _states(tmp_path) == {first: STATE_OUTSTANDING, second: STATE_FIXED}
    assert [f["id"] for f in outstanding_findings(read_findings(tmp_path, _WP))] == [first]


def test_apply_findings_rejects_an_unusable_key(tmp_path: Path) -> None:
    try:
        apply_findings(tmp_path, "../escape", reviewer="x", updates=[])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a key that cannot be mapped")


# ---------------------------------------------------------------------------
# auto-closing findings for a file that left the work product
# ---------------------------------------------------------------------------


def test_findings_for_a_removed_file_are_auto_closed(tmp_path: Path) -> None:
    """Nothing will re-read a file that is no longer part of the work product,
    so an outstanding finding against it could never be verified fixed and
    would block the loop forever."""
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "on the removed file", "locations": [_loc(path=_OTHER)]},
            {"kind": "gap", "description": "on the kept file", "locations": [_loc(path=_FILE)]},
        ],
    )
    doomed, kept = _ids(tmp_path)

    closed = close_findings_for_paths(tmp_path, _WP, (_OTHER,))

    assert closed == [doomed]
    assert _states(tmp_path) == {doomed: STATE_FIXED, kept: STATE_OUTSTANDING}


def test_a_cross_file_finding_survives_when_only_one_side_is_removed(tmp_path: Path) -> None:
    """Closing it would discard exactly the defect the multi-file model exists
    to catch, while its other half is still present and still wrong."""
    _apply(
        tmp_path,
        [
            {
                "kind": "interface_drift",
                "description": "two sides",
                "locations": [_loc(path=_FILE), _loc(path=_OTHER)],
            }
        ],
    )
    finding_id = _ids(tmp_path)[0]

    assert close_findings_for_paths(tmp_path, _WP, (_OTHER,)) == []
    assert _states(tmp_path) == {finding_id: STATE_OUTSTANDING}
    # Once BOTH sides are gone there is nothing left to fix.
    assert close_findings_for_paths(tmp_path, _WP, (_FILE, _OTHER)) == [finding_id]


def test_auto_close_leaves_already_closed_and_unlocated_findings_alone(tmp_path: Path) -> None:
    """A finding with no locations is about the work as a whole and does not
    belong to any one file, so removing a file must not close it."""
    _apply(
        tmp_path,
        [
            {"kind": "gap", "description": "whole-set concern"},
            {"kind": "gap", "description": "on the file", "locations": [_loc(path=_OTHER)]},
        ],
    )
    whole, on_file = _ids(tmp_path)
    _apply(tmp_path, [{"id": on_file, "state": STATE_FIXED}])

    assert close_findings_for_paths(tmp_path, _WP, (_OTHER,)) == []
    assert _states(tmp_path)[whole] == STATE_OUTSTANDING


def test_auto_close_writes_no_review_round(tmp_path: Path) -> None:
    """Bookkeeping, not a review — a removed file must not make the work
    product look 'reviewed since its last revision'."""
    _apply(tmp_path, [{"kind": "gap", "description": "a", "locations": [_loc(path=_OTHER)]}])
    before = last_round_timestamp(tmp_path, _WP)

    close_findings_for_paths(tmp_path, _WP, (_OTHER,))

    assert last_round_timestamp(tmp_path, _WP) == before


def test_auto_close_is_a_noop_with_nothing_removed_or_an_unusable_key(tmp_path: Path) -> None:
    _apply(tmp_path, [{"kind": "gap", "description": "a", "locations": [_loc()]}])
    assert close_findings_for_paths(tmp_path, _WP, ()) == []
    assert close_findings_for_paths(tmp_path, "../escape", (_FILE,)) == []


# ---------------------------------------------------------------------------
# review rounds
# ---------------------------------------------------------------------------


def test_every_round_is_recorded_even_when_it_changed_nothing(tmp_path: Path) -> None:
    """The round marker is what makes "reviewed since the last revision"
    answerable, so it must be written whether or not any finding moved."""
    assert last_round_timestamp(tmp_path, _WP) == ""

    _apply(tmp_path, [])
    first = last_round_timestamp(tmp_path, _WP)
    assert first

    _apply(tmp_path, [])
    assert last_round_timestamp(tmp_path, _WP) >= first


def test_reading_a_work_product_with_no_log_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_findings(tmp_path, "proj/never-reviewed") == []
    assert last_round_timestamp(tmp_path, "proj/never-reviewed") == ""
    assert read_findings(tmp_path, "../escape") == []


# ---------------------------------------------------------------------------
# the user's rejection
# ---------------------------------------------------------------------------


def test_user_feedback_becomes_an_outstanding_finding_in_the_same_backlog(
    tmp_path: Path,
) -> None:
    _apply(tmp_path, [{"kind": "gap", "description": "a"}])

    finding_id = record_user_feedback(
        tmp_path,
        _WP,
        "  the North Star is missing  ",
        path=_FILE,
        project="proj",
        agent="architect",
    )

    findings = read_findings(tmp_path, _WP)
    assert finding_id and finding_id == findings[1]["id"]
    assert findings[1]["kind"] == USER_FEEDBACK_KIND
    assert findings[1]["reported_by"] == "user"
    assert findings[1]["description"] == "the North Star is missing"
    assert findings[1]["state"] == STATE_OUTSTANDING
    # Anchored to the member the user was looking at, so the author knows
    # which file to revisit.
    assert findings[1]["locations"] == [
        {"path": _FILE, "first_line": None, "last_line": None, "excerpt": ""}
    ]


def test_user_feedback_about_the_whole_set_carries_no_location(tmp_path: Path) -> None:
    finding_id = record_user_feedback(
        tmp_path, _WP, "these do not hang together", project="proj", agent="architect"
    )
    assert finding_id
    assert read_findings(tmp_path, _WP)[0]["locations"] == []


def test_user_feedback_does_not_count_as_a_review_round(tmp_path: Path) -> None:
    """The user is not a critic: their rejection must not make a work product
    look 'reviewed since its last revision'."""
    record_user_feedback(tmp_path, _WP, "needs work", project="proj", agent="architect")
    assert last_round_timestamp(tmp_path, _WP) == ""


def test_empty_user_feedback_records_nothing(tmp_path: Path) -> None:
    assert record_user_feedback(tmp_path, _WP, "   ") == ""
    assert read_findings(tmp_path, _WP) == []


# ---------------------------------------------------------------------------
# sort_for_display — the order the user's findings table reads in
# ---------------------------------------------------------------------------


def _finding(
    finding_id: str, *, state: str = STATE_OUTSTANDING, locations: list[dict[str, object]] | None
) -> dict[str, object]:
    return {
        "id": finding_id,
        "kind": "gap",
        "description": "…",
        "locations": locations if locations is not None else [],
        "state": state,
        "reported_by": "architect_critic",
    }


def test_display_order_puts_outstanding_above_fixed(tmp_path: Path) -> None:
    """The top of the table is the work still to do; the bottom is the record of
    what the loop has closed."""
    findings = [
        _finding("f_fixed", state=STATE_FIXED, locations=[_loc("proj/a.md", 1)]),
        _finding("f_open", locations=[_loc("proj/z.md", 99)]),
    ]
    assert [f["id"] for f in sort_for_display(findings)] == ["f_open", "f_fixed"]


def test_display_order_is_by_path_then_line_within_a_state(tmp_path: Path) -> None:
    findings = [
        _finding("b_md_2", locations=[_loc("proj/b.md", 2)]),
        _finding("a_md_40", locations=[_loc("proj/a.md", 40)]),
        _finding("a_md_7", locations=[_loc("proj/a.md", 7)]),
    ]
    # Line 7 before line 40: ordered numerically, not by the id's own text.
    assert [f["id"] for f in sort_for_display(findings)] == ["a_md_7", "a_md_40", "b_md_2"]


def test_a_multi_location_finding_is_placed_by_its_first_location(tmp_path: Path) -> None:
    """One row per finding, never one per location: `locations` is a list
    precisely so a cross-file defect stays a single finding."""
    cross_file = _finding("f_cross", locations=[_loc("proj/a.md", 5), _loc("proj/z.md", 1)])
    later = _finding("f_later", locations=[_loc("proj/m.md", 1)])

    ordered = sort_for_display([later, cross_file])

    assert [f["id"] for f in ordered] == ["f_cross", "f_later"]
    assert len(ordered) == 2


def test_a_finding_with_no_location_or_line_still_sorts(tmp_path: Path) -> None:
    """The user's own rejection about a set as a whole is anchored nowhere, and
    a critic may raise a document-wide finding with no line."""
    findings = [
        _finding("f_lined", locations=[_loc("proj/a.md", 3)]),
        _finding("f_unlined", locations=[_loc("proj/a.md")]),
        _finding("f_unanchored", locations=[]),
    ]
    assert [f["id"] for f in sort_for_display(findings)] == [
        "f_unanchored",
        "f_unlined",
        "f_lined",
    ]


def test_display_order_is_total_so_it_does_not_shuffle_between_rounds(tmp_path: Path) -> None:
    """Two findings on the same line would otherwise swap places between
    emissions for no reason the reader can see."""
    same_spot = [
        _finding("f_b", locations=[_loc("proj/a.md", 3)]),
        _finding("f_a", locations=[_loc("proj/a.md", 3)]),
    ]
    assert [f["id"] for f in sort_for_display(same_spot)] == ["f_a", "f_b"]
    assert [f["id"] for f in sort_for_display(list(reversed(same_spot)))] == ["f_a", "f_b"]
