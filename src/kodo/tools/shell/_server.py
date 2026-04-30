"""MCP stdio server providing a shell command execution tool."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent


class Shell:
    """MCP stdio server that exposes shell command execution as a tool.

    Commands are executed via the system shell (``shell=True``).  Security
    enforcement is handled by the caller's MCP security layer, not here.
    """

    __app: FastMCP

    def __init__(self) -> None:
        """Initialise the server and register the ``run_command`` tool."""
        self.__app = FastMCP("kodo-shell")
        self.__app.tool(
            name="run_command",
            description=(
                "Execute a shell command and return its exit code, stdout, and stderr "
                "as separate content blocks."
            ),
        )(self.__run_command)

    def run(self) -> None:
        """Start the MCP stdio server and block until the client disconnects."""
        self.__app.run(transport="stdio")

    def __run_command(
        self,
        command: str,
        working_dir: str | None = None,
    ) -> list[TextContent]:
        """Run a shell command and return exit code, stdout, and stderr.

        Args:
            command (str): Shell command to execute.
            working_dir (str | None): Directory to run the command in.
                Defaults to the current working directory.

        Returns:
            list[TextContent]: Three blocks — exit code, stdout, stderr.
        """
        cwd = Path(working_dir).resolve() if working_dir else None
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return [
            TextContent(type="text", text=f"exit_code: {result.returncode}"),
            TextContent(type="text", text=f"stdout:\n{result.stdout}"),
            TextContent(type="text", text=f"stderr:\n{result.stderr}"),
        ]
