"""Per-project work products — the reviewable unit of a sub-agent's output.

A **work product** is every file one ``run_subagent_<author>`` review loop
wrote, reviewed and accepted together. It replaced ``primary_path``, whose
one-file-per-author model came from the first authors each writing exactly one
document and broke down as soon as a coder implemented a feature spanning
several: a multi-file change has to land in one go or the build breaks, so the
critic has to see the whole set — reviewing file-by-file cannot catch the
coherence *between* files, which is what is most likely to be wrong.

This package owns only membership: which files a work product currently holds,
and which left. The ledger is **project**-scoped — one append-only
``<project root>/.kodo/workproducts.jsonl`` — not session-scoped: a work
product's identity is derived rather than minted, so it outlives both the call
that created it and the session, which is what makes "carry on from where the
last round left off" answerable when the Guide re-invokes the same author.
The findings raised against it are the session-scoped half, living in
:mod:`kodo.findings` keyed by the work product's id; the per-file commit
history stays in :mod:`kodo.guided_state`, which is correctly per file and is
not touched here.

Like its two siblings, a leaf package of plain functions with no in-memory
index — current state is always a replay of the log.
"""

from ._records import (
    ENTRY_COMPONENTS,
    ENTRY_MEMBERSHIP,
    WorkProduct,
    components_entry,
    membership_entry,
    work_product_id,
)
from ._resolve import ResolvedNeed, resolve_needs
from ._store import (
    read_components,
    read_work_product,
    read_work_products,
    record_components,
    record_membership,
    work_product_for_path,
    work_products_log_path,
)

__all__ = [
    "ENTRY_COMPONENTS",
    "ENTRY_MEMBERSHIP",
    "ResolvedNeed",
    "WorkProduct",
    "components_entry",
    "membership_entry",
    "read_components",
    "read_work_product",
    "read_work_products",
    "record_components",
    "record_membership",
    "resolve_needs",
    "work_product_for_path",
    "work_product_id",
    "work_products_log_path",
]
