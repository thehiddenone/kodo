"""Tool-call logger.

Writes a pair of pretty-printed JSON files for every tool invocation
and its result within an agent turn:

    <log_dir>/{N:04d}_{tool_name}_{T:02d}_invocation.json
    <log_dir>/{N:04d}_{tool_name}_{T:02d}_result.json

N is a process-wide monotonically increasing turn counter (one increment
per WorkflowEngine agent-turn call).  T is the tool-call sequence number
within that turn, starting at 1.
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["ToolCallLogger"]

_log = logging.getLogger(__name__)
_turn_counter = itertools.count(1)


class ToolCallLogger:
    """Logs every tool invocation and result to disk for one agent turn."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._turn_n = next(_turn_counter)
        self._seq = itertools.count(1)

    def log_invocation(self, tool_name: str, tool_input: dict[str, object]) -> int:
        """Write the invocation file and return the tool-call sequence number."""
        tc_n = next(self._seq)
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        path = self._log_dir / f"{self._turn_n:04d}_{tool_name}_{tc_n:02d}_invocation.json"
        data: dict[str, object] = {
            "turn_n": self._turn_n,
            "tool_call_n": tc_n,
            "tool_name": tool_name,
            "timestamp": ts,
            "input": tool_input,
        }
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write tool invocation log %s: %s", path, exc)
        return tc_n

    def log_result(self, tool_name: str, tc_n: int, result_text: str) -> None:
        """Write the result file."""
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        path = self._log_dir / f"{self._turn_n:04d}_{tool_name}_{tc_n:02d}_result.json"
        try:
            result_obj: object = json.loads(result_text)
        except json.JSONDecodeError:
            result_obj = {"_raw": result_text}
        data: dict[str, object] = {
            "turn_n": self._turn_n,
            "tool_call_n": tc_n,
            "tool_name": tool_name,
            "timestamp": ts,
            "result": result_obj,
        }
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write tool result log %s: %s", path, exc)
