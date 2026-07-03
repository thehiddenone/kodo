"""Security layer — per-call allow/ask judgement over the tool catalog.

The implemented surface is :class:`SecurityLayer` (see :mod:`._layer` for the
three postures) plus its building blocks: static ``run_command`` workspace
analysis (:mod:`._analysis`) and the SMART-mode LLM intent judge's prompt and
verdict parsing (:mod:`._judge`). The runtime injects the actual LLM callable
(:data:`JudgeCallable`) and surfaces ``ask`` decisions as ``prompt.permission``
requests; this package performs no I/O of its own.

Imports only :mod:`kodo.toolspecs` (the catalog) and :mod:`kodo.shellparser`
(the structural parse) — consumed exclusively by ``runtime`` (see
doc/SECURITY.md for the full design).

``_rules`` / ``_store`` / ``_defaults`` remain stubs for a future iteration:
persistent user-defined allow/deny rules ("always allow commands like this")
layered ahead of the per-call judgement implemented here.
"""

from ._analysis import CommandAnalysis, analyze_command
from ._judge import JudgeVerdict, build_judge_messages, parse_judge_verdict
from ._layer import (
    MODE_DEFENSIVE,
    MODE_PERMISSIVE,
    MODE_SMART,
    JudgeCallable,
    SecurityDecision,
    SecurityLayer,
)

__all__ = [
    "MODE_DEFENSIVE",
    "MODE_PERMISSIVE",
    "MODE_SMART",
    "CommandAnalysis",
    "JudgeCallable",
    "JudgeVerdict",
    "SecurityDecision",
    "SecurityLayer",
    "analyze_command",
    "build_judge_messages",
    "parse_judge_verdict",
]
