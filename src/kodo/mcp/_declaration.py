"""LLM-provider-specific tool declaration builders backed by an MCPRegistry."""

from __future__ import annotations

from typing import Any

from ._registry import MCPRegistry


class MCPDeclaration:
    """Generates provider-specific tool declaration lists from an :class:`MCPRegistry`.

    Applies prompt-caching hints where the provider supports them.
    """

    __registry: MCPRegistry

    def __init__(self, registry: MCPRegistry) -> None:
        """Initialise the declaration builder.

        Args:
            registry (MCPRegistry): The registry to source tool definitions from.
        """
        self.__registry = registry

    def for_anthropic(self) -> list[dict[str, Any]]:
        """Generate Anthropic-compatible tool declarations.

        Applies an ephemeral ``cache_control`` hint to the final declaration
        to enable prompt caching on the tool list.

        Returns:
            list[dict]: Tool declarations ready to pass to
            ``anthropic.messages.create(tools=...)``.
        """
        declarations = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in self.__registry.tools()
        ]
        if declarations:
            declarations[-1]["cache_control"] = {"type": "ephemeral"}
        return declarations
