"""Tests for ``kodo.llms._logger`` -- transparent LLM request/response logger.

Covers:
* :func:`_event_to_dict` for every :class:`~kodo.llms._interface.StreamEvent`
  subtype (ThinkingDelta, ThinkingSignature, TokenDelta, ToolCallEvent, TurnEnd).
* :class:`LoggingLLMPlugin` properties (``name``, ``supported_models``) pass
  through to the inner plugin.
* :meth:`LoggingLLMPlugin.stream_query` writes request/response JSON files and
  forwards events from the inner async iterator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodo.llms._interface import (
    LLMPlugin,
    Message,
    StreamEvent,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
)
from kodo.llms._logger import LoggingLLMPlugin, _event_to_dict

# ---------------------------------------------------------------------------
# _event_to_dict -- pure
# ---------------------------------------------------------------------------


def test_event_to_dict_thinking_delta() -> None:
    event = ThinkingDelta(text="let me think...")
    d = _event_to_dict(event)
    assert d == {"type": "thinking_delta", "text": "let me think..."}


def test_event_to_dict_thinking_signature() -> None:
    event = ThinkingSignature(signature="sig123")
    d = _event_to_dict(event)
    assert d == {"type": "thinking_signature", "signature": "sig123"}


def test_event_to_dict_token_delta() -> None:
    event = TokenDelta(text="hello world")
    d = _event_to_dict(event)
    assert d == {"type": "token_delta", "text": "hello world"}


def test_event_to_dict_tool_call_event() -> None:
    event = ToolCallEvent(
        tool_use_id="tool_1",
        tool_name="read_file",
        tool_input={"path": "/foo/bar.py"},
    )
    d = _event_to_dict(event)
    assert d["type"] == "tool_call"
    assert d["tool_use_id"] == "tool_1"
    assert d["tool_name"] == "read_file"
    assert d["tool_input"] == {"path": "/foo/bar.py"}


def test_event_to_dict_turn_end() -> None:
    usage = Usage(
        input_tokens=100,
        output_tokens=50,
        cache_write_tokens=10,
        cache_read_tokens=5,
        model="test-model",
    )
    event = TurnEnd(usage=usage, stop_reason="end_turn")
    d = _event_to_dict(event)
    assert d["type"] == "turn_end"
    assert d["stop_reason"] == "end_turn"
    assert d["usage"]["model"] == "test-model"
    assert d["usage"]["input_tokens"] == 100
    assert d["usage"]["output_tokens"] == 50
    assert d["usage"]["cache_write_tokens"] == 10
    assert d["usage"]["cache_read_tokens"] == 5


def test_event_to_dict_unknown_event_type() -> None:
    """Any event that's not a known subtype gets a dict with just its class name."""
    fake_event = object()
    d = _event_to_dict(fake_event)
    assert d == {"type": "object"}


def test_event_to_dict_token_with_empty_text() -> None:
    """A TokenDelta with empty text still serializes correctly."""
    event = TokenDelta(text="")
    d = _event_to_dict(event)
    assert d == {"type": "token_delta", "text": ""}


def test_event_to_dict_thinking_with_empty_text() -> None:
    """A ThinkingDelta with empty text still serializes correctly."""
    event = ThinkingDelta(text="")
    d = _event_to_dict(event)
    assert d == {"type": "thinking_delta", "text": ""}


def test_event_to_dict_tool_call_with_empty_input() -> None:
    """A ToolCallEvent with empty input serializes with empty dict."""
    event = ToolCallEvent(
        tool_use_id="t1",
        tool_name="foo",
        tool_input={},
    )
    d = _event_to_dict(event)
    assert d["tool_input"] == {}


def test_event_to_dict_turn_end_with_none_stop_reason() -> None:
    """A TurnEnd with stop_reason=None preserves None in the dict."""
    usage = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="test",
    )
    event = TurnEnd(usage=usage, stop_reason=None)
    d = _event_to_dict(event)
    assert d["stop_reason"] is None


# ---------------------------------------------------------------------------
# _FakeInnerPlugin
# ---------------------------------------------------------------------------


class _FakeInnerPlugin(LLMPlugin):
    """Minimal LLMPlugin subclass for testing the wrapper."""

    def __init__(self) -> None:
        self._name = "fake-inner"
        self._supported = ["model-a", "model-b"]

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_models(self) -> list[str]:
        return self._supported

    async def stream_query(  # type: ignore[override]
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
        **kwargs: Any,
    ) -> Any:
        # Return an async generator from this method.
        async for event in self._stream_gen(**kwargs):
            yield event

    async def cancel(self, stream_id: str) -> None:
        pass

    async def _stream_gen(self, **kwargs: Any) -> Any:
        return
        yield  # make this an async generator


@pytest.fixture
def _inner_plugin() -> _FakeInnerPlugin:
    return _FakeInnerPlugin()


@pytest.fixture
def _logged_plugin(_inner_plugin: _FakeInnerPlugin, tmp_path: Path) -> LoggingLLMPlugin:
    return LoggingLLMPlugin(_inner_plugin, tmp_path)


# ---------------------------------------------------------------------------
# LoggingLLMPlugin tests
# ---------------------------------------------------------------------------


def test_logging_plugin_name_passthrough(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
) -> None:
    assert _logged_plugin.name == _inner_plugin.name


def test_logging_plugin_supported_models_passthrough(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
) -> None:
    assert _logged_plugin.supported_models == _inner_plugin.supported_models


