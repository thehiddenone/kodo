"""MCP server configuration for LLM tool access."""

from __future__ import annotations

from typing import Optional


class MCPServerConfig:
    """Configuration for a single MCP server connection.

    Either ``url`` (for HTTP-based servers) or ``command`` (for stdio-based
    servers) must be supplied, but not both.
    """

    __name: str
    __url: Optional[str]
    __command: Optional[list[str]]

    def __init__(
        self,
        name: str,
        *,
        url: Optional[str] = None,
        command: Optional[list[str]] = None,
    ) -> None:
        """Initialise the configuration.

        Args:
            name (str): Logical name for this MCP server.
            url (str | None): HTTP endpoint for network-based servers.
            command (list[str] | None): Command and arguments for stdio-based servers.

        Raises:
            ValueError: If neither or both of ``url`` and ``command`` are provided.
        """
        if not url and not command:
            raise ValueError("Exactly one of 'url' or 'command' must be provided")
        if url and command:
            raise ValueError("Exactly one of 'url' or 'command' must be provided")
        self.__name = name
        self.__url = url
        self.__command = command

    @property
    def name(self) -> str:
        """Logical name for this MCP server."""
        return self.__name

    @property
    def url(self) -> Optional[str]:
        """HTTP endpoint, or ``None`` for stdio-based servers."""
        return self.__url

    @property
    def command(self) -> Optional[list[str]]:
        """Command and arguments for stdio-based servers, or ``None`` for HTTP servers."""
        return self.__command
