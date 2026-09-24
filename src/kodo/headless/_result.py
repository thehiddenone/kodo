"""The run result — the one machine-readable contract of a headless run.

Written as the final ``run.result`` stdout event, optionally to ``--result``,
and summarized on one ``KODO-RESULT outcome=… error=…`` line (for Harbor's
``ERROR_PATTERNS``, which match stdout text). ``schema_version`` is bumped on
any incompatible change (doc/HEADLESS.md §"Result").
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum

__all__ = ["RESULT_SCHEMA_VERSION", "RunOutcome", "RunResult"]

RESULT_SCHEMA_VERSION = 1


class RunOutcome(StrEnum):
    """How a headless run ended; each maps to one process exit code."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    STARTUP_ERROR = "startup_error"
    CRASHED = "crashed"
    STOPPED = "stopped"

    @property
    def exit_code(self) -> int:
        """The process exit code for this outcome."""
        return {
            RunOutcome.COMPLETED: 0,
            RunOutcome.TIMEOUT: 2,
            RunOutcome.RUNTIME_ERROR: 3,
            RunOutcome.STARTUP_ERROR: 4,
            RunOutcome.CRASHED: 5,
            RunOutcome.STOPPED: 130,
        }[self]


@dataclass(frozen=True)
class RunResult:
    """Everything a caller (Harbor's adapter, CI) needs about one run.

    Attributes:
        outcome: How the run ended (:class:`RunOutcome` value).
        session_id: The kodo session that ran.
        agent: Top-level agent requested.
        model: Local-registry entry the run used.
        final_phase: The session phase the turn rested in.
        assistant_text: The top-level agent's final visible response.
        cumulative_input_tokens: Every input token, cached or not.
        cumulative_input_tokens_uncached: The subset not served from cache.
        cumulative_output_tokens: Every generated token.
        cumulative_usd: Total cost (``0`` for local models).
        per_model: Per-model ``{calls, input_tokens, output_tokens, usd}``.
        per_agent: The same, per agent (top-level agent and every sub-agent).
        tool_calls: Tool calls dispatched.
        tool_denials: Calls the headless sandbox refused.
        questions_asked: Questions the agent asked though no user was present.
        nudges: Stuck-watchdog course corrections.
        wall_seconds: Run duration.
        error: The failure message, when not completed.
        schema_version: :data:`RESULT_SCHEMA_VERSION`.
    """

    outcome: str
    session_id: str
    agent: str
    model: str
    final_phase: str = ""
    assistant_text: str = ""
    cumulative_input_tokens: int = 0
    cumulative_input_tokens_uncached: int = 0
    cumulative_output_tokens: int = 0
    cumulative_usd: float = 0.0
    per_model: dict[str, dict[str, float]] = field(default_factory=dict)
    per_agent: dict[str, dict[str, float]] = field(default_factory=dict)
    tool_calls: int = 0
    tool_denials: int = 0
    questions_asked: int = 0
    nudges: int = 0
    wall_seconds: float = 0.0
    error: str | None = None
    schema_version: int = RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        """This result as a JSON-ready mapping.

        Returns:
            dict[str, object]: Every field.
        """
        return asdict(self)

    def to_json(self) -> str:
        """This result as indented JSON.

        Returns:
            str: The JSON document.
        """
        return json.dumps(self.to_dict(), indent=2)

    def summary_line(self) -> str:
        """The one greppable ``KODO-RESULT`` line.

        Returns:
            str: ``KODO-RESULT outcome=<outcome> error=<one-line message>``.
        """
        error = (self.error or "").replace("\n", " ").strip()
        return f"KODO-RESULT outcome={self.outcome} error={error}"
