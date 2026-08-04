"""Unit tests for kodo.runtime._think_tag_guard.ThinkTagDetector.

Pure algorithm tests, no engine involved -- see test_engine_watchdog.py for
how the detector is wired into the real streaming loop.
"""

from __future__ import annotations

from kodo.runtime._think_tag_guard import ThinkTagDetector


def _feed_chunks(detector: ThinkTagDetector, text: str, size: int = 3) -> bool:
    """Feed *text* in small fragments (mirrors real sub-token streaming
    granularity), stopping as soon as (if ever) the tag fires."""
    return any(detector.feed(text[i : i + size]) for i in range(0, len(text), size))


def test_no_tag_never_fires() -> None:
    detector = ThinkTagDetector()
    text = '{"task": "build the create_measurements.cpp generator"}'
    assert _feed_chunks(detector, text) is False


def test_tag_fires_in_a_single_fragment() -> None:
    detector = ThinkTagDetector()
    assert detector.feed('{"task": "<think>let me plan this out</think> build it"}') is True


def test_tag_fires_when_split_across_fragment_boundary() -> None:
    """The exact shape of the motivating incident: the tag arrives token by
    token, so the check must not require the whole tag in one fragment."""
    detector = ThinkTagDetector()
    text = '{"task": "<think>reasoning here</think>"}'
    assert _feed_chunks(detector, text, size=1) is True


def test_tag_split_exactly_at_the_boundary() -> None:
    """Regression case for the boundary-safe tail: the split lands exactly
    inside the tag token itself ("<thi" | "nk>")."""
    detector = ThinkTagDetector()
    assert detector.feed('{"task": "<thi') is False
    assert detector.feed('nk>plan</think>"}') is True


def test_fires_only_once_the_open_tag_is_complete() -> None:
    detector = ThinkTagDetector()
    assert detector.feed('{"task": "<thin') is False
    assert detector.feed("k") is False
    assert detector.feed('>now thinking"}') is True


def test_ordinary_angle_brackets_do_not_false_positive() -> None:
    """A tool call legitimately using '<' (e.g. a comparison in generated
    code) must not be mistaken for a thinking tag."""
    detector = ThinkTagDetector()
    text = '{"content": "if (a < b) { return c; }"}'
    assert _feed_chunks(detector, text) is False


def test_empty_fragment_is_a_noop() -> None:
    detector = ThinkTagDetector()
    assert detector.feed("") is False
