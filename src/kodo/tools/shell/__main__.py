"""Entry point for ``python -m kodo.tools.shell``."""

from __future__ import annotations

from ._server import Shell


def main() -> None:
    """Start the shell command MCP stdio server."""
    Shell().run()


if __name__ == "__main__":
    main()
