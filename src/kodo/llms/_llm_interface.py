"""Generic interface classes for LLM facades."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ._mcp import MCPServerConfig


class LLMInterface(ABC):
    """Abstract base class for all LLM integration facades.

    Subclasses provide model selection, prompt execution, and MCP tool
    declaration for a specific LLM provider.
    """

    @property
    @abstractmethod
    def model(self) -> str | None:
        """Currently selected model identifier, or ``None`` if not yet set."""

    @abstractmethod
    def set_model(self, model: str) -> None:
        """Select the model to use for subsequent ``run()`` calls.

        Args:
            model (str): Provider-specific model identifier.
        """

    @abstractmethod
    def run(self, prompt: str) -> str:
        """Execute a prompt and return the model's final text response.

        If MCP servers have been declared the model may invoke tools
        during the agentic loop before producing its final answer.

        Args:
            prompt (str): User prompt to send to the model.

        Returns:
            str: Final text response from the model.
        """

    @abstractmethod
    def declare_mcp_servers(self, servers: list[MCPServerConfig]) -> None:
        """Declare MCP servers whose tools the model may use during ``run()``.

        Args:
            servers (list[MCPServerConfig]): MCP servers to make available.
        """
