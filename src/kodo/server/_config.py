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
from kodo.titling import DEFAULT_HOUSEKEEPER_LLM_ID

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
    # kodo/llms/_cloud_registry.py or kodo/llms/local_registry/), so
    # switching models changes the limit and the auto-compaction threshold.
    # See ContextCompactor.context_limit (runtime/_engine/_compaction.py) and
    # doc/STATE_AND_LIFECYCLE.md §4.5.
    "models": {
        "local": "llamacpp-qwen36-27b-q4-k-xl",
        "cloud": {
            "anthropic": {
                "low": "claude-haiku-4-5-20251001",
                "medium": "claude-sonnet-5",
                "high": "claude-opus-5",
                "max": "claude-fable-5",
            },
            # Only 3 GPT-5.6 SKUs exist (kodo/llms/_cloud_registry.py) for 4
            # effort tiers — Terra covers both "medium" and "high", Sol is
            # reserved for "max" only.
            "openai": {
                "low": "gpt-5.6-luna",
                "medium": "gpt-5.6-terra",
                "high": "gpt-5.6-terra",
                "max": "gpt-5.6-sol",
            },
            # Meta has no effort-tiered lineup (kodo/llms/_cloud_registry.py's
            # _META_MODELS) -- Muse Spark 1.2 is the one model on offer, so it
            # is assigned to all four tiers.
            "meta": {
                "low": "muse-spark-1.2",
                "medium": "muse-spark-1.2",
                "high": "muse-spark-1.2",
                "max": "muse-spark-1.2",
            },
            # Two Gemini SKUs exist (kodo/llms/_cloud_registry.py's
            # _GOOGLE_MODELS) for 4 effort tiers -- gemini-3.6-flash covers
            # medium/high/max, gemini-3.5-flash-lite is reserved for low.
            "google": {
                "low": "gemini-3.5-flash-lite",
                "medium": "gemini-3.6-flash",
                "high": "gemini-3.6-flash",
                "max": "gemini-3.6-flash",
            },
            # Three Qwen3.8 SKUs exist (kodo/llms/_cloud_registry.py's
            # _ALIBABA_MODELS) for 4 effort tiers -- qwen3.8-plus covers both
            # "medium" and "high", same shape as the openai row above,
            # qwen3.8-flash is reserved for low, qwen3.8-max for max.
            "alibaba": {
                "low": "qwen3.8-flash",
                "medium": "qwen3.8-plus",
                "high": "qwen3.8-plus",
                "max": "qwen3.8-max",
            },
        },
    },
    # Meta's discounted "contributor" tier: trades training-data permission
    # for heavily discounted Muse Spark 1.2 pricing (kodo/llms/meta/_usage.py).
    # Off by default -- turning it on is an explicit opt-in surfaced in the
    # Cloud AI Settings webview's Meta tab (real-world eligibility is
    # country-restricted; see that tab's warning copy). Read fresh per LLM
    # dispatch by kodo/runtime/_engine/_llm.py's Meta plugin factory, exactly
    # like active_cloud_vendor/models.cloud above -- not a dedicated WS
    # command, since it has no server-side validation to run (unlike
    # housekeeper_llm.set) and no side effect beyond which model id the next
    # Meta call uses.
    "meta_contributor_tier": False,
    # Governs the stuck-agent watchdog (kodo.runtime._engine._watchdog,
    # doc/STUCK_DETECTION.md) — detects a turn that ended without finishing
    # its task (e.g. an empty final response, or a truncated generation) and
    # nudges the agent to continue. Exposed in the Kōdo Settings webview
    # panel's "General" section via the stuck_detection.get/.set WS commands
    # (doc/WS_PROTOCOL.md §7.6d); this block is still the ground truth.
    "stuck_detection": {
        # "off" | "local_only" | "local_and_cloud" — which model residence
        # the watchdog runs for. Local LLMs are the primary target (this is
        # a small/quantized-model failure mode cloud models rarely exhibit).
        "active": "local_only",
        # "top_level" | "top_level_and_subagents" — whether only the main
        # entry agent (Guide/Problem Solver) is watched, or sub-agents
        # (run_subagent_<name>) too.
        "scope": "top_level",
        # In interactive mode, whether a detected stall is nudged
        # automatically (True) or surfaced as a prompt.stuck_alert the user
        # must confirm (False). Autonomous mode always nudges immediately,
        # regardless of this flag.
        "auto_unstuck_interactive": False,
    },
    # Which small local model backs session titling/greeting (kodo.titling,
    # doc/INTERNALS.md §10c). A key into kodo.titling.HOUSEKEEPER_LLM_OPTIONS
    # — exposed in the Kōdo Settings webview panel's "General" section via
    # the housekeeper_llm.get/.set WS commands (doc/WS_PROTOCOL.md §7.6f).
    "housekeeper_llm": DEFAULT_HOUSEKEEPER_LLM_ID,
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
