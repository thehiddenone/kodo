"""Entry point for ``python -m kodo.tools.fileio``."""

from __future__ import annotations

import argparse

from ._server import FileIO


def main() -> None:
    """Parse CLI arguments and start the file I/O MCP stdio server."""
    parser = argparse.ArgumentParser(description="Kōdo file I/O MCP server")
    parser.add_argument(
        "--base-dir",
        default=None,
        metavar="DIR",
        help="Restrict all file operations to this directory (optional).",
    )
    args = parser.parse_args()
    FileIO(base_dir=args.base_dir).run()


if __name__ == "__main__":
    main()
