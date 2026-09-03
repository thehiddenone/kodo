"""Unit tests for :mod:`kodo.guided_state` — the per-document evolution log.

Replaces the artifact index: nothing is held in memory; the log on disk is the
whole record. Since review content moved to :mod:`kodo.findings`, this log holds
only ``new_revision``/``review_result``/``accepted``, and a document's *status*
is a function of both stores — :func:`~kodo.guided_state.derive_status` takes the
findings half as arguments, and the merge itself is tested in
``test_document_status.py``.
"""

from __future__ import annotations

from pathlib import Path

from kodo.guided_state import (
    append_accepted,
    append_new_revision,
    append_review_result,
    derive_status,
    is_tracked,
    last_revision_timestamp,
    read_document_state,
    read_history,
    scan_tracked_files,
    shadow_path,
)


def _status(doc: Path, root: Path, *, reviewed: bool = False, outstanding: int = 0) -> str:
    """Derive *doc*'s status with an explicit findings half."""
    state = read_document_state(doc, root)
    last = state["last_entry"] if state else None
    return derive_status(
        last if isinstance(last, dict) else None, reviewed=reviewed, outstanding=outstanding
    )


# ---------------------------------------------------------------------------
# shadow_path / is_tracked
# ---------------------------------------------------------------------------


def test_shadow_path_maps_specs_src_test_under_kodo_guided_dev_state(tmp_path: Path) -> None:
    for root_name in ("specs", "src", "test"):
        real = tmp_path / root_name / "sub" / "doc.md"
        shadow = shadow_path(real, tmp_path)
        assert (
            shadow == tmp_path / ".kodo" / "guided_dev_state" / root_name / "sub" / "doc.md.jsonl"
        )


def test_shadow_path_returns_none_outside_tracked_roots(tmp_path: Path) -> None:
    assert shadow_path(tmp_path / "README.md", tmp_path) is None
    assert shadow_path(tmp_path / "scripts" / "build.sh", tmp_path) is None


def test_is_tracked_matches_shadow_path(tmp_path: Path) -> None:
    assert is_tracked(tmp_path / "specs" / "a.md", tmp_path) is True
    assert is_tracked(tmp_path / "a.md", tmp_path) is False


# ---------------------------------------------------------------------------
# append_new_revision / read_history / read_document_state
# ---------------------------------------------------------------------------


def test_append_new_revision_is_a_noop_outside_tracked_roots(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha",
        author="a",
        tool="filesystem",
        summary="s",
        workflow="guided",
    )
    assert read_history(doc, tmp_path) == []
    assert read_document_state(doc, tmp_path) is None


def test_read_document_state_is_none_for_untouched_tracked_file(tmp_path: Path) -> None:
    doc = tmp_path / "specs" / "a.md"
    assert read_document_state(doc, tmp_path) is None


def test_new_revision_entry_carries_commit_and_workflow(tmp_path: Path) -> None:
    doc = tmp_path / "specs" / "a.md"
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha1",
        author="architect",
        tool="filesystem",
        summary="create_file",
        workflow="guided",
    )
    history = read_history(doc, tmp_path)
    assert len(history) == 1
    entry = history[0]
    assert entry["type"] == "new_revision"
    assert entry["commit_hash"] == "sha1"
    assert entry["author"] == "architect"
    assert entry["tool"] == "filesystem"
    assert entry["workflow"] == "guided"
    assert entry["timestamp"]

    state = read_document_state(doc, tmp_path)
    assert state is not None
    assert state["last_revision_ts"] == entry["timestamp"]
    assert _status(doc, tmp_path) == "pending_review"


def test_new_revision_tags_problem_solving_writes_distinctly(tmp_path: Path) -> None:
    """A Problem-Solver edit of a tracked file is still recorded, tagged apart.

    The point: the Guide can reconcile state after a Problem-Solver session
    touched a tracked document, without that write being mistaken for a
    Guided-mode author turn.
    """
    doc = tmp_path / "src" / "a.py"
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha2",
        author="problem_solver",
        tool="edit_file",
        summary="edit",
        workflow="problem_solving",
    )
    history = read_history(doc, tmp_path)
    assert history[0]["workflow"] == "problem_solving"
    # Still just a new_revision — no review_result/accepted appear outside
    # Guided mode, because nothing in that flow ever fires there.
    assert [e["type"] for e in history] == ["new_revision"]


# ---------------------------------------------------------------------------
# Status derivation across the full state machine
# ---------------------------------------------------------------------------


