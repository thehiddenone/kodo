"""Entry point for ``python -m kodo.tools.workspace``."""

from __future__ import annotations

import argparse
from pathlib import Path

from kodo.toolchains import PythonPlugin

from ._server import WorkspaceTool


def main() -> None:
    """Parse CLI arguments and start the workspace MCP stdio server."""
    parser = argparse.ArgumentParser(description="Kōdo workspace MCP server")
    parser.add_argument(
        "--project-root",
        required=True,
        metavar="DIR",
        help="Root directory of the Kodo project.",
    )
    args = parser.parse_args()
    WorkspaceTool(
        project_root=Path(args.project_root), toolchain=PythonPlugin(Path(args.project_root))
    ).run()


if __name__ == "__main__":
    main()
