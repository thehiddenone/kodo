"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
The security preamble (``preamble.md`` in the same directory) is mandatory and is
prepended to every subagent's system prompt. The tools reference (``tools_kodo.md``
in the same directory) is mandatory and is used to render each subagent's ``## Tools``
section, replacing the ``PLACEHOLDER`` token with descriptions for the tools listed
in its frontmatter.
Raises :class:`~._loader.AgentLoadError` on duplicate names or missing entries.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from ._loader import AgentLoadError, SubAgent, load_agent

_PREAMBLE_FILENAME = "preamble.md"
_TOOLS_FILENAME = "tools_kodo.md"
_TOOLS_PLACEHOLDER = "{PLACEHOLDER:TOOLS}"

_TOOL_HEADING_RE = re.compile(r"^## (\S+)[ \t]*$", re.MULTILINE)
_EXTERNAL_NAME_RE = re.compile(r"^- \*\*External name:\*\* (.+)$", re.MULTILINE)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent's ``system_prompt`` has the security preamble prepended and its
    ``## Tools`` placeholder replaced with descriptions for its allowed tools.

    Args:
        agents_dir: Directory containing ``preamble.md``, ``tools_kodo.md``, and
            ``subagent_*.md`` files.

    Raises:
        AgentLoadError: ``preamble.md`` or ``tools_kodo.md`` is missing or empty,
            or an agent references a tool not defined in ``tools_kodo.md``.
    """

    __slots__ = ("__agents",)

    def __init__(self, agents_dir: Path) -> None:
        preamble = self.__load_preamble(agents_dir)
        tools = self.__load_tools(agents_dir)
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            tools_section = self.__render_tools_section(agent.tools, tools, path)
            system_prompt = agent.system_prompt.replace(_TOOLS_PLACEHOLDER, tools_section)
            system_prompt = f"{preamble}\n\n{system_prompt}"
            agent = replace(agent, system_prompt=system_prompt)
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
    def __load_tools(agents_dir: Path) -> dict[str, str]:
        path = agents_dir / _TOOLS_FILENAME
        if not path.is_file():
            raise AgentLoadError(f"{path}: tools reference file is missing")
        text = path.read_text(encoding="utf-8")

        matches = list(_TOOL_HEADING_RE.finditer(text))
        if not matches:
            raise AgentLoadError(f"{path}: no tool sections found")

        tools: dict[str, str] = {}
        for i, m in enumerate(matches):
            name = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            if not body:
                raise AgentLoadError(f"{path}: tool {name!r} has an empty description")
            tools[name] = body
        return tools

    @staticmethod
    def __render_tools_section(
        agent_tools: frozenset[str], tools: dict[str, str], path: Path
    ) -> str:
        blocks: list[str] = []
        for name in sorted(agent_tools):
            body = tools.get(name)
            if body is None:
                raise AgentLoadError(f"{path}: tool {name!r} is not defined in {_TOOLS_FILENAME}")
            m = _EXTERNAL_NAME_RE.search(body)
            if not m:
                raise AgentLoadError(
                    f"tool {name!r} in {_TOOLS_FILENAME}: missing 'External name' field"
                )
            external_name = m.group(1).strip()
            remainder = _EXTERNAL_NAME_RE.sub("", body, count=1).strip()
            blocks.append(f"### {external_name} (`{name}`)\n\n{remainder}")
        return "\n\n".join(blocks)

    def get(self, name: str) -> SubAgent:
        """Return the subagent for ``name``.

        Args:
            name: Subagent name (e.g. ``'narrative_author'``).

        Returns:
            SubAgent: The matching subagent definition.

        Raises:
            AgentLoadError: No subagent file found for this name.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No subagent file for {name!r}. Expected: subagents/subagent_{name}.md"
            )
        return self.__agents[name]

    def all_agents(self) -> list[SubAgent]:
        """Return all loaded subagents in deterministic (name) order."""
        return sorted(self.__agents.values(), key=lambda a: a.name)
