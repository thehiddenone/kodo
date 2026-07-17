"""Security layer — per-call allow/ask judgement over the tool catalog.

The implemented surface is :class:`SecurityLayer` (see :mod:`._layer` for the
three postures) plus its building blocks: static ``run_command`` workspace
analysis (:mod:`._analysis` over the :mod:`._classify` normalized view) and
the heuristic command rule engine (:mod:`._rules` evaluating the built-in
:mod:`._defaults` table). Judgement is fully deterministic — no LLM is ever
consulted; the runtime surfaces ``ask`` decisions as ``prompt.permission``
requests, and this package performs no I/O of its own.

Imports :mod:`kodo.common` (the OS-temp-directory helper shared with
``kodo.tools``'s path resolvers), :mod:`kodo.toolspecs` (the catalog),
:mod:`kodo.shellparser` (the structural parse), and :mod:`kodo.project`
(``WorkspaceLayout``, for the global rule store's on-disk location only) —
consumed exclusively by ``runtime`` (see doc/SECURITY.md for the full
design, doc/SECURITY_RULES_PLAN.md for the rule engine's plan and rationale).

Phase 2 "always allow commands like this" rules (a permission-prompt-granted
``(executable, subcommand)`` shape, doc/SECURITY_RULES_PLAN.md §2) are two
independent scopes: **session** rules are ordinary runtime state
(``kodo.runtime.SessionState.security_rules``, no code here) merged in by
the caller as ``SecurityLayer.evaluate(session_rules=...)``; **global**
(user-wide, every project) rules are this package's own concern —
:mod:`._store`'s flat JSON store, consulted automatically inside
``SecurityLayer`` without the caller passing anything.
"""

from ._analysis import CommandAnalysis, analyze_command
from ._layer import (
    MODE_DEFENSIVE,
    MODE_PERMISSIVE,
    MODE_SMART,
    SecurityDecision,
    SecurityLayer,
)
from ._rules import AskPart, CommandRule, RuleDecision, evaluate_command
from ._store import add_global_rule, global_rules, global_rules_path

__all__ = [
    "MODE_DEFENSIVE",
    "MODE_PERMISSIVE",
    "MODE_SMART",
    "AskPart",
    "CommandAnalysis",
    "CommandRule",
    "RuleDecision",
    "SecurityDecision",
    "SecurityLayer",
    "add_global_rule",
    "analyze_command",
    "evaluate_command",
    "global_rules",
    "global_rules_path",
]
