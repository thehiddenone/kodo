"""Unit tests for :mod:`kodo.guided_state` — the per-document evolution log.

Replaces the artifact index: a document's current state is always derived
from the last line of its ``.jsonl`` log, never from any in-memory store.
"""

from __future__ import annotations

from pathlib import Path

from kodo.guided_state import (
    append_accepted,
    append_feedback,
    append_new_revision,
    append_review_result,
    is_tracked,
    read_history,
    read_status,
    scan_tracked_files,
    shadow_path,
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
# append_new_revision / read_history / read_status
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
    assert read_status(doc, tmp_path) is None


def test_read_status_is_none_for_untouched_tracked_file(tmp_path: Path) -> None:
    doc = tmp_path / "specs" / "a.md"
    assert read_status(doc, tmp_path) is None


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

    status = read_status(doc, tmp_path)
    assert status is not None
    assert status["status"] == "pending_review"


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
    # Still just a new_revision — no feedback/review_result/accepted appear
    # outside Guided mode, because nothing in that flow ever fires there.
    assert [e["type"] for e in history] == ["new_revision"]


# ---------------------------------------------------------------------------
# Status derivation across the full state machine
# ---------------------------------------------------------------------------


def test_status_derivation_full_lifecycle(tmp_path: Path) -> None:
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
    assert read_status(doc, tmp_path)["status"] == "pending_review"  # type: ignore[index]

    append_feedback(
        doc,
        tmp_path,
        reviewer="architect_critic",
        accept=False,
        concerns=[{"kind": "gap", "description": "x"}],
        summary="rejected",
    )
    assert read_status(doc, tmp_path)["status"] == "needs_revision"  # type: ignore[index]

    # Author revises — a fresh commit, new sha.
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha-b",
        author="architect",
        tool="edit_file",
        summary="revise",
        workflow="guided",
    )
    assert read_status(doc, tmp_path)["status"] == "pending_review"  # type: ignore[index]

    append_feedback(
        doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
    )
    assert read_status(doc, tmp_path)["status"] == "pending_acceptance"  # type: ignore[index]

    # Interactive mode: user rejects.
    append_review_result(doc, tmp_path, decision="reject", comment="not quite")
    assert read_status(doc, tmp_path)["status"] == "needs_revision"  # type: ignore[index]

    # Author revises again, critic accepts again, user approves this time.
    append_new_revision(
        doc,
        tmp_path,
        commit_hash="sha-c",
        author="architect",
        tool="edit_file",
        summary="revise again",
        workflow="guided",
    )
    append_feedback(
        doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
    )
    append_review_result(doc, tmp_path, decision="approve", comment="")
    assert read_status(doc, tmp_path)["status"] == "pending_acceptance"  # type: ignore[index]

    append_accepted(doc, tmp_path)
    final = read_status(doc, tmp_path)
    assert final is not None
    assert final["status"] == "accepted"
    # Acceptance never creates a new commit — it reuses the latest new_revision's.
    assert final["commit_hash"] == "sha-c"


def test_append_accepted_reuses_most_recent_new_revision_commit_even_through_feedback(
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
    append_feedback(
        doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
    )
    # No further new_revision before accepted — must still find "first-sha".
    append_accepted(doc, tmp_path)
    final = read_history(doc, tmp_path)[-1]
    assert final["commit_hash"] == "first-sha"


def test_append_feedback_and_review_result_raise_for_untracked_path(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    try:
        append_feedback(doc, tmp_path, reviewer="x", accept=True, concerns=[], summary="")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an untracked path")

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

    results = {r["path"]: r["status"] for r in scan_tracked_files(tmp_path)}
    assert results == {"specs/a.md": "accepted", "src/comp/b.py": "pending_review"}
