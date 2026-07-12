"""CLI argument parsing and settings for the singleton Kōdo server.

The server is a machine-wide singleton rooted at the global home ``~/.kodo``.
Settings live in a single ``~/.kodo/etc/settings.json`` (no per-workspace
layering — there is no per-workspace state any more).  Compiled-in defaults
fill any missing keys.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field

from kodo.project import WorkspaceLayout

__all__ = ["Config"]

_DEFAULT_PORT: int = 9042
_DEFAULT_LOG_LEVEL: str = "INFO"

_DEFAULT_USER_SETTINGS: dict[str, object] = {
    "log_level": "INFO",
    "mode": "local",
    "cloud_concurrency": 2,
    # The active cloud vendor when mode=="cloud" — selects which sub-map of
    # models.cloud below is used to resolve a capability. See doc/LLM_REGISTRY.md.
    "active_cloud_vendor": "anthropic",
    # NOTE: the main-context token budget is no longer a global setting. It is
    # the *current model's* context window (the per-model `context_window` in
    # kodo/llms/_cloud_registry.py or kodo/llms/_local_registry.py), so
    # switching models changes the limit and the auto-compaction threshold.
    # See ContextCompactor.context_limit (runtime/_engine/_compaction.py) and
    # doc/STATE_AND_LIFECYCLE.md §4.5.
    "models": {
        "local": "llamacpp-qwen36-27b-q4-k-xl",
        # base_llm -> thinking-tier slug (e.g. "high", "unlimited"). Absent
        # key = that family's default tier. See
        # kodo.llms.local_thinking_default_tier / doc/LLM_REGISTRY.md.
        "local_thinking": {},
        "cloud": {
            "anthropic": {
                "low": "claude-haiku-4-5-20251001",
                "medium": "claude-sonnet-5",
                "high": "claude-opus-4-8",
                "max": "claude-fable-5",
            },
        },
    },
}

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration for the singleton Kōdo server.

    Attributes:
        port: TCP port for the WebSocket listener (loopback only).
        log_level: Python logging level name.
        extra: Full merged settings dict for use by the engine/gateway.
    """

    port: int = _DEFAULT_PORT
    log_level: str = _DEFAULT_LOG_LEVEL
    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> Config:
        """Parse CLI arguments and load the global settings file.

        Args:
            argv (list[str] | None): Argument list; defaults to ``sys.argv[1:]``.

        Returns:
            Config: Fully resolved configuration.
        """
        parser = argparse.ArgumentParser(
            prog="kodo-server",
            description="Kodo WebSocket server — one machine-wide singleton instance.",
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
            help=f"Logging level (default: {_DEFAULT_LOG_LEVEL}; overrides etc/settings.json).",
        )
        args = parser.parse_args(argv)

        _ensure_user_settings()
        settings = _load_settings()
        settings_log_level = str(settings.get("log_level", _DEFAULT_LOG_LEVEL))
        log_level = args.log_level if args.log_level is not None else settings_log_level

        return cls(
            port=args.port,
            log_level=log_level,
            extra=settings,
        )

    def reload_settings(self) -> dict[str, object]:
        """Re-read the global settings file from disk.

        Returns:
            dict[str, object]: Fresh merged settings (defaults ← file).
        """
        return _load_settings()


def _ensure_user_settings() -> None:
    """Write ``~/.kodo/etc/settings.json`` with defaults if it does not exist."""
    path = WorkspaceLayout().settings_json
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_DEFAULT_USER_SETTINGS, indent=2), encoding="utf-8")
    _log.info("Created default settings: %s", path)


def _load_settings() -> dict[str, object]:
    """Load the single global ``~/.kodo/etc/settings.json`` over compiled defaults.

    Returns:
        dict[str, object]: Merged settings (defaults overridden by the file).
    """
    merged: dict[str, object] = dict(_DEFAULT_USER_SETTINGS)

    path = WorkspaceLayout().settings_json
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Could not load settings from %s: %s", path, exc)

    return merged
