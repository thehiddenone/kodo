"""Agent registry — ``(name, model) -> Agent`` lookup.

Loads all ``.md`` files from the agents package directory at construction time.
Raises :class:`~._loader.AgentLoadError` on duplicate agents or missing variants.
"""

from __future__ import annotations

from pathlib import Path

from ._loader import Agent, AgentLoadError, load_agent

__all__ = ["AgentRegistry"]


class AgentRegistry:
    """Index of all loaded agents, looked up by ``(name, model)`` pair.

    Args:
        agents_dir: Directory containing ``*.md`` agent files.

    Raises:
        AgentLoadError: Duplicate ``(name, model)`` pair found across files.
    """

    __slots__ = ("__agents",)

    def __init__(self, agents_dir: Path) -> None:
        self.__agents: dict[tuple[str, str], Agent] = {}
        for path in sorted(agents_dir.glob("*.md")):
            agent = load_agent(path)
            key = (agent.name, agent.model)
            if key in self.__agents:
                existing = self.__agents[key].source_path
                raise AgentLoadError(
                    f"Duplicate agent ({agent.name!r}, {agent.model!r}): "
                    f"{existing} and {path}"
                )
            self.__agents[key] = agent

    def get(self, name: str, model: str) -> Agent:
        """Return the agent for ``(name, model)``.

        Args:
            name: Agent name (e.g. ``'narrative_author'``).
            model: Model identifier (e.g. ``'claude-sonnet-4-6'``).

        Returns:
            Agent: The matching agent definition.

        Raises:
            AgentLoadError: No variant found for this ``(name, model)`` pair.
        """
        key = (name, model)
        if key not in self.__agents:
            raise AgentLoadError(
                f"No agent file for ({name!r}, {model!r}). "
                f"Expected: agents/{name}.{model}.md"
            )
        return self.__agents[key]

    def all_agents(self) -> list[Agent]:
        """Return all loaded agents in deterministic order."""
        return sorted(self.__agents.values(), key=lambda a: (a.name, a.model))
