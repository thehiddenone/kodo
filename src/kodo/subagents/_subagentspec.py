"""The :class:`SubAgentSpec` dataclass — the typed interface of a sub-agent.

A sub-agent is "a tool with agentic behavior": like a :class:`~kodo.toolspecs.ToolSpec`
it declares an ``input_schema`` (what the caller must supply when delegating) and
an ``output_schema`` (what the sub-agent returns, via the ``return_result`` tool,
when it finishes). Both are JSON-Schema ``object`` dicts.

Per the project decision, a ``SubAgentSpec`` carries **only** the schemas (plus a
caller-facing ``description``); every other piece of agent metadata — tools,
capability, ``display_name``, ``solo``/``critic``/``standalone``, ``## Purpose`` —
stays in the ``subagent_*.md`` frontmatter/body and is loaded by
:func:`~kodo.subagents._loader.load_agent`. The registry cross-references a spec
to its :class:`~kodo.subagents._loader.SubAgent` by ``name``.

One spec per file under :mod:`kodo.subagents.specs`, mirroring the
``kodo.toolspecs`` one-literal-per-file convention.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SubAgentSpec"]


@dataclass(frozen=True)
class SubAgentSpec:
    """The typed input/output contract of a single sub-agent.

    Attributes:
        name: Sub-agent name — matches ``SubAgent.name`` and the
            ``subagent_<name>.md`` filename stem.
        description: One-line, caller-facing summary of the delegation, rendered
            into a caller's ``{PLACEHOLDER:SUBAGENTS}`` roster alongside the
            schemas.
        input_schema: JSON Schema (an ``object`` schema) describing the
            structured task the caller must supply when delegating. The engine
            validates the delegated ``task_input`` against it before spawning.
        output_schema: JSON Schema describing what the sub-agent returns through
            the ``return_result`` tool. The engine augments it with the
            engine-owned ``schema_compliance`` field (see
            :mod:`kodo.toolspecs._compliance`) before showing it to the agent and
            normalizes the ``return_result`` payload against it — so, like a
            tool's output schema, specs must NOT declare ``schema_compliance``.
            May be a top-level ``oneOf`` for a dual-role agent (``test_coder``).
    """

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
