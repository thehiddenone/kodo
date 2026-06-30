"""Per-document evolution tracking for Guided mode — append-only ``.jsonl`` logs.

Replaces the old ``kodo.workspace`` artifact-staging system. Authors write
real files directly with ``filesystem``/``edit_file``; the engine records a
``new_revision`` entry (with the resulting mirror-commit sha) right after.
Critics record their verdict via the ``document_feedback`` tool, which writes
a ``feedback`` entry. The engine alone writes ``review_result`` (the user's
decision) and ``accepted`` (the final marker) — no dispatched tool ever
produces those two. A document's current status is always derived from the
*last* line of its log (:func:`derive_status`); there is no in-memory index
to rebuild at bootstrap.

Storage convention: ``<root>/specs/foo/bar.md`` ->
``<root>/.kodo/guided_dev_state/specs/foo/bar.md.jsonl`` (``src/``, ``test/``
analogously). A path outside ``specs/``, ``src/``, ``test/`` is untracked.
"""

from ._paths import is_tracked, shadow_path
from ._records import ConcernItem, Status, derive_status
from ._scan import scan_tracked_files
from ._store import (
    append_accepted,
    append_feedback,
    append_new_revision,
    append_review_result,
    read_history,
    read_status,
)

__all__ = [
    "ConcernItem",
    "Status",
    "append_accepted",
    "append_feedback",
    "append_new_revision",
    "append_review_result",
    "derive_status",
    "is_tracked",
    "read_history",
    "read_status",
    "scan_tracked_files",
    "shadow_path",
]
