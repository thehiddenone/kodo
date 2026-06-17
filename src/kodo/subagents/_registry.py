"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
The security preamble (``preamble.md`` in the same directory) is mandatory and is
prepended to every subagent's system prompt.

Each subagent's ``## Tools`` section is rendered lazily by replacing the
``{PLACEHOLDER:TOOLS}`` token with one block per tool listed in its frontmatter
``tools:``. Every part of that block — the heading (``external_name``), the
``Autonomous mode`` line, and the ``When to use`` bullets — comes from that
tool's :class:`~kodo.toolspecs.ToolSpec`; no separate prompt file is involved.
``when_to_use`` bullets are written generically (they describe a situation, not
which agent is in it), since the same spec may be rendered into multiple
agents' prompts.

The ``ask_user`` tool has two specs — :data:`~kodo.toolspecs._ask_user.ASK_USER`
(leaf agents) and :data:`~kodo.toolspecs._ask_user_orchestrator.ORCHESTRATOR_ASK_USER`
(the orchestrator) — with the same tool name but different guidance. The
registry picks between them based on which subagent is being rendered.

Rendering is performed lazily by :meth:`AgentRegistry.get` so it can honour the
current ``autonomous`` flag: tools whose :class:`~kodo.toolspecs.ToolSpec`
marks ``autonomous_mode`` as ``unavailable`` are excluded — from both the
rendered ``## Tools`` section and the returned :attr:`SubAgent.tools` set —
when ``autonomous=True``.

Raises :class:`~._loader.AgentLoadError` on duplicate names, missing entries,
or a tool with no matching :class:`~kodo.toolspecs.ToolSpec`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kodo.toolspecs import ALL_TOOLS, ASK_USER, ORCHESTRATOR_ASK_USER, ToolSpec

from ._loader import AgentLoadError, SubAgent, load_agent

_PREAMBLE_FILENAME = "preamble.md"
_TOOLS_PLACEHOLDER = "{PLACEHOLDER:TOOLS}"

# The only subagent that uses the orchestrator variant of `ask_user`. Must
# match kodo.runtime._engine._ORCHESTRATOR_AGENT_NAME.
_ORCHESTRATOR_AGENT_NAME = "orchestrator"

# Tool specs available to leaf sub-agents, keyed by tool name.
_LEAF_SPECS_BY_NAME: dict[str, ToolSpec] = {
    t.name: t for t in ALL_TOOLS if t is not ORCHESTRATOR_ASK_USER
}

# Tool specs available to the orchestrator, keyed by tool name.
_ORCHESTRATOR_SPECS_BY_NAME: dict[str, ToolSpec] = {
    t.name: t for t in ALL_TOOLS if t is not ASK_USER
}

# Tools withheld entirely in autonomous mode.
_AUTONOMOUS_DISABLED: frozenset[str] = frozenset(
    t.name for t in ALL_TOOLS if t.autonomous_mode and "unavailable" in t.autonomous_mode.lower()
)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent returned by :meth:`get` has the security preamble prepended and
    its ``## Tools`` placeholder replaced with descriptions for its allowed
    tools, filtered for the requested mode.

    Args:
        agents_dir: Directory containing ``preamble.md`` and ``subagent_*.md``
            files.

    Raises:
        AgentLoadError: ``preamble.md`` is missing or empty, or an agent
            references a tool with no matching :class:`~kodo.toolspecs.ToolSpec`.
    """

    __slots__ = ("__agents", "__preamble")

    def __init__(self, agents_dir: Path) -> None:
        self.__preamble = self.__load_preamble(agents_dir)
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            # Validate every declared tool resolves now, at load time, so a bad
            # frontmatter reference fails fast rather than at first render.
            self.__render_tools_section(agent.tools, agent.name, path)
            self.__agents[agent.name] = agent

    @staticmethod
    def __load_preamble(agents_dir: Path) -> str:
        path = agents_dir / _PREAMBLE_FILENAME
        if not path.is_file():
            raise AgentLoadError(f"{path}: security preamble file is missing")
        preamble = path.read_text(encoding="utf-8").strip()
        if not preamble:
            raise AgentLoadError(f"{path}: security preamble file is empty")
        return preamble

    @staticmethod
    def __render_tools_section(agent_tools: frozenset[str], agent_name: str, path: Path) -> str:
        specs = (
            _ORCHESTRATOR_SPECS_BY_NAME
            if agent_name == _ORCHESTRATOR_AGENT_NAME
            else _LEAF_SPECS_BY_NAME
        )
        blocks: list[str] = []
        for name in sorted(agent_tools):
            spec = specs.get(name)
            if spec is None:
                raise AgentLoadError(f"{path}: tool {name!r} has no ToolSpec in kodo.toolspecs")
            lines = []
            if spec.autonomous_mode:
                lines.append(f"- **Autonomous mode:** {spec.autonomous_mode}")
            lines.append("- **When to use:**")
            lines.extend(f"  - {bullet}" for bullet in spec.when_to_use)
            blocks.append(f"### {spec.external_name} (`{name}`)\n\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    def __finalize(self, agent: SubAgent, autonomous: bool) -> SubAgent:
        """Render *agent* for the requested mode.

        Filters autonomous-disabled tools from both the effective tool set and
        the rendered ``## Tools`` section, then prepends the preamble.
        """
        effective_tools = agent.tools
        if autonomous and _AUTONOMOUS_DISABLED:
            effective_tools = frozenset(t for t in agent.tools if t not in _AUTONOMOUS_DISABLED)
        tools_section = self.__render_tools_section(effective_tools, agent.name, agent.source_path)
        system_prompt = agent.system_prompt.replace(_TOOLS_PLACEHOLDER, tools_section)
        system_prompt = f"{self.__preamble}\n\n{system_prompt}"
        return replace(agent, tools=effective_tools, system_prompt=system_prompt)

    def get(self, name: str, autonomous: bool = False) -> SubAgent:
        """Return the subagent for ``name``, rendered for the requested mode.

        Args:
            name: Subagent name (e.g. ``'narrative_author'``).
            autonomous: When ``True``, tools whose ``ToolSpec.autonomous_mode``
                is ``unavailable`` are excluded from the agent's tool set and
                its rendered ``## Tools`` section.

        Returns:
            SubAgent: The matching subagent definition.

        Raises:
            AgentLoadError: No subagent file found for this name.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No subagent file for {name!r}. Expected: subagents/subagent_{name}.md"
            )
        return self.__finalize(self.__agents[name], autonomous)

    def all_agents(self) -> list[SubAgent]:
        """Return all loaded subagents (interactive-mode render) in name order."""
        return [
            self.__finalize(agent, False)
            for agent in sorted(self.__agents.values(), key=lambda a: a.name)
        ]
