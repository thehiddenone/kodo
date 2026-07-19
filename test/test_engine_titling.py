"""Behavioral tests for :class:`kodo.runtime._engine._titling.SessionTitler`.

The old sub-agent-based titler ran a full LLM turn (10-15s); the current one
awaits :func:`kodo.titling.generate_title` directly (a chat-completion call
against the titler's own dedicated llama-server — genuinely async I/O) and
fires it from the queue worker without awaiting *that*. These tests
monkeypatch ``generate_title`` with an async stub (network-free,
deterministic) and drive :class:`SessionTitler` against a real
:class:`TransientStore` + a fake :class:`MessageSink`, asserting the
``session.naming``/``session.name`` event sequence and ``meta.json``
persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.common import Envelope
from kodo.runtime._engine import _titling
from kodo.runtime._engine._events import EngineEmitters
from kodo.runtime._engine._titling import SessionTitler
from kodo.runtime._session import SessionState
from kodo.state import TransientStore


class _FakeSink:
    def __init__(self) -> None:
        self.sent: list[Envelope] = []

    async def send(self, env: Envelope) -> None:
        self.sent.append(env)


class _FakeHost:
    _orch_session_id = "sess-1"


def _make_titler(tmp_path: Path) -> tuple[SessionTitler, _FakeSink, TransientStore]:
    transient = TransientStore(tmp_path)
    transient.attach_session("sess-1", resumed=False)
    sink = _FakeSink()
    session = SessionState(session_id="sess-1")
    emitters = EngineEmitters(sink, session, context_stats=lambda: {}, transient=transient)
    titler = SessionTitler(_FakeHost(), transient=transient, sink=sink, emitters=emitters)
    return titler, sink, transient


async def _drain(titler: SessionTitler) -> None:
    """Await the in-flight background titling task, if any."""
    if titler._naming_task is not None:
        await titler._naming_task


async def test_generates_and_persists_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen(text: str) -> str:
        return "csv export endpoint"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    # 11 words — over _MAX_TITLE_WORDS, so this goes through the summarizer
    # rather than being titled verbatim from the prompt itself.
    titler.maybe_generate_session_title("please add csv export to the reports page for our users")
    await _drain(titler)

    assert transient.is_session_named
    name_events = [env for env in sink.sent if env.payload.get("name")]
    assert len(name_events) == 1
    assert name_events[0].payload["name"] == "Csv Export Endpoint"
    assert name_events[0].payload["session_id"] == "sess-1"


async def test_naming_indicator_brackets_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gen(text: str) -> str:
        return "a real title here"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, _ = _make_titler(tmp_path)
    titler.maybe_generate_session_title(
        "please add a login page for our returning users again today"
    )
    await _drain(titler)

    naming_events = [env for env in sink.sent if "active" in env.payload]
    assert [e.payload["active"] for e in naming_events] == [True, False]


@pytest.mark.parametrize("prompt", ["   ", ""])
async def test_blank_prompt_is_a_noop(
    prompt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _gen(text: str) -> str:
        calls.append(text)
        return "x y z"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    titler.maybe_generate_session_title(prompt)
    await _drain(titler)

    assert calls == []
    assert not transient.is_session_named
    assert sink.sent == []


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("fix this", "Fix This"),
        ("help me", "Help Me"),
        ("hi", "Hi"),
        ("add a login page", "Add A Login Page"),
        ("add csv export to the reports page", "Add Csv Export To The Reports Page"),
        ("please fix this login bug, thanks!", "Please Fix This Login Bug Thanks"),
    ],
)
async def test_short_prompt_is_titled_from_its_own_words(
    prompt: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _MAX_TITLE_WORDS (8) or fewer words never reaches the summarizer — the
    # title is the prompt itself, sanitized (every case above is <= 8 words).
    calls: list[str] = []

    async def _gen(text: str) -> str:
        calls.append(text)
        return "x y z"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    titler.maybe_generate_session_title(prompt)
    await _drain(titler)

    assert calls == []
    assert transient.is_session_named
    name_events = [env for env in sink.sent if env.payload.get("name")]
    assert len(name_events) == 1
    assert name_events[0].payload["name"] == expected
    # No naming indicator for the instant, LLM-free path.
    assert not any("active" in env.payload for env in sink.sent)


async def test_nine_word_prompt_routes_through_the_summarizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One word over the _MAX_TITLE_WORDS (8) threshold — confirms the fork
    # boundary itself, not just "well over"/"well under" cases.
    calls: list[str] = []

    async def _gen(text: str) -> str:
        calls.append(text)
        return "nine word summary right here now"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    titler.maybe_generate_session_title("one two three four five six seven eight nine")
    await _drain(titler)

    assert len(calls) == 1
    assert transient.is_session_named
    naming_events = [env for env in sink.sent if "active" in env.payload]
    assert [e.payload["active"] for e in naming_events] == [True, False]


async def test_already_named_session_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def _gen(text: str) -> str:
        nonlocal called
        called = True
        return "some title"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    transient.set_session_name("Already Named")
    titler.maybe_generate_session_title("a new prompt")
    await _drain(titler)

    assert not called
    assert sink.sent == []


async def test_second_prompt_does_not_race_first_in_flight_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = 0

    async def _gen(text: str) -> str:
        nonlocal call_count
        call_count += 1
        return "first call title"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, _, _ = _make_titler(tmp_path)
    # Over _MAX_TITLE_WORDS, so both prompts route through the summarizer
    # (and thus the in-flight guard being tested here).
    titler.maybe_generate_session_title(
        "the first rather long user prompt about something interesting today please"
    )
    # Second prompt arrives before the first's background task has landed —
    # asyncio.create_task schedules but does not run the coroutine inline, so
    # _naming_task is already set and not done() by the time this runs.
    titler.maybe_generate_session_title(
        "the second rather long user prompt about something interesting today please"
    )
    await _drain(titler)

    assert call_count == 1


async def test_degenerate_output_falls_back_to_leading_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single bare word fails the 2-8 word acceptable-title band, so the
    # titler falls back to the prompt's own leading words.
    async def _gen(text: str) -> str:
        return "python"

    monkeypatch.setattr(_titling, "generate_title", _gen)

    titler, sink, transient = _make_titler(tmp_path)
    titler.maybe_generate_session_title(
        "some rather longer user prompt about something here today please now"
    )
    await _drain(titler)

    assert transient.is_session_named
    assert any(
        env.payload.get("name") == "Some Rather Longer User Prompt About Something Here"
        for env in sink.sent
    )


async def test_generation_failure_falls_back_to_leading_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(text: str) -> str:
        raise RuntimeError("titler llama-server not available")

    monkeypatch.setattr(_titling, "generate_title", _boom)

    titler, sink, transient = _make_titler(tmp_path)
    titler.maybe_generate_session_title(
        "some rather longer user prompt about something here today please now"
    )
    await _drain(titler)

    assert transient.is_session_named
    assert any(
        env.payload.get("name") == "Some Rather Longer User Prompt About Something Here"
        for env in sink.sent
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  csv export endpoint  ", "Csv Export Endpoint"),
        ('"quoted title"', "Quoted Title"),
        ("api rate limiter.", "Api Rate Limiter"),
        ("REST API Gateway", "REST API Gateway"),
        ("first line\nsecond line", "First Line"),
        ("fix bug, update docs!", "Fix Bug Update Docs"),
        ("a+b=c && d", "A B C D"),
        ("state's config", "State S Config"),
        ("front-end setup", "Front End Setup"),
        ("!!! ??? ...", None),
        # A full-prompt echo (>8 words) is clamped to the first _MAX_TITLE_WORDS,
        # which both fits the acceptable-title band and drops any degenerate tail.
        (
            "implement a game of tic tac toe with a CLI and tests",
            "Implement A Game Of Tic Tac Toe With",
        ),
        ("", None),
        ("   ", None),
    ],
)
def test_sanitize_title(raw: str, expected: str | None) -> None:
    assert SessionTitler._sanitize_title(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hi\nthere", "Hi There"),
        ("  fix   this   bug  ", "Fix This Bug"),
        (
            "one two three four five six seven eight nine ten",
            "One Two Three Four Five Six Seven Eight",
        ),
    ],
)
def test_sanitize_prompt_text_flattens_multiline_and_whitespace(text: str, expected: str) -> None:
    assert SessionTitler._sanitize_prompt_text(text) == expected


@pytest.mark.parametrize(
    ("title", "acceptable"),
    [
        (None, False),
        ("", False),
        ("Python", False),
        ("Csv Export Endpoint", True),
        ("A B C D E F G H", True),
        ("A B C D E F G H I", False),
    ],
)
def test_is_acceptable_title(title: str | None, acceptable: bool) -> None:
    assert SessionTitler._is_acceptable_title(title) is acceptable
