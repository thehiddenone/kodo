"""LLMInterface implementation for Anthropic Claude models."""

from __future__ import annotations

from typing import Any

import anthropic

from .._llm_interface import LLMInterface
from .._mcp import MCPServerConfig


class ClaudeClient(LLMInterface):
    """LLM facade for Anthropic Claude models.

    Supports model selection, prompt execution with an agentic tool-use loop,
    and declaration of MCP servers whose tools are made available to the model.

    Typical usage::

        client = ClaudeClient(api_key="...")
        models = client.available_models()
        client.set_model(next(iter(models)))
        result = client.run("Hello!")
    """

    __model: str | None
    __client: anthropic.Anthropic
    __mcp_servers: list[MCPServerConfig]

    def __init__(self, api_key: str) -> None:
        """Initialise the Claude facade.

        No model is selected on construction. Call :meth:`available_models`
        then :meth:`set_model` before invoking :meth:`run`.

        Args:
            api_key (str): Anthropic API key.
        """
        self.__model = None
        self.__client = anthropic.Anthropic(api_key=api_key)
        self.__mcp_servers = []

    @property
    def model(self) -> str | None:
        """Currently selected model identifier, or ``None`` if not yet set."""
        return self.__model

    def available_models(self) -> dict[str, dict[str, Any]]:
        """Fetch available models from the Anthropic API and return their capabilities.

        Returns:
            dict[str, dict]: Mapping of model ID to a capabilities dict containing
            ``display_name`` and ``created_at`` as reported by the API.
        """
        page = self.__client.models.list()
        return {
            m.id: {
                "display_name": m.display_name,
                "created_at": m.created_at,
            }
            for m in page.data
        }

    def set_model(self, model: str) -> None:
        """Select the model to use for subsequent ``run()`` calls.

        Args:
            model (str): A value from :class:`ClaudeModel` or any valid Claude model ID.
        """
        self.__model = model

    def declare_mcp_servers(self, servers: list[MCPServerConfig]) -> None:
        """Declare MCP servers whose tools the model may use during ``run()``.

        Args:
            servers (list[MCPServerConfig]): MCP servers to make available.
        """
        self.__mcp_servers = list(servers)

    def run(self, prompt: str) -> str:
        """Execute a prompt and return the model's final text response.

        Runs the full agentic loop: if the model requests MCP tool calls they
        are dispatched and their results fed back until the model produces a
        final ``end_turn`` response.

        Args:
            prompt (str): User prompt to send to the model.

        Returns:
            str: Final text response from the model.
        """
        tools = self.__build_tools()
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]

        while True:
            kwargs: dict[str, Any] = {
                "model": self.__model,
                "max_tokens": 8192,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = self.__client.messages.create(**kwargs)

            if response.stop_reason == "end_turn":
                return self.__extract_text(response)

            if response.stop_reason == "tool_use":
                tool_results = self.__dispatch_tool_calls(response)
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                return self.__extract_text(response)

    def __build_tools(self) -> list[dict[str, Any]]:
        """Convert declared MCP servers into Anthropic tool definitions.

        Returns:
            list[dict]: Tool definitions to pass to the API, or an empty list
            when no MCP servers have been declared.
        """
        # TODO: connect to each MCP server and fetch its tool manifest
        return []

    def __dispatch_tool_calls(
        self,
        response: anthropic.types.Message,
    ) -> list[dict[str, Any]]:
        """Execute all tool-use blocks in a model response and collect results.

        Args:
            response (anthropic.types.Message): Model response containing tool-use blocks.

        Returns:
            list[dict]: Tool-result content blocks for the next user message.
        """
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = self.__call_mcp_tool(block.name, block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        return results

    def __call_mcp_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        server: MCPServerConfig | None = None,
    ) -> str:
        """Invoke a single MCP tool and return its output.

        Args:
            tool_name (str): Name of the tool to invoke.
            tool_input (dict): Arguments for the tool.
            server (MCPServerConfig | None): The server that owns the tool.

        Returns:
            str: Tool output to return to the model.

        Raises:
            NotImplementedError: Until MCP dispatch is implemented.
        """
        # TODO: route to the correct MCP server and execute the tool call
        raise NotImplementedError(
            f"MCP tool dispatch not yet implemented (tool: {tool_name!r})"
        )

    def __extract_text(self, response: anthropic.types.Message) -> str:
        """Concatenate all text blocks from a model response.

        Args:
            response (anthropic.types.Message): Model response to extract from.

        Returns:
            str: Concatenated plain text content.
        """
        return "".join(
            block.text
            for block in response.content
            if block.type == "text"
        )
