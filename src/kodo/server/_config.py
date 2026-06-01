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
from dataclasses import dataclass, field
from pathlib import Path

from kodo.project._layout import ProjectLayout, kodo_user_dir

__all__ = ["Config"]

_DEFAULT_PORT: int = 9042
_DEFAULT_LOG_LEVEL: str = "INFO"

_DEFAULT_USER_SETTINGS: dict[str, object] = {
    "log_level": "INFO",
    "mode": "local",
    "models": {
        "high": "claude-opus-4-6",
        "medium": "claude-sonnet-4-6",
        "low": "claude-haiku-4-5",
        "local": "llamacpp-qwen36-27b",
    },
}

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration for the Kōdo server.

    Attributes:
        project: Absolute path to the Kodo project root.
        port: TCP port for the WebSocket listener (loopback only).
        log_level: Python logging level name.
        extra: Full merged settings dict for use by the engine.
    """

    project: Path
    port: int = _DEFAULT_PORT
    log_level: str = _DEFAULT_LOG_LEVEL
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> Config:
        """Parse CLI arguments and layer in settings files.

        Settings from ``<project>/.kodo/settings.json`` override
        ``~/.kodo/settings.json`` which override compiled-in defaults.

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
            default=None,  # None = not explicitly set; settings file wins over built-in default
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            metavar="LEVEL",
            help=f"Logging level (default: {_DEFAULT_LOG_LEVEL}; overrides settings.json).",
        )
        args = parser.parse_args(argv)
        project = Path(args.project).resolve()

        _ensure_user_settings()
        settings = _load_settings(project)
        settings_log_level = str(settings.get("log_level", _DEFAULT_LOG_LEVEL))
        log_level = args.log_level if args.log_level is not None else settings_log_level

        return cls(
            project=project,
            port=args.port,
            log_level=log_level,
            extra=settings,
        )

    def reload_settings(self) -> dict[str, object]:
        """Re-read and merge settings files from disk.

        Returns:
            dict[str, object]: Fresh merged settings (project overrides user).
        """
        return _load_settings(self.project)


def _ensure_user_settings() -> None:
    """Write ``~/.kodo/settings.json`` with defaults if it does not exist."""
    path = kodo_user_dir() / "settings.json"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_DEFAULT_USER_SETTINGS, indent=2), encoding="utf-8")
    _log.info("Created default user settings: %s", path)


def _load_settings(project: Path) -> dict[str, object]:
    """Load merged settings from user and project settings files.

    Args:
        project (Path): Project root path.

    Returns:
        dict[str, object]: Merged settings (project overrides user).
    """
    merged: dict[str, object] = {}

    user_settings = kodo_user_dir() / "settings.json"
    project_settings = ProjectLayout(project).settings_json

    for path in (user_settings, project_settings):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged.update(data)
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Could not load settings from %s: %s", path, exc)

    return merged
