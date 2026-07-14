"""Security layer — per-call allow/ask judgement over the tool catalog.

The implemented surface is :class:`SecurityLayer` (see :mod:`._layer` for the
three postures) plus its building blocks: static ``run_command`` workspace
analysis (:mod:`._analysis` over the :mod:`._classify` normalized view) and
the heuristic command rule engine (:mod:`._rules` evaluating the built-in
:mod:`._defaults` table). Judgement is fully deterministic — no LLM is ever
consulted; the runtime surfaces ``ask`` decisions as ``prompt.permission``
requests, and this package performs no I/O of its own.

Imports only :mod:`kodo.common` (the OS-temp-directory helper shared with
``kodo.tools``'s path resolvers), :mod:`kodo.toolspecs` (the catalog), and
:mod:`kodo.shellparser` (the structural parse) — consumed exclusively by
``runtime`` (see doc/SECURITY.md for the full design,
doc/SECURITY_RULES_PLAN.md for the rule engine's plan and rationale).

``_store`` remains a stub for Phase 2: persistent user-defined "always allow
commands like this" rules layered into the rule ladder implemented here.
"""

from ._analysis import CommandAnalysis, analyze_command
from ._layer import (
    MODE_DEFENSIVE,
    MODE_PERMISSIVE,
    MODE_SMART,
    SecurityDecision,
    SecurityLayer,
)
from ._rules import CommandRule, RuleDecision, evaluate_command

__all__ = [
    "MODE_DEFENSIVE",
    "MODE_PERMISSIVE",
    "MODE_SMART",
    "CommandAnalysis",
    "CommandRule",
    "RuleDecision",
    "SecurityDecision",
    "SecurityLayer",
    "analyze_command",
    "evaluate_command",
]
