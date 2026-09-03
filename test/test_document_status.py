"""Tests for :func:`kodo.tools.document_status` — the two-store merge seam.

Since findings moved into their own session-scoped store, a document's status is
no longer a function of one log's last line: it depends on the project-scoped
evolution log *and* this session's findings backlog for the same document
(doc/FINDINGS.md §6). Both the ``guided_dev_status`` tool and the engine's review
loop go through this one function, so these tests are the single place the rule
itself is pinned.
"""

from __future__ import annotations

from pathlib import Path

from kodo.findings import apply_findings, record_user_feedback
from kodo.guided_state import append_accepted, append_new_revision, append_review_result
from kodo.tools import document_status

_LOGICAL = "proj/specs/architecture.md"


def _doc(root: Path) -> Path:
    path = root / "specs" / "architecture.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content", encoding="utf-8")
    return path


def _revise(root: Path, sha: str = "sha") -> Path:
    doc = _doc(root)
    append_new_revision(
        doc,
        root,
        commit_hash=sha,
        author="architect",
        tool="edit_file",
        summary="write",
        workflow="guided",
    )
    return doc


def _status(root: Path, findings_dir: Path | None) -> str:
    return document_status(_doc(root), root, findings_dir, _LOGICAL)


def test_written_but_never_reviewed_is_pending_review(tmp_path: Path) -> None:
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    _revise(root)
    assert _status(root, findings) == "pending_review"


def test_an_outstanding_finding_means_needs_revision(tmp_path: Path) -> None:
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    _revise(root)
    apply_findings(
        findings,
        _LOGICAL,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "x"}],
    )
    assert _status(root, findings) == "needs_revision"


def test_a_clean_review_since_the_last_revision_is_pending_acceptance(tmp_path: Path) -> None:
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    _revise(root)
    apply_findings(findings, _LOGICAL, reviewer="architect_critic", updates=[])
    assert _status(root, findings) == "pending_acceptance"


def test_a_revision_after_a_clean_review_goes_back_to_pending_review(tmp_path: Path) -> None:
    """The exact question ``reviewed`` exists to answer: an empty backlog means
    "reviewed clean" only if the review is *newer* than the revision."""
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    _revise(root, sha="sha-1")
    apply_findings(findings, _LOGICAL, reviewer="architect_critic", updates=[])
    assert _status(root, findings) == "pending_acceptance"

    _revise(root, sha="sha-2")
    assert _status(root, findings) == "pending_review"


def test_the_users_rejection_outranks_an_empty_backlog(tmp_path: Path) -> None:
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    doc = _revise(root)
    apply_findings(findings, _LOGICAL, reviewer="architect_critic", updates=[])
    append_review_result(doc, root, decision="reject", comment="no")
    assert _status(root, findings) == "needs_revision"


def test_the_users_feedback_finding_alone_puts_it_back_in_revision(tmp_path: Path) -> None:
    """The rejection reaches the author as a finding, and that finding is enough
    on its own to keep the document unsettled."""
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    _revise(root)
    apply_findings(findings, _LOGICAL, reviewer="architect_critic", updates=[])
    record_user_feedback(findings, _LOGICAL, "needs a North Star")
    assert _status(root, findings) == "needs_revision"


def test_accepted_is_terminal_regardless_of_the_backlog(tmp_path: Path) -> None:
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    doc = _revise(root)
    append_accepted(doc, root)
    apply_findings(
        findings,
        _LOGICAL,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "x"}],
    )
    assert _status(root, findings) == "accepted"


def test_no_session_store_reads_the_backlog_as_empty(tmp_path: Path) -> None:
    """Findings are session-scoped, so a document reviewed in another session
    has no backlog here — it reads as never-reviewed rather than inheriting a
    verdict this session cannot see (doc/FINDINGS.md §2)."""
    root = tmp_path / "p"
    root.mkdir()
    _revise(root)
    assert _status(root, None) == "pending_review"


def test_an_untracked_or_unwritten_document_still_answers(tmp_path: Path) -> None:
    """A document with no evolution log at all: the backlog alone decides, so a
    caller never has to special-case "no log yet"."""
    root, findings = tmp_path / "p", tmp_path / "f"
    root.mkdir()
    assert _status(root, findings) == "pending_review"
    apply_findings(
        findings,
        _LOGICAL,
        reviewer="architect_critic",
        updates=[{"kind": "gap", "description": "x"}],
    )
    assert _status(root, findings) == "needs_revision"
