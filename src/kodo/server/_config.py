"""CLI argument parsing and settings for the Kōdo server."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_PORT: int = 9042
_DEFAULT_LOG_LEVEL: str = "INFO"


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration for the Kōdo server.

    Attributes:
        project: Absolute path to the Kodo project root.
        port: TCP port for the WebSocket listener (loopback only).
        log_level: Python logging level name.
    """

    project: Path
    port: int = _DEFAULT_PORT
    log_level: str = _DEFAULT_LOG_LEVEL

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> Config:
        """Parse CLI arguments into a :class:`Config`.

        Args:
            argv (list[str] | None): Argument list; defaults to ``sys.argv[1:]``.

        Returns:
            Config: Resolved configuration.
        """
        parser = argparse.ArgumentParser(
            prog="kodo-server",
            description="Kodo WebSocket server -- one instance per project.",
        )
        parser.add_argument(
            "--project",
            required=True,
            metavar="DIR",
            help="Path to the Kodo project root (must contain kodo.md).",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=_DEFAULT_PORT,
            metavar="PORT",
            help=f"WebSocket port (default: {_DEFAULT_PORT}).",
        )
        parser.add_argument(
            "--log-level",
            default=_DEFAULT_LOG_LEVEL,
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            metavar="LEVEL",
            help=f"Logging level (default: {_DEFAULT_LOG_LEVEL}).",
        )
        args = parser.parse_args(argv)
        return cls(
            project=Path(args.project).resolve(),
            port=args.port,
            log_level=args.log_level,
        )
