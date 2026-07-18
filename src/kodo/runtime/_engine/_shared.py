"""Shared constants and helpers for the engine subpackage.

Only what more than one engine module (or an external test) needs lives
here: the well-known agent names, the tool-spec index, the project-directory
helpers, and the two small stuck-detection data shapes that both
:mod:`._turns` and :mod:`._watchdog` need — they live here rather than in
:mod:`._watchdog` itself because :mod:`._proto` (imported by every mixin,
including :mod:`._watchdog`) must also reference them in ``EngineHost``'s
protocol signatures, and :mod:`._proto` cannot import a mixin module without
risking a cycle. Constants owned by a single concern live in that concern's
module (e.g. titling limits in :mod:`._titling`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kodo.llms import Message, ToolSpec
from kodo.toolspecs import ALL_TOOLS

_GUIDE_AGENT_NAME = "guide"
_PROBLEM_SOLVER_AGENT_NAME = "problem_solver"
# Entry agent behind the ``"judge"`` workflow mode (agent_judge.md). Scores a
# finished run for kodo.validator (kodo.validator._evaluate) — read-only tools
# only, no editing/execution/ask_user. Reachable only by sending
# ``workflow.set`` with ``mode: "judge"`` over the wire; kodo-vsix's workflow
# picker only ever sends ``"guided"``/``"problem_solving"``, so this mode is
# never exposed to or selectable from the extension.
_JUDGE_AGENT_NAME = "judge"
_COMPACTOR_AGENT_NAME = "compactor"
# Dependency-management sub-agent behind the ``toolchain_deps`` tool. Spawned only
# through the tool's dedicated ungated service (``_run_dependency_manager``), so
# it is intentionally *not* in ``_DIRECT_ONLY_AGENTS`` (which would make
# ``_spawn_subagent`` short-circuit it) nor in any agent's ``subagents:`` list.
_DEPSMGR_AGENT_NAME = "toolchain_depsmgr"
# The agent behind the ``web_search`` tool (doc/WEB_SEARCH.md): drives its own
# discovery/read/synthesis loop via query_search_engine/read_webpage plus the
# pacing tools. Driven only through the tool's dedicated ungated service
# (``_run_web_search_agent``) as a *silent, multi-round tool-calling* turn —
# never as a subsession, since ``web_search`` is typically called from a
# sub-agent (the investigator) and subsessions do not nest.
_WEB_SEARCH_AGENT_NAME = "web_search"

# Sub-agents that the engine drives directly and that must never be reachable
# through the ``run_subagent`` tool (the Guide/Problem Solver cannot
# invoke them).
_DIRECT_ONLY_AGENTS = frozenset({_COMPACTOR_AGENT_NAME, _WEB_SEARCH_AGENT_NAME})

# Every tool spec keyed by name — used to normalize each tool's output against
# its declared schema and to project the customer-visible detail rows.
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


@dataclass(frozen=True)
class TurnSignal:
    """What one no-tool-call turn produced — input to every red-flag detector
    (doc/STUCK_DETECTION.md, :mod:`._watchdog`).

    Attributes:
        text: The turn's visible text (``""`` when the model produced none —
            the same emptiness the ``"(no text)"`` sentinel in
            :meth:`~._turns.TurnLoopMixin._run_agent_turn` papers over).
        thinking_text: The turn's thinking block, if any (context only; no
            detector currently inspects it, but a future one may).
        stop_reason: The provider's stop reason for this call (``"end_turn"``,
            ``"max_tokens"``, …).
    """

    text: str
    thinking_text: str
    stop_reason: str


@dataclass(frozen=True)
class StallDecision:
    """What :meth:`~._watchdog.WatchdogMixin._make_stall_handler`'s closure
    decided for one stalled round.

    Attributes:
        retry: When ``True``, :meth:`~._turns.TurnLoopMixin._run_agent_turn`
            appends ``message`` and loops again instead of ending the turn.
        message: The nudge to append when ``retry`` is ``True``; ``None``
            otherwise.
    """

    retry: bool
    message: Message | None = None


@dataclass(frozen=True)
class RedFlag:
    """One matched stuck-agent red flag (:mod:`._watchdog`).

    Attributes:
        code: Short machine-readable id (persisted in the nudge's ``detail``).
        hint: One-sentence, user-facing description of what was observed —
            joined into the nudge's user-facing note, never sent to the LLM.
    """

    code: str
    hint: str


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