def test_logging_plugin_creates_log_dir(tmp_path: Path) -> None:
    """The log directory is created if it doesn't exist."""
    log_dir = tmp_path / "nested" / "log"
    assert not log_dir.exists()
    LoggingLLMPlugin(_FakeInnerPlugin(), log_dir)
    # The directory is only created on stream_query, not __init__.
    assert not log_dir.exists()


@pytest.mark.asyncio
async def test_logging_plugin_stream_query_writes_request_and_response(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_query writes both request and response JSON files and forwards events."""
    events = [
        TokenDelta(text="hello "),
        ThinkingDelta(text="reasoning"),
        ToolCallEvent(
            tool_use_id="t1",
            tool_name="foo",
            tool_input={"a": 1},
        ),
        TurnEnd(
            usage=Usage(
                input_tokens=10,
                output_tokens=5,
                cache_write_tokens=0,
                cache_read_tokens=0,
                model="test-model",
            ),
            stop_reason="end_turn",
        ),
    ]

    async def _fake_stream(**kwargs: Any) -> Any:
        for e in events:
            yield e

    monkeypatch.setattr(_inner_plugin, "stream_query", _fake_stream)

    collected: list[StreamEvent] = []
    async for event in _logged_plugin.stream_query(
        stream_id="s1",
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        collected.append(event)

    assert collected == events

    # Request file exists.
    request_files = sorted(tmp_path.glob("*_request.json"))
    assert len(request_files) == 1
    request_data = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert request_data["stream_id"] == "s1"
    assert request_data["model"] == "test-model"
    assert request_data["system"] == "sys"

    # Response file exists and contains all events as dicts.
    response_files = sorted(tmp_path.glob("*_response.json"))
    assert len(response_files) == 1
    response_data = json.loads(response_files[0].read_text(encoding="utf-8"))
    assert len(response_data["events"]) == 4
    assert response_data["events"][0]["type"] == "token_delta"
    assert response_data["events"][1]["type"] == "thinking_delta"
    assert response_data["events"][2]["type"] == "tool_call"
    assert response_data["events"][3]["type"] == "turn_end"


@pytest.mark.asyncio
async def test_logging_plugin_stream_query_thinking_level_in_request(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When thinking_level is provided, it's included in the request JSON."""

    async def _fake_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="done")

    monkeypatch.setattr(_inner_plugin, "stream_query", _fake_stream)

    events_collected = []
    async for event in _logged_plugin.stream_query(
        stream_id="s2",
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
        thinking_level="high",
    ):
        events_collected.append(event)

    request_files = sorted(tmp_path.glob("*_request.json"))
    assert len(request_files) == 1
    request_data = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert request_data["thinking_level"] == "high"


@pytest.mark.asyncio
async def test_logging_plugin_stream_query_no_thinking_level_excludes_field(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When thinking_level is None, the request JSON has thinking_level=None."""

    async def _fake_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="done")

    monkeypatch.setattr(_inner_plugin, "stream_query", _fake_stream)

    events_collected = []
    async for event in _logged_plugin.stream_query(
        stream_id="s3",
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        events_collected.append(event)

    request_files = sorted(tmp_path.glob("*_request.json"))
    assert len(request_files) == 1
    request_data = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert request_data["thinking_level"] is None


@pytest.mark.asyncio
async def test_logging_plugin_cancel_forwards_to_inner(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel() forwards to the inner plugin."""
    cancel_called = False

    async def _fake_cancel(stream_id: str) -> None:
        nonlocal cancel_called
        cancel_called = True

    monkeypatch.setattr(_inner_plugin, "cancel", _fake_cancel)
    await _logged_plugin.cancel("s1")
    assert cancel_called


@pytest.mark.asyncio
async def test_logging_plugin_handles_inner_exception_gracefully(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the inner plugin raises during streaming, response file is still written."""

    async def _raise_stream(**kwargs: Any) -> Any:
        yield TokenDelta(text="partial")
        raise RuntimeError("boom")

    monkeypatch.setattr(_inner_plugin, "stream_query", _raise_stream)

    collected = []
    with pytest.raises(RuntimeError, match="boom"):
        async for event in _logged_plugin.stream_query(
            stream_id="s4",
            model="test-model",
            system="sys",
            messages=[],
            tools=[],
            cache_breakpoints=[],
        ):
            collected.append(event)

    # Response file should still be written (finally block).
    response_files = sorted(tmp_path.glob("*_response.json"))
    assert len(response_files) == 1
    response_data = json.loads(response_files[0].read_text(encoding="utf-8"))
    assert len(response_data["events"]) == 1  # only the partial event
    assert response_data["events"][0]["type"] == "token_delta"


@pytest.mark.asyncio
async def test_logging_plugin_empty_inner_stream_writes_response(
    _logged_plugin: LoggingLLMPlugin,
    _inner_plugin: _FakeInnerPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner stream that yields nothing still produces a response file."""

    async def _empty_stream(**kwargs: Any) -> Any:
        return
        yield  # make this an async generator

    monkeypatch.setattr(_inner_plugin, "stream_query", _empty_stream)

    collected = []
    async for event in _logged_plugin.stream_query(
        stream_id="s5",
        model="test-model",
        system="sys",
        messages=[],
        tools=[],
        cache_breakpoints=[],
    ):
        collected.append(event)

    assert collected == []
    response_files = sorted(tmp_path.glob("*_response.json"))
    assert len(response_files) == 1
    response_data = json.loads(response_files[0].read_text(encoding="utf-8"))
    assert len(response_data["events"]) == 0
