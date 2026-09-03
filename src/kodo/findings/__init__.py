"""Per-session findings — the shared author/critic backlog (doc/FINDINGS.md).

A **finding** is one defect a critic raised against one document, with an
identity that survives the round it was raised in and a state that is either
``outstanding`` or ``fixed``. Both halves of an author/critic loop read the
backlog through the ``get_findings`` tool; the critic alone writes to it, via
its own ``return_result``.

Storage is **session**-scoped (``<session-dir>/findings/<logical path>.jsonl``),
not project-scoped: two sessions may review the same tree under different
models and settings, so a backlog is a fact about a session's review rather
than about the project. What survives across sessions is the document's own
project-scoped evolution log (:mod:`kodo.guided_state`), which this package does
not touch.

Like ``guided_state``, this is a leaf package of plain functions with no
in-memory index — current state is always a replay of the log.
"""

from ._paths import findings_log_path
from ._records import (
    FINDING_FIELDS,
    STATE_FIXED,
    STATE_OUTSTANDING,
    Finding,
    FindingState,
    RoundSummary,
)
from ._store import (
    USER_FEEDBACK_KIND,
    apply_findings,
    last_round_timestamp,
    outstanding_findings,
    read_findings,
    record_user_feedback,
)

__all__ = [
    "FINDING_FIELDS",
    "STATE_FIXED",
    "STATE_OUTSTANDING",
    "USER_FEEDBACK_KIND",
    "Finding",
    "FindingState",
    "RoundSummary",
    "apply_findings",
    "findings_log_path",
    "last_round_timestamp",
    "outstanding_findings",
    "read_findings",
    "record_user_feedback",
]
