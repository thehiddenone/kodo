"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
Two mandatory preambles — the **security** preamble (``preamble_security.md``)
and the **performance** preamble (``preamble_performance.md``) — are prepended,
in that order, to every subagent's system prompt. Because the system prompt is
rebuilt fresh on every turn, both preambles are always present regardless of
context compaction (compaction only rewrites the conversation history, never the
system prompt).

Each subagent's ``## Tools`` section is rendered lazily by replacing the
``{PLACEHOLDER:TOOLS}`` token with one block per tool listed in its frontmatter
``tools:``. Every part of that block — the heading (``external_name``), the
``Autonomous mode`` line, and the ``When to use`` bullets — comes from that
tool's :class:`~kodo.toolspecs.ToolSpec`; no separate prompt file is involved.
``when_to_use`` bullets are written generically (they describe a situation, not
which agent is in it), since the same spec may be rendered into multiple
agents' prompts.

Rendering is performed lazily by :meth:`AgentRegistry.get` so it can honour the
current ``autonomous`` flag: tools whose :class:`~kodo.toolspecs.ToolSpec`
marks ``autonomous_mode`` as ``unavailable`` are excluded — from both the
rendered ``## Tools`` section and the returned :attr:`SubAgent.tools` set —
when ``autonomous=True``.

Raises :class:`~._loader.AgentLoadError` on duplicate names, missing entries,
or a tool with no matching :class:`~kodo.toolspecs.ToolSpec`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from kodo.toolspecs import ALL_TOOLS, ToolSpec, augment_output_schema

from ._loader import AgentLoadError, SubAgent, load_agent

_SECURITY_PREAMBLE_FILENAME = "preamble_security.md"
_PERFORMANCE_PREAMBLE_FILENAME = "preamble_performance.md"
_TOOLS_PLACEHOLDER = "{PLACEHOLDER:TOOLS}"

# Every tool spec, keyed by tool name (names are unique in the catalog).
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}

# Tools withheld entirely in autonomous mode.
_AUTONOMOUS_DISABLED: frozenset[str] = frozenset(
    t.name for t in ALL_TOOLS if t.autonomous_mode and "unavailable" in t.autonomous_mode.lower()
)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent returned by :meth:`get` has the security and performance
    preambles prepended (in that order) and its ``## Tools`` placeholder
    replaced with descriptions for its allowed tools, filtered for the
    requested mode.

    Args:
        agents_dir: Directory containing ``preamble_security.md``,
            ``preamble_performance.md`` and ``subagent_*.md`` files.

    Raises:
        AgentLoadError: a preamble file is missing or empty, or an agent
            references a tool with no matching :class:`~kodo.toolspecs.ToolSpec`.
    """

    __slots__ = ("__agents", "__preamble")

    def __init__(self, agents_dir: Path) -> None:
        # Security first (it takes precedence), then performance. Both are always
        # re-prepended on every render, so compaction can never drop them.
        security = self.__load_preamble(agents_dir, _SECURITY_PREAMBLE_FILENAME)
        performance = self.__load_preamble(agents_dir, _PERFORMANCE_PREAMBLE_FILENAME)
        self.__preamble = f"{security}\n\n{performance}"
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            # Validate every declared tool resolves now, at load time, so a bad
            # frontmatter reference fails fast rather than at first render.
            self.__render_tools_section(agent.tools, path)
            self.__agents[agent.name] = agent

    @staticmethod
    def __load_preamble(agents_dir: Path, filename: str) -> str:
        path = agents_dir / filename
        if not path.is_file():
            raise AgentLoadError(f"{path}: preamble file is missing")
        preamble = path.read_text(encoding="utf-8").strip()
        if not preamble:
            raise AgentLoadError(f"{path}: preamble file is empty")
        return preamble

    @staticmethod
    def __render_tools_section(agent_tools: frozenset[str], path: Path) -> str:
        blocks: list[str] = []
        for name in sorted(agent_tools):
            spec = _SPECS_BY_NAME.get(name)
            if spec is None:
                raise AgentLoadError(f"{path}: tool {name!r} has no ToolSpec in kodo.toolspecs")
            lines = []
            if spec.autonomous_mode:
                lines.append(f"- **Autonomous mode:** {spec.autonomous_mode}")
            lines.append(f"- **Security impact:** {spec.security_impact.label}")
            lines.append("- **When to use:**")
            lines.extend(f"  - {bullet}" for bullet in spec.when_to_use)
            # Output schema is visible to the agent (augmented in-flight with the
            # engine-owned `schema_compliance` flag the agent should consult).
            output_schema = json.dumps(augment_output_schema(spec.output_schema), indent=2)
            lines.append("- **Output schema:**")
            lines.append(f"  ```json\n{output_schema}\n  ```")
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
        tools_section = self.__render_tools_section(effective_tools, agent.source_path)
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

    def allowed_subagents(self, name: str) -> frozenset[str]:
        """Return the set of sub-agent names *name* is permitted to spawn.

        Read straight from the agent's frontmatter ``subagents:`` allow-list (no
        prompt rendering). Empty when the agent declares none — the default, so
        no agent can spawn sub-agents unless it explicitly opts in. The engine
        consults this to gate every ``run_subagent`` /
        ``run_author_critic_iteration`` call, for *whichever* agent makes it.

        Raises:
            AgentLoadError: No subagent file found for this name.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No subagent file for {name!r}. Expected: subagents/subagent_{name}.md"
            )
        return self.__agents[name].subagents

    def all_agents(self) -> list[SubAgent]:
        """Return all loaded subagents (interactive-mode render) in name order."""
        return [
            self.__finalize(agent, False)
            for agent in sorted(self.__agents.values(), key=lambda a: a.name)
        ]
