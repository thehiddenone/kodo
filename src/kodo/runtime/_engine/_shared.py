"""Shared constants and helpers for the engine subpackage.

Only what more than one engine module (or an external test) needs lives
here: the well-known agent names, the tool-spec index, and the
project-directory helpers. Constants owned by a single concern live in
that concern's module (e.g. titling limits in :mod:`._titling`).
"""

from __future__ import annotations

import re
from pathlib import Path

from kodo.llms import ToolSpec
from kodo.toolspecs import ALL_TOOLS

_GUIDE_AGENT_NAME = "guide"
_PROBLEM_SOLVER_AGENT_NAME = "problem_solver"
_SESSION_TITLER_AGENT_NAME = "session_titler"
_COMPACTOR_AGENT_NAME = "compactor"
# Dependency-management sub-agent behind the ``toolchain_deps`` tool. Spawned only
# through the tool's dedicated ungated service (``_run_dependency_manager``), so
# it is intentionally *not* in ``_DIRECT_ONLY_AGENTS`` (which would make
# ``_spawn_subagent`` short-circuit it) nor in any agent's ``subagents:`` list.
_DEPSMGR_AGENT_NAME = "toolchain_depsmgr"
# Theme-summarization sub-agent behind the ``web_search`` tool's phase 3 (see
# doc/WEB_SEARCH.md). Driven only through the tool's dedicated ungated service
# (``_run_web_summarizer``) as a *silent* titler-style turn — never as a
# subsession, since ``web_search`` is typically called from a sub-agent (the
# investigator) and subsessions do not nest.
_WEB_SUMMARIZER_AGENT_NAME = "web_summarizer"

# Sub-agents that the engine drives directly and that must never be reachable
# through the ``run_subagent`` tool (the Guide/Problem Solver cannot
# invoke them).
_DIRECT_ONLY_AGENTS = frozenset(
    {_SESSION_TITLER_AGENT_NAME, _COMPACTOR_AGENT_NAME, _WEB_SUMMARIZER_AGENT_NAME}
)

# Every tool spec keyed by name — used to normalize each tool's output against
# its declared schema and to project the customer-visible detail rows.
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


def _slugify_project_name(name: str) -> str:
    """Derive a filesystem-safe directory slug from a human project name.

    Lowercases, turns every run of non-alphanumeric characters into a single
    dash, and trims leading/trailing dashes. Falls back to ``"project"`` when
    nothing usable remains (e.g. a name made entirely of punctuation).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _unique_child_dir(parent: Path, slug: str) -> Path:
    """Return a not-yet-existing child of *parent* based on *slug*.

    Tries ``parent/slug`` first, then ``slug-2``, ``slug-3``… so an existing
    project directory is never reused or overwritten.
    """
    candidate = parent / slug
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{slug}-{suffix}"
        suffix += 1
    return candidate
