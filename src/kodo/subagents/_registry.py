"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
The security preamble (``preamble.md`` in the same directory) is mandatory and is
prepended to every subagent's system prompt.
Raises :class:`~._loader.AgentLoadError` on duplicate names or missing entries.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ._loader import AgentLoadError, SubAgent, load_agent

_PREAMBLE_FILENAME = "preamble.md"


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent's ``system_prompt`` has the security preamble prepended.

    Args:
        agents_dir: Directory containing ``preamble.md`` and ``subagent_*.md`` files.

    Raises:
        AgentLoadError: ``preamble.md`` is missing or empty.
    """

    __slots__ = ("__agents",)

    def __init__(self, agents_dir: Path) -> None:
        preamble = self.__load_preamble(agents_dir)
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            agent = replace(agent, system_prompt=f"{preamble}\n\n{agent.system_prompt}")
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
