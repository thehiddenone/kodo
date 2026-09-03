"""Per-document evolution tracking for Guided mode — append-only ``.jsonl`` logs.

Replaces the old ``kodo.workspace`` artifact-staging system. Authors write
real files directly with ``filesystem``/``edit_file``/``create_file``; the
engine records a ``new_revision`` entry (with the resulting mirror-commit sha)
right after.
The engine alone writes ``review_result`` (the user's decision) and
``accepted`` (the final marker) — no dispatched tool ever produces either.

Review *content* no longer lives here at all: the old ``feedback`` entry
(``{accept, concerns}``) is gone, replaced by the identified, stateful,
session-scoped backlog in :mod:`kodo.findings` (doc/FINDINGS.md). A document's
status is therefore a function of both stores; :func:`derive_status` takes the
findings half as arguments, and :func:`kodo.tools.document_status` is the one
place that merges them. There is still no in-memory index to rebuild at
bootstrap.

Storage convention: ``<root>/specs/foo/bar.md`` ->
``<root>/.kodo/guided_dev_state/specs/foo/bar.md.jsonl`` (``src/``, ``test/``
analogously). A path outside ``specs/``, ``src/``, ``test/`` is untracked.
"""

from ._paths import is_tracked, shadow_path
from ._records import Status, derive_status, last_revision_timestamp
from ._scan import scan_tracked_files
from ._store import (
    append_accepted,
    append_new_revision,
    append_review_result,
    read_document_state,
    read_history,
)

__all__ = [
    "Status",
    "append_accepted",
    "append_new_revision",
    "append_review_result",
    "derive_status",
    "is_tracked",
    "last_revision_timestamp",
    "read_document_state",
    "read_history",
    "scan_tracked_files",
    "shadow_path",
]