def test_status_derivation_full_lifecycle(tmp_path: Path) -> None:
    """The whole state machine, with the findings half supplied per step.

    ``reviewed``/``outstanding`` are what a caller reads off the session findings
    store; here they are passed literally so this test covers the *rule* rather
    than the plumbing.
    """
    doc = tmp_path / "specs" / "a.md"

    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha-a",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    # Written, never looked at.
    assert _status(doc, tmp_path) == "pending_review"

    # A critic round raised two findings.
    assert _status(doc, tmp_path, reviewed=True, outstanding=2) == "needs_revision"

    # Author revises — a fresh commit, new sha. The findings are still open, so
    # the document is still in revision, not waiting on a first look.
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha-b",
        author="architect",
        tool="edit_file",
        summary="revise",
        workflow="guided",
    )
    assert _status(doc, tmp_path, outstanding=2) == "needs_revision"

    # The critic re-reviewed and closed both: reviewed since the last revision,
    # nothing outstanding.
    assert _status(doc, tmp_path, reviewed=True, outstanding=0) == "pending_acceptance"

    # Interactive mode: the user rejects. That decision outranks the backlog.
    append_review_result(doc, tmp_path, decision="reject", comment="not quite")
    assert _status(doc, tmp_path, reviewed=True, outstanding=0) == "needs_revision"

    # Author revises again, critic clears it again, user approves this time.
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha-c",
        author="architect",
        tool="edit_file",
        summary="revise again",
        workflow="guided",
    )
    append_review_result(doc, tmp_path, decision="approve", comment="")
    assert _status(doc, tmp_path, reviewed=True) == "pending_acceptance"

    append_accepted(doc, tmp_path)
    # Terminal: the backlog no longer has any say.
    assert _status(doc, tmp_path, outstanding=3) == "accepted"
    final = read_history(doc, tmp_path)[-1]
    # Acceptance never creates a new commit — it reuses the latest new_revision's.
    assert final["commit_hash"] == "sha-c"


def test_reviewed_clean_and_never_reviewed_are_distinguished(tmp_path: Path) -> None:
    """The one thing ``reviewed`` exists for: an empty backlog means two very
    different things before and after a critic has looked at the file."""
    doc = tmp_path / "specs" / "a.md"
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    assert _status(doc, tmp_path, reviewed=False, outstanding=0) == "pending_review"
    assert _status(doc, tmp_path, reviewed=True, outstanding=0) == "pending_acceptance"


def test_a_legacy_feedback_entry_is_ignored_rather_than_interpreted(tmp_path: Path) -> None:
    """A log written by an older build can still hold a ``feedback`` line. It
    must not be read as a verdict — it falls through to the same branch as a
    ``new_revision``, so the current stores decide."""
    assert derive_status({"type": "feedback", "accept": True}) == "pending_review"
    assert derive_status({"type": "feedback", "accept": True}, outstanding=1) == "needs_revision"
    assert (
        derive_status({"type": "feedback", "accept": False}, reviewed=True) == "pending_acceptance"
    )


def test_last_revision_timestamp_finds_the_most_recent_revision(tmp_path: Path) -> None:
    doc = tmp_path / "specs" / "a.md"
    assert last_revision_timestamp([]) == ""
    for sha in ("sha-a", "sha-b"):
        append_new_revision(
            doc,
            tmp_path,
            commit_hash=sha,
            author="architect",
            tool="edit_file",
            summary="x",
            workflow="guided",
        )
    append_review_result(doc, tmp_path, decision="reject", comment="")
    history = read_history(doc, tmp_path)
    # The last new_revision, not the last entry.
    assert last_revision_timestamp(history) == history[1]["timestamp"]


def test_append_accepted_reuses_most_recent_new_revision_commit(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "specs" / "a.md"
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="first-sha",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    append_review_result(doc, tmp_path, decision="approve", comment="")
    # No further new_revision before accepted — must still find "first-sha".
    append_accepted(doc, tmp_path)
    final = read_history(doc, tmp_path)[-1]
    assert final["commit_hash"] == "first-sha"


def test_review_result_raises_for_untracked_path(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    try:
        append_review_result(doc, tmp_path, decision="approve", comment="")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an untracked path")


# ---------------------------------------------------------------------------
# scan_tracked_files
# ---------------------------------------------------------------------------


def test_scan_tracked_files_returns_empty_list_when_nothing_tracked(tmp_path: Path) -> None:
    assert scan_tracked_files(tmp_path) == []


def test_scan_tracked_files_reports_every_tracked_document(tmp_path: Path) -> None:
    doc_a = tmp_path / "specs" / "a.md"
    doc_b = tmp_path / "src" / "comp" / "b.py"
    append_new_revision(
        doc_a,
        tmp_path,
        commit_hash="sha-a",
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    append_new_revision(
        doc_b,
        tmp_path,
        commit_hash="sha-b",
        author="coder",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    append_accepted(doc_a, tmp_path)

    rows = {str(r["path"]): r for r in scan_tracked_files(tmp_path)}
    assert set(rows) == {"specs/a.md", "src/comp/b.py"}
    # The scan reports raw inputs, not a status — deriving one needs the findings
    # store too, which this package deliberately knows nothing about.
    assert rows["specs/a.md"]["last_entry"]["type"] == "accepted"  # type: ignore[index]
    assert rows["src/comp/b.py"]["last_entry"]["type"] == "new_revision"  # type: ignore[index]
    assert rows["src/comp/b.py"]["last_revision_ts"]
