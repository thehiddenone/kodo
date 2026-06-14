"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
The security preamble (``preamble.md`` in the same directory) is mandatory and is
prepended to every subagent's system prompt. The tools reference (``tools_kodo.md``
in the same directory) is mandatory and is used to render each subagent's ``## Tools``
section, replacing the ``PLACEHOLDER`` token with descriptions for the tools listed
in its frontmatter.

Rendering is performed lazily by :meth:`AgentRegistry.get` so it can honour the
current ``autonomous`` flag: tools whose ``tools_kodo.md`` entry marks them
``Autonomous mode: unavailable`` are excluded — from both the rendered ``## Tools``
section and the returned :attr:`SubAgent.tools` set — when ``autonomous=True``.

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
_AUTONOMOUS_FIELD_RE = re.compile(r"^- \*\*Autonomous mode:\*\* (.+)$", re.MULTILINE)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent returned by :meth:`get` has the security preamble prepended and
    its ``## Tools`` placeholder replaced with descriptions for its allowed
    tools, filtered for the requested mode.

    Args:
        agents_dir: Directory containing ``preamble.md``, ``tools_kodo.md``, and
            ``subagent_*.md`` files.

    Raises:
        AgentLoadError: ``preamble.md`` or ``tools_kodo.md`` is missing or empty,
            or an agent references a tool not defined in ``tools_kodo.md``.
    """

    __slots__ = ("__agents", "__preamble", "__tools", "__autonomous_disabled")

    def __init__(self, agents_dir: Path) -> None:
        self.__preamble = self.__load_preamble(agents_dir)
        self.__tools = self.__load_tools(agents_dir)
        self.__autonomous_disabled = self.__compute_autonomous_disabled(self.__tools)
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            # Validate every declared tool resolves now, at load time, so a bad
            # frontmatter reference fails fast rather than at first render.
            self.__render_tools_section(agent.tools, path)
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
    def __compute_autonomous_disabled(tools: dict[str, str]) -> frozenset[str]:
        """Return the names of tools withheld in autonomous mode.

        A tool opts out of autonomous mode by carrying an
        ``- **Autonomous mode:** unavailable ...`` line in its ``tools_kodo.md``
        body (see that file's header for the field contract).
        """
        disabled: set[str] = set()
        for name, body in tools.items():
            m = _AUTONOMOUS_FIELD_RE.search(body)
            if m and "unavailable" in m.group(1).lower():
                disabled.add(name)
        return frozenset(disabled)

    def __render_tools_section(self, agent_tools: frozenset[str], path: Path) -> str:
        blocks: list[str] = []
        for name in sorted(agent_tools):
            body = self.__tools.get(name)
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

    def __finalize(self, agent: SubAgent, autonomous: bool) -> SubAgent:
        """Render *agent* for the requested mode.

        Filters autonomous-disabled tools from both the effective tool set and
        the rendered ``## Tools`` section, then prepends the preamble.
        """
        effective_tools = agent.tools
        if autonomous and self.__autonomous_disabled:
            effective_tools = frozenset(
                t for t in agent.tools if t not in self.__autonomous_disabled
            )
        tools_section = self.__render_tools_section(effective_tools, agent.source_path)
        system_prompt = agent.system_prompt.replace(_TOOLS_PLACEHOLDER, tools_section)
        system_prompt = f"{self.__preamble}\n\n{system_prompt}"
        return replace(agent, tools=effective_tools, system_prompt=system_prompt)

    def get(self, name: str, autonomous: bool = False) -> SubAgent:
        """Return the subagent for ``name``, rendered for the requested mode.

        Args:
            name: Subagent name (e.g. ``'narrative_author'``).
            autonomous: When ``True``, tools marked ``Autonomous mode:
                unavailable`` in ``tools_kodo.md`` are excluded from the agent's
                tool set and its rendered ``## Tools`` section.

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
