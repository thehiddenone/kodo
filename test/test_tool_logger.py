"""Behavioral tests for :class:`kodo.llms.ToolCallLogger`.

Every tool call in an agent turn leaves a pretty-printed JSON invocation file
and a matching result file under the logger's directory; a write failure is
logged as a warning and never interrupts the turn.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kodo.llms import ToolCallLogger


def _only(log_dir: Path, suffix: str) -> dict[str, object]:
    files = sorted(log_dir.glob(f"*{suffix}"))
    assert len(files) == 1, files
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert isinstance(body, dict)
    return body


def test_an_invocation_is_written_and_numbered_from_one(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs" / "nested"
    logger = ToolCallLogger(log_dir)

    tc_n = logger.log_invocation("read_file", {"path": "a.txt"})

    assert tc_n == 1
    body = _only(log_dir, "_invocation.json")
    assert body["tool_call_n"] == 1
    assert body["tool_name"] == "read_file"
    assert body["input"] == {"path": "a.txt"}
    assert isinstance(body["timestamp"], str) and body["timestamp"]
    turn_n = body["turn_n"]
    assert isinstance(turn_n, int)
    assert (log_dir / f"{turn_n:04d}_read_file_01_invocation.json").is_file()


def test_tool_calls_within_a_turn_are_numbered_in_sequence(tmp_path: Path) -> None:
    logger = ToolCallLogger(tmp_path)

    numbers = [logger.log_invocation(f"tool_{i}", {}) for i in range(3)]

    assert numbers == [1, 2, 3]
    turns = {
        json.loads(p.read_text(encoding="utf-8"))["turn_n"]
        for p in tmp_path.glob("*_invocation.json")
    }
    assert len(turns) == 1


def test_each_logger_is_a_later_turn(tmp_path: Path) -> None:
    first, second = ToolCallLogger(tmp_path / "a"), ToolCallLogger(tmp_path / "b")
    first.log_invocation("t", {})
    second.log_invocation("t", {})

    first_turn = _only(tmp_path / "a", "_invocation.json")["turn_n"]
    second_turn = _only(tmp_path / "b", "_invocation.json")["turn_n"]

    assert isinstance(first_turn, int) and isinstance(second_turn, int)
    assert second_turn > first_turn


def test_a_json_result_is_stored_parsed(tmp_path: Path) -> None:
    logger = ToolCallLogger(tmp_path)
    tc_n = logger.log_invocation("read_file", {"path": "a.txt"})

    logger.log_result("read_file", tc_n, json.dumps({"content": "hello", "lines": 1}))

    body = _only(tmp_path, "_result.json")
    assert body["tool_call_n"] == tc_n
    assert body["tool_name"] == "read_file"
    assert body["result"] == {"content": "hello", "lines": 1}
    turn_n = body["turn_n"]
    assert isinstance(turn_n, int)
    assert (tmp_path / f"{turn_n:04d}_read_file_01_result.json").is_file()


def test_a_non_json_result_is_stored_raw(tmp_path: Path) -> None:
    logger = ToolCallLogger(tmp_path)
    tc_n = logger.log_invocation("shell", {})

    logger.log_result("shell", tc_n, "plain text, not JSON")

    assert _only(tmp_path, "_result.json")["result"] == {"_raw": "plain text, not JSON"}


def _fail_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", refuse)


def test_a_failed_invocation_write_is_logged_and_still_numbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = ToolCallLogger(tmp_path)
    _fail_writes(monkeypatch)

    with caplog.at_level(logging.WARNING):
        first = logger.log_invocation("read_file", {})
        second = logger.log_invocation("read_file", {})

    assert (first, second) == (1, 2)
    assert list(tmp_path.iterdir()) == []
    assert "Failed to write tool invocation log" in caplog.text
    assert "disk full" in caplog.text


def test_a_failed_result_write_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = ToolCallLogger(tmp_path)
    tc_n = logger.log_invocation("read_file", {})
    _fail_writes(monkeypatch)

    with caplog.at_level(logging.WARNING):
        logger.log_result("read_file", tc_n, "{}")

    assert list(tmp_path.glob("*_result.json")) == []
    assert "Failed to write tool result log" in caplog.text
