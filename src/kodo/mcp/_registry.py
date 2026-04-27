"""MCP server registry: loads configuration, discovers tools, and routes calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent


class _ServerEntry:
    """Configuration record for a single MCP server."""

    __name: str
    __command: list[str] | None
    __env: dict[str, str]
    __url: str | None

    def __init__(
        self,
        name: str,
        command: list[str] | None,
        env: dict[str, str],
        url: str | None,
    ) -> None:
        """Initialise the server entry.

        Args:
            name (str): Logical name for this server.
            command (list[str] | None): Executable and arguments for stdio-based servers.
            env (dict[str, str]): Extra environment variables for the subprocess.
            url (str | None): HTTP SSE endpoint for network-based servers.
        """
        self.__name = name
        self.__command = command
        self.__env = env
        self.__url = url

    @property
    def name(self) -> str:
        """Logical name for this server."""
        return self.__name

    @property
    def command(self) -> list[str] | None:
        """Executable and arguments, or ``None`` for HTTP-based servers."""
        return self.__command

    @property
    def env(self) -> dict[str, str]:
        """Extra environment variables passed to the subprocess."""
        return self.__env

    @property
    def url(self) -> str | None:
        """HTTP SSE endpoint, or ``None`` for stdio-based servers."""
        return self.__url


class MCPRegistry:
    """Registry of MCP servers loaded from a JSON configuration file.

    Discovers tools from all configured servers on first access, caches the
    results, and routes tool calls to the appropriate server.

    Registry JSON format::

        {
          "servers": {
            "my-stdio-server": {
              "command": "python",
              "args": ["-m", "my_server"],
              "env": {"KEY": "value"}
            },
            "my-http-server": {
              "url": "http://localhost:8000/sse"
            }
          }
        }
    """

    __config_path: Path
    __servers: dict[str, _ServerEntry]
    __tools_cache: list[dict[str, object]] | None
    __tool_index: dict[str, str]

    def __init__(self, config_path: str | Path) -> None:
        """Initialise the registry from a JSON configuration file.

        Args:
            config_path (str | Path): Path to the registry JSON file.

        Raises:
            ValueError: If a server entry has neither ``command`` nor ``url``.
        """
        self.__config_path = Path(config_path)
        self.__servers = self.__load_config()
        self.__tools_cache = None
        self.__tool_index = {}

    def tools(self) -> list[dict[str, object]]:
        """Return all tools from all registered servers.

        Connects to each server on first call; subsequent calls return the
        cached result.

        Returns:
            list[dict]: Tool definitions, each with ``name``, ``description``,
            and ``input_schema`` keys.
        """
        if self.__tools_cache is None:
            self.__tools_cache = asyncio.run(self.__discover_all_tools())
        return self.__tools_cache

    def call(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Route a tool call to the appropriate server and return its output.

        Args:
            tool_name (str): Name of the tool to invoke.
            tool_input (dict): Input arguments for the tool.

        Returns:
            str: Concatenated text output from the tool.

        Raises:
            KeyError: If no registered server provides the named tool.
        """
        if not self.__tool_index:
            self.tools()
        server_name = self.__tool_index.get(tool_name)
        if server_name is None:
            raise KeyError(f"No registered MCP server provides tool {tool_name!r}")
        return asyncio.run(self.__invoke_tool(self.__servers[server_name], tool_name, tool_input))

    def __load_config(self) -> dict[str, _ServerEntry]:
        data: dict[str, object] = json.loads(self.__config_path.read_text(encoding="utf-8"))
        servers: dict[str, _ServerEntry] = {}
        servers_dict = data.get("servers", {})
        if isinstance(servers_dict, dict):
            for name, cfg in servers_dict.items():
                command: list[str] | None = None
                if "command" in cfg:
                    command = [cfg["command"]] + cfg.get("args", [])
                url: str | None = cfg.get("url")
                if command is None and url is None:
                    raise ValueError(f"MCP server {name!r} must have either 'command' or 'url'")
                servers[name] = _ServerEntry(
                    name=name,
                    command=command,
                    env=cfg.get("env", {}),
                    url=url,
                )
        return servers

    async def __discover_all_tools(self) -> list[dict[str, object]]:
        all_tools: list[dict[str, object]] = []
        for name, entry in self.__servers.items():
            async with self.__open_session(entry) as session:
                result = await session.list_tools()
                for tool in result.tools:
                    self.__tool_index[tool.name] = name
                    all_tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                    )
        return all_tools

    async def __invoke_tool(
        self,
        entry: _ServerEntry,
        tool_name: str,
        tool_input: dict[str, object],
    ) -> str:
        async with self.__open_session(entry) as session:
            result = await session.call_tool(tool_name, tool_input)
            parts: list[str] = []
            for block in result.content:
                if isinstance(block, TextContent):
                    parts.append(block.text)
            return "\n".join(parts)

    @contextlib.asynccontextmanager
    async def __open_session(self, entry: _ServerEntry) -> AsyncIterator[ClientSession]:
        if entry.url is not None:
            async with (
                sse_client(entry.url) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
        else:
            assert entry.command is not None
            params = StdioServerParameters(
                command=entry.command[0],
                args=entry.command[1:],
                env=entry.env or None,
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                yield session
