"""CLI argument parsing and layered settings for the Kōdo server.

Settings precedence (FR-STA-05):
    project  ``<project>/.kodo/settings.json``
        ↑ overrides
    user     ``~/.kodo/settings.json``
        ↑ overrides
    defaults baked into :class:`Config`
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config"]

_DEFAULT_PORT: int = 9042
_DEFAULT_LOG_LEVEL: str = "INFO"
_DEFAULT_MODEL: str = "claude-sonnet-4-6"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration for the Kōdo server.

    Attributes:
        project: Absolute path to the Kodo project root.
        port: TCP port for the WebSocket listener (loopback only).
        log_level: Python logging level name.
        anthropic_api_key: Anthropic API key from the environment.
        default_model: Default Claude model identifier.
    """

    project: Path
    port: int = _DEFAULT_PORT
    log_level: str = _DEFAULT_LOG_LEVEL
    anthropic_api_key: str = ""
    default_model: str = _DEFAULT_MODEL
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> Config:
        """Parse CLI arguments and layer in settings files.

        Reads ``ANTHROPIC_API_KEY`` from the environment.  Settings from
        ``<project>/.kodo/settings.json`` override ``~/.kodo/settings.json``
        which override compiled-in defaults.

        Args:
            argv (list[str] | None): Argument list; defaults to ``sys.argv[1:]``.

        Returns:
            Config: Fully resolved configuration.
        """
        parser = argparse.ArgumentParser(
            prog="kodo-server",
            description="Kodo WebSocket server — one instance per project.",
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
        project = Path(args.project).resolve()

        # Layer settings: defaults → user → project
        settings = _load_settings(project)
        log_level = str(settings.get("log_level", args.log_level))
        default_model = str(settings.get("default_model", _DEFAULT_MODEL))

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        return cls(
            project=project,
            port=args.port,
            log_level=log_level,
            anthropic_api_key=api_key,
            default_model=default_model,
            extra=settings,
        )


def _load_settings(project: Path) -> dict[str, object]:
    """Load merged settings from user and project settings files.

    Args:
        project (Path): Project root path.

    Returns:
        dict[str, object]: Merged settings (project overrides user).
    """
    merged: dict[str, object] = {}

    user_settings = Path(os.path.expanduser("~")) / ".kodo" / "settings.json"
    project_settings = project / ".kodo" / "settings.json"

    for path in (user_settings, project_settings):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged.update(data)
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Could not load settings from %s: %s", path, exc)

    return merged
