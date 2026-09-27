"""Turn what a trial's kodo run left on disk into Harbor's ``AgentContext``.

The source of truth is ``kodo-result.json`` — ``kodo-headless``'s versioned
``RunResult`` (doc/HEADLESS.md §2.2). When Harbor's own agent timeout killed
the run before it wrote one, the per-call ``usage`` events already streamed to
``kodo.jsonl`` still account for every token spent, so the context is rebuilt
from those instead and marked ``result_source: "partial_log"``.

Token semantics follow ``AgentContext``: ``n_input_tokens`` includes cached
tokens, and ``n_cache_tokens`` counts cache *reads* (what Harbor's own
Claude Code adapter reports).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from harbor.models.agent.context import AgentContext, ModelUsage

from kodo.headless import RESULT_SCHEMA_VERSION

__all__ = ["KODO_JSONL", "KODO_RESULT_JSON", "KodoRunRecord"]

#: The run's stdout event stream, in the agent log directory.
KODO_JSONL = "kodo.jsonl"
#: The run's ``RunResult``, in the agent log directory.
KODO_RESULT_JSON = "kodo-result.json"

_ROW_KEYS = ("calls", "input_tokens", "cache_read_tokens", "output_tokens", "usd")
_METADATA_KEYS = (
    "outcome",
    "session_id",
    "agent",
    "model",
    "final_phase",
    "tool_calls",
    "tool_denials",
    "questions_asked",
    "nudges",
    "wall_seconds",
    "error",
    "per_agent",
)


class KodoRunRecord:
    """The accounting of one kodo run, read back from the agent log directory."""

    __result: dict[str, object]
    __per_model: dict[str, dict[str, float]]
    __source: str

    def __init__(
        self, result: dict[str, object], per_model: dict[str, dict[str, float]], source: str
    ) -> None:
        """Bind a run's result fields and per-model usage.

        Args:
            result (dict[str, object]): ``RunResult`` fields (possibly partial).
            per_model (dict[str, dict[str, float]]): Per-model usage rows.
            source (str): ``"result"`` or ``"partial_log"``.
        """
        self.__result = result
        self.__per_model = per_model
        self.__source = source

    @classmethod
    def load(cls, logs_dir: Path) -> KodoRunRecord | None:
        """Read the run's record from *logs_dir*.

        Args:
            logs_dir (Path): The trial's agent log directory (host side).

        Returns:
            KodoRunRecord | None: The record, or ``None`` when the run left
            neither a result nor a single usage event.
        """
        result = _read_json(logs_dir / KODO_RESULT_JSON)
        if result is not None:
            return cls(result, _rows(result.get("per_model")), "result")
        events = _read_events(logs_dir / KODO_JSONL)
        final = next((e for e in reversed(events) if e.get("type") == "run.result"), None)
        if final is not None:
            return cls(final, _rows(final.get("per_model")), "result")
        per_model: dict[str, dict[str, float]] = {}
        for event in events:
            if event.get("type") != "usage":
                continue
            row = per_model.setdefault(str(event.get("model") or "unknown"), _empty_row())
            row["calls"] += 1
            for key, field in (
                ("input_tokens", "input_tokens"),
                ("cache_read_tokens", "cache_read_tokens"),
                ("output_tokens", "output_tokens"),
                ("usd", "usd"),
            ):
                row[key] += _number(event.get(field))
        if not per_model:
            return None
        return cls({"outcome": "killed"}, per_model, "partial_log")

    @property
    def outcome(self) -> str:
        """The run's outcome (``"killed"`` when only a partial log exists)."""
        return str(self.__result.get("outcome", ""))

    @property
    def source(self) -> str:
        """Where the record came from: ``"result"`` or ``"partial_log"``."""
        return self.__source

    def fill(self, context: AgentContext, *, kodo_version: str) -> None:
        """Write tokens, cost, per-model usage and kodo's metadata into *context*.

        Args:
            context (AgentContext): The trial's context (Harbor's model).
            kodo_version (str): The py-kodo version the trial ran.
        """
        if self.__per_model:
            rows = self.__per_model.values()
            n_input = int(sum(r["input_tokens"] for r in rows))
            n_cache = int(sum(r["cache_read_tokens"] for r in rows))
            n_output = int(sum(r["output_tokens"] for r in rows))
            cost = round(sum(r["usd"] for r in rows), 6)
        else:
            n_input = int(_number(self.__result.get("cumulative_input_tokens")))
            uncached = int(_number(self.__result.get("cumulative_input_tokens_uncached")))
            n_cache = max(n_input - uncached, 0)
            n_output = int(_number(self.__result.get("cumulative_output_tokens")))
            cost = _number(self.__result.get("cumulative_usd"))
        context.n_input_tokens = n_input
        context.n_cache_tokens = n_cache
        context.n_output_tokens = n_output
        context.cost_usd = cost
        context.model_usage = {
            model: ModelUsage(
                n_input_tokens=int(row["input_tokens"]),
                n_cache_tokens=int(row["cache_read_tokens"]),
                n_output_tokens=int(row["output_tokens"]),
                cost_usd=row["usd"],
            )
            for model, row in self.__per_model.items()
        }
        metadata: dict[str, object] = {
            key: self.__result[key] for key in _METADATA_KEYS if key in self.__result
        }
        metadata["kodo_version"] = kodo_version
        metadata["result_source"] = self.__source
        metadata["result_schema_version"] = self.__result.get(
            "schema_version", RESULT_SCHEMA_VERSION
        )
        context.metadata = {"kodo": metadata}


def _empty_row() -> dict[str, float]:
    return {key: 0.0 if key == "usd" else 0 for key in _ROW_KEYS}


def _rows(raw: object) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict):
        return {}
    rows: dict[str, dict[str, float]] = {}
    for model, values in cast(dict[str, object], raw).items():
        if not isinstance(values, dict):
            continue
        source = cast(dict[str, object], values)
        row = _empty_row()
        for key in _ROW_KEYS:
            row[key] = _number(source.get(key))
        rows[str(model)] = row
    return rows


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else None


def _read_events(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue  # the trailing KODO-RESULT line, or a truncated write
        try:
            loaded: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            events.append(cast(dict[str, object], loaded))
    return events
