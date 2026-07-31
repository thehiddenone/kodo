"""The one place an agent's LLM-facing tool list is assembled.

Most tools resolve straight from the static catalog by name, but two cannot: a
sub-agent's task shape and result shape are only knowable once you know *which*
agent is running and *which* sub-agents it may invoke. Those two live in
:mod:`kodo.subagents` (the registry) while tool resolution lives in
:mod:`kodo.tools` — sibling packages at the same import tier, neither of which
may import the other. :func:`agent_tool_specs` is the join, one tier up in
``runtime``, which imports both.

Every caller that builds a ``tools=[...]`` payload goes through it: the live
turn loop, the crash-resume path, sub-agent subsessions, the silent
engine-driven turns, and the ``kodo --tools`` diagnostic. That way the tool
surface a model sees is identical no matter which of those produced it.
"""

from __future__ import annotations

from kodo.subagents import AgentRegistry, SubAgent
from kodo.tools import tools_for_agent
from kodo.toolspecs import RETURN_RESULT, RUN_SUBAGENT, ToolSpec

__all__ = ["agent_tool_specs"]


def agent_tool_specs(registry: AgentRegistry, agent: SubAgent) -> list[ToolSpec]:
    """Return the tool specs to send to the LLM for *agent*.

    The agent's declared ``tools:`` set, resolved through
    :func:`~kodo.tools.tools_for_agent`, with two names expanded from the
    registry instead of the catalog:

    - ``run_subagent`` → one ``run_subagent_<name>`` tool per sub-agent this
      agent may invoke, each declaring that sub-agent's own ``input_schema``.
      An agent whose allow-list is empty gets none at all.
    - ``return_result`` → the same tool with ``result`` bound to this agent's
      own ``output_schema``. An entry agent (no ``SubAgentSpec``) gets none,
      which is correct: it never returns a result to anybody.

    Args:
        registry: The loaded agent registry (the source of both expansions).
        agent: The agent whose tools to resolve — already rendered by
            :meth:`~kodo.subagents.AgentRegistry.get`, so its ``tools`` set is
            the effective one for the run's mode (autonomous-filtered, with
            ``return_result`` auto-granted where it applies).

    Returns:
        list[ToolSpec]: Specs to pass to the LLM.
    """
    return tools_for_agent(
        agent.tools,
        {
            RUN_SUBAGENT.name: registry.run_subagent_specs(agent.name),
            RETURN_RESULT.name: registry.return_result_specs(agent.name),
        },
    )
