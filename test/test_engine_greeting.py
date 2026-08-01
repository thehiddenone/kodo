"""Behavioral tests for :class:`kodo.runtime._engine._greeting.SessionGreeter`.

Mirrors ``test_engine_titling.py``'s approach: monkeypatch
``kodo.titling.generate_greeting`` with an async stub (network-free,
deterministic) and drive :class:`SessionGreeter` against a real
:class:`TransientStore` + a fake :class:`MessageSink`, asserting the
persisted ``greeting`` marker and the live ``session.greeting`` event, plus
the fallback-to-default behavior when the titler is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.runtime._engine import _greeting
from kodo.runtime._engine._events import EngineEmitters
from kodo.runtime._engine._greeting import _DEFAULT_GREETING, SessionGreeter
from kodo.runtime._session import SessionState
from kodo.state import TransientStore


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[Envelope] = []

    async def send(self, env: Envelope) -> None:
        self.sent.append(env)


def _make_greeter(tmp_path: Path) -> tuple[SessionGreeter, _FakeSink, TransientStore]:
    transient = TransientStore(tmp_path)
    transient.attach_session("sess-1", resumed=False)
    sink = _FakeSink()
    session = SessionState(session_id="sess-1")
    emitters = EngineEmitters(sink, session, context_stats=lambda: {}, transient=transient)
    greeter = SessionGreeter(emitters=emitters)
    return greeter, sink, transient


async def _drain(greeter: SessionGreeter) -> None:
    """Await the in-flight background greeting task, if any."""
    if greeter._task is not None:
        await greeter._task


async def test_generates_persists_and_pushes_greeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen() -> str:
        return "Hello! Ready to build something new today?"

    monkeypatch.setattr(_greeting, "generate_greeting", _gen)

    greeter, sink, transient = _make_greeter(tmp_path)
    greeter.start()
    await _drain(greeter)

    greeting_events = [env for env in sink.sent if env.payload.get("type") == "session.greeting"]
    assert len(greeting_events) == 1
    assert greeting_events[0].payload["text"] == "Hello! Ready to build something new today?"

    lines = transient.read_session_lines()
    markers = [ln for ln in lines if ln.get("type") == "greeting"]
    assert len(markers) == 1
    assert markers[0]["text"] == "Hello! Ready to build something new today?"


async def test_falls_back_to_default_when_titler_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen() -> None:
        return None

    monkeypatch.setattr(_greeting, "generate_greeting", _gen)

    greeter, sink, _transient = _make_greeter(tmp_path)
    greeter.start()
    await _drain(greeter)

    greeting_events = [env for env in sink.sent if env.payload.get("type") == "session.greeting"]
    assert len(greeting_events) == 1
    assert greeting_events[0].payload["text"] == _DEFAULT_GREETING


async def test_falls_back_to_default_when_generation_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen() -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(_greeting, "generate_greeting", _gen)

    greeter, sink, _transient = _make_greeter(tmp_path)
    greeter.start()
    await _drain(greeter)

    greeting_events = [env for env in sink.sent if env.payload.get("type") == "session.greeting"]
    assert len(greeting_events) == 1
    assert greeting_events[0].payload["text"] == _DEFAULT_GREETING


async def test_strips_whitespace_and_wrapping_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen() -> str:
        return '  "Hello there, let\'s build something."  '

    monkeypatch.setattr(_greeting, "generate_greeting", _gen)

    greeter, sink, _transient = _make_greeter(tmp_path)
    greeter.start()
    await _drain(greeter)

    greeting_events = [env for env in sink.sent if env.payload.get("type") == "session.greeting"]
    assert greeting_events[0].payload["text"] == "Hello there, let's build something."


async def test_second_start_call_while_in_flight_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def _gen() -> str:
        nonlocal calls
        calls += 1
        return "Hello!"

    monkeypatch.setattr(_greeting, "generate_greeting", _gen)

    greeter, _sink, _transient = _make_greeter(tmp_path)
    greeter.start()
    greeter.start()
    await _drain(greeter)

    assert calls == 1
