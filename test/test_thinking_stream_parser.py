"""Tests for ThinkingStreamParser eager-flush behaviour.

Regression coverage for the "incomplete sentence then a long silence, then the
last few characters arrive alongside the tool call" symptom: the parser used to
always withhold the last few characters of every chunk (guarding against a
``<think>`` tag split across chunks), so a sentence tail like ``"both."`` sat in
the buffer until the very end of the stream. It must now be emitted immediately
when it cannot possibly be the start of a tag.
"""

from __future__ import annotations

from kodo.llms import TokenDelta
from kodo.llms.llamacpp import ThinkingStreamParser


def _token_text(events: list[object]) -> str:
    return "".join(e.text for e in events if isinstance(e, TokenDelta))


def test_plain_text_tail_flushes_immediately() -> None:
    parser = ThinkingStreamParser()
    # No '<' anywhere: every character is safe to emit now; nothing is withheld.
    events = parser.feed("Let me fix both.")
    assert _token_text(events) == "Let me fix both."
    assert parser.flush() == []


def test_only_partial_tag_prefix_is_withheld() -> None:
    parser = ThinkingStreamParser()
    # Trailing "<th" could become "<think>", so only those 3 chars are held.
    events = parser.feed("ready <th")
    assert _token_text(events) == "ready "
    # The next chunk completes the tag; the prefix text was never lost.
    more = parser.feed("ink>reasoning</think>done")
    assert _token_text(more) == "done"


def test_lone_open_bracket_then_plain_text_is_not_a_tag() -> None:
    parser = ThinkingStreamParser()
    events = parser.feed("a < b")  # '<' followed by space is not a tag start
    assert _token_text(events) == "a < b"
    assert parser.flush() == []
