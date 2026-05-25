"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
Raises :class:`~._loader.AgentLoadError` on duplicate names or missing entries.
"""

from __future__ import annotations

from pathlib import Path

from ._loader import AgentLoadError, SubAgent, load_agent


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Args:
        agents_dir: Directory containing ``subagent_*.md`` files.
    """

    __slots__ = ("__agents",)

    def __init__(self, agents_dir: Path) -> None:
        self.__agents: dict[str, SubAgent] = {}
        for path in sorted(agents_dir.glob("subagent_*.md")):
            agent = load_agent(path)
            self.__agents[agent.name] = agent

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
