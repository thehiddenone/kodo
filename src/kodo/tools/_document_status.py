"""The one place a document's status is merged out of its two stores.

Since findings moved into their own session-scoped store
(:mod:`kodo.findings`, doc/FINDINGS.md), a document's status is no longer a
function of one log's last line. It depends on:

* the document's own **project-scoped** evolution log
  (:mod:`kodo.guided_state`) — has the user approved it, rejected it, or is it
  merely written; and
* the **session-scoped** findings backlog for the same document — is anything
  still outstanding, and has a critic round run since the last revision.

Both the ``guided_dev_status`` tool and the engine's review loop need the
answer, and neither may own it privately, so it lives here — a plain function in
``kodo.tools`` (which both may import) rather than inside either leaf package,
neither of which may import the other.
"""

from __future__ import annotations

from pathlib import Path

from kodo.findings import last_round_timestamp, outstanding_findings, read_findings
from kodo.guided_state import Status, derive_status, read_document_state

__all__ = ["document_status", "status_from_state"]


def status_from_state(
    state: dict[str, object] | None,
    findings_dir: Path | None,
    logical_path: str,
) -> Status:
    """Merge one document's project-log state with its session findings backlog.

    Args:
        state: ``{last_entry, last_revision_ts, last_event}`` from
            :func:`kodo.guided_state.read_document_state` or one
            :func:`~kodo.guided_state.scan_tracked_files` row, or ``None`` for a
            document with no log at all.
        findings_dir: This session's ``findings/`` directory, or ``None`` when
            no session store is available (the backlog then reads as empty).
        logical_path: Folder-prefixed logical path keying the findings log.

    Returns:
        Status: The document's current status.
    """
    last_entry = state.get("last_entry") if state else None
    reviewed = False
    outstanding = 0
    if findings_dir is not None and logical_path:
        findings = read_findings(findings_dir, logical_path)
        outstanding = len(outstanding_findings(findings))
        round_ts = last_round_timestamp(findings_dir, logical_path)
        revision_ts = str(state.get("last_revision_ts", "")) if state else ""
        reviewed = bool(round_ts) and round_ts > revision_ts
    return derive_status(
        last_entry if isinstance(last_entry, dict) else None,
        reviewed=reviewed,
        outstanding=outstanding,
    )


def document_status(
    real_path: Path,
    project_root: Path,
    findings_dir: Path | None,
    logical_path: str,
) -> Status:
    """Read both stores for one document and derive its status.

    Args:
        real_path: The resolved absolute path of the document.
        project_root: The bound root it lives under.
        findings_dir: This session's ``findings/`` directory, or ``None``.
        logical_path: The document's folder-prefixed logical path.

    Returns:
        Status: The document's current status.
    """
    return status_from_state(
        read_document_state(real_path, project_root), findings_dir, logical_path
    )
