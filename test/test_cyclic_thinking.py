"""Unit tests for kodo.runtime._cyclic_thinking.CyclicThinkingDetector.

Pure algorithm tests, no engine involved -- see test_engine_watchdog.py for
how the detector is wired into the real streaming loop.
"""

from __future__ import annotations

import json
import random

from kodo.runtime._cyclic_thinking import (
    _ARGS_MIN_BUFFERED_CHARS,
    _MAX_PERIOD,
    _MIN_PERIOD,
    _MIN_REPEATS,
    CyclicThinkingDetector,
)

_VOCAB = [
    "system", "value", "threshold", "compute", "review", "dataset", "vector",
    "result", "matrix", "signal", "output", "kernel", "buffer", "stream",
    "config", "handler", "module", "record", "schema", "policy", "metric",
    "session", "cluster", "index", "queue", "cache", "socket", "thread",
    "graph", "table", "because", "however", "therefore", "although",
    "considering", "meanwhile", "additionally", "specifically", "generally",
    "roughly", "precisely", "unlikely", "reasonable", "surprising",
    "consistent", "variable", "constant", "boundary", "interval",
    "sequence", "pattern", "structure", "behavior", "outcome", "scenario",
    "hypothesis", "assumption", "conclusion", "evidence", "argument",
    "function", "iteration", "recursion", "exception", "latency",
    "throughput", "concurrency", "allocation", "deadlock", "heuristic",
    "invariant", "partition", "checksum", "fingerprint", "telemetry",
    "anomaly", "regression", "baseline", "calibration",
]  # fmt: skip


def _feed_chunks(detector: CyclicThinkingDetector, text: str, size: int = 3) -> bool:
    """Feed *text* in small fragments (mirrors real sub-token streaming
    granularity), stopping as soon as (if ever) a cycle fires."""
    return any(detector.feed(text[i : i + size]) for i in range(0, len(text), size))


def _long_healthy_text(num_words: int = 3000, seed: int = 1234) -> str:
    """Several KB of varied, non-repeating prose -- a large enough vocabulary
    drawn in random order that no coincidental exact or near-duplicate match
    is expected, unlike either a single fixed phrase or a tiny vocabulary
    would produce."""
    rng = random.Random(seed)
    words = [rng.choice(_VOCAB) for _ in range(num_words)]
    return " ".join(words) + "."


# ---------------------------------------------------------------------------
# Exact block-repeat
# ---------------------------------------------------------------------------


def test_exact_repeat_fires_on_third_repeat_not_second() -> None:
    block = "The reasoning loop keeps repeating here in exactly this way!\n"
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, block * 2) is False
    assert _feed_chunks(detector, block) is True


def test_exact_repeat_is_phase_agnostic() -> None:
    """The repeat need not start at the beginning of the buffer -- a unique
    prefix precedes it here, proving detection doesn't depend on buffer-start
    alignment."""
    prefix = "".join(f"w{i} " for i in range(20))  # unrelated unique lead-in
    block = "abcdefghijklmnopqrstuvwxyz012345\n"  # period-33 block
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, prefix + block * 2) is False
    assert _feed_chunks(detector, block) is True


def test_exact_repeat_detected_when_fed_in_single_character_fragments() -> None:
    """Real providers stream sub-token fragments, sometimes a single
    character at a time -- detection must not depend on feed() being called
    with whole-line granularity."""
    block = "same three lines over and over again here\n"
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, block * 3, size=1) is True


def test_exact_repeat_fires_at_both_ends_of_the_period_range() -> None:
    """The shortest and longest admitted period lengths both fire.

    ``_check_exact_repeat`` locates candidate periods by searching for a
    repeat of the buffer's trailing ``_MIN_PERIOD`` characters rather than
    trying each length in turn, so the two ends of ``[_MIN_PERIOD,
    _MAX_PERIOD]`` are exactly where an off-by-one in that search would show
    up -- neither is exercised by the line-scale blocks used elsewhere here.
    """
    for period in (_MIN_PERIOD, _MAX_PERIOD):
        block = "".join(_VOCAB[i % len(_VOCAB)][0] for i in range(period))
        detector = CyclicThinkingDetector()

        assert _feed_chunks(detector, block * _MIN_REPEATS) is True, period


def test_repeat_shorter_than_the_minimum_period_never_fires() -> None:
    """A block one character below ``_MIN_PERIOD``, repeated the required
    number of times, stays below the floor and must not fire -- the floor is
    what keeps word-scale repetition out (see ``_MIN_PERIOD``'s rationale)."""
    block = "x" * (_MIN_PERIOD - 1)
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, block * _MIN_REPEATS) is False


def test_two_repeats_of_a_long_period_never_fires() -> None:
    """Exactly two occurrences (not three) of an otherwise-substantial
    repeated period must not fire -- proves the >= 3 requirement, not just
    "more than once"."""
    sentence = "Let me reconsider the edge cases for this function once more.\n"
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, sentence * 2 + _long_healthy_text(200)) is False


# ---------------------------------------------------------------------------
# Fuzzy near-duplicate
# ---------------------------------------------------------------------------


def _near_dup_stream(n_reps: int) -> str:
    # A short period (so several reps fit inside one fuzzy comparison
    # window) that changes by one digit every repeat -- so no period ever
    # matches byte-for-byte three times running (only the fuzzy check can
    # catch this), while still looking like the same near-identical thought
    # each time, e.g. Gemma's documented "Wait, I found it. The 14." loop.
    return "".join(f"Wait, I found it, checking option {i} again now.\n" for i in range(n_reps))


def test_fuzzy_near_duplicate_with_minor_variation_fires() -> None:
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, _near_dup_stream(20)) is True


def test_fuzzy_near_duplicate_does_not_fire_on_a_couple_of_repeats() -> None:
    """Too little text has accumulated yet for even one fuzzy comparison
    window, let alone the required 2-in-a-row streak."""
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, _near_dup_stream(3)) is False


# ---------------------------------------------------------------------------
# False-positive avoidance
# ---------------------------------------------------------------------------


def test_doubled_short_word_amid_healthy_text_never_fires() -> None:
    detector = CyclicThinkingDetector()
    text = (
        "I was very very tired after reviewing this, but the calculation checks out. "
        + _long_healthy_text(300)
    )

    assert _feed_chunks(detector, text) is False


def test_long_healthy_text_never_fires() -> None:
    # A handful of different seeds/vocab orderings, not just one lucky draw.
    for seed in (1234, 7, 99, 4242):
        detector = CyclicThinkingDetector()
        assert _feed_chunks(detector, _long_healthy_text(3000, seed=seed)) is False


def test_numbered_list_with_varied_substance_never_fires() -> None:
    """A templated but substantively-progressing enumeration (each item is
    genuinely different content, only the sentence shape repeats) must not
    be mistaken for a cyclic loop -- this is ordinary, healthy step-by-step
    reasoning, not degeneration."""
    items = [
        "the cache eviction policy might be stale",
        "the retry backoff could be too aggressive under load",
        "the connection pool may be exhausted during bursts",
        "the serializer might choke on a null field",
        "the queue consumer could be double-acking messages",
        "the index rebuild might race with a live write",
        "the config reload may drop an env override",
        "the health check could be hitting the wrong port",
        "the token refresh might expire mid-request",
        "the shard router may misroute during a rebalance",
    ]
    text = "".join(f"{i + 1}. Checking whether {items[i % len(items)]}.\n" for i in range(40))
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, text) is False


def test_minimal_variation_template_never_fires() -> None:
    """The hardest false-positive case: a template with almost nothing
    varying between repeats but the item label itself (still not a real
    loop -- the label keeps changing, so it's making progress, however
    little each step says)."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    text = "".join(f"Checking case {letters[i % 26]}: looks fine so far.\n" for i in range(20))
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, text) is False


def test_empty_fragment_is_a_noop() -> None:
    detector = CyclicThinkingDetector()

    assert detector.feed("") is False


# ---------------------------------------------------------------------------
# The tool-call-argument profile (doc/STUCK_DETECTION.md §2.10)
#
# Every case below is a real false positive the default (thinking-block)
# calibration produces, taken from the session-1788649506 incident: an
# architect writing a perfectly good architecture document had its turn killed
# inside the first ~150 characters of a `create_file` call. Tool-call arguments
# are JSON carrying markdown, code, tables, padding and indentation, where a
# 24-character block repeating three times is *formatting*.
# ---------------------------------------------------------------------------

_PADDED_TABLE_SEPARATOR = "# Doc\n\n| A | B | C |\n| " + " | ".join(["-" * 24] * 3) + " |\n"


def _args(content: str) -> str:
    """Wrap *content* the way a real create_file call's arguments carry it."""
    return json.dumps({"intent": "write it", "path": "proj/specs/x.md", "content": content})


def test_padded_markdown_table_separator_fires_by_default_but_not_for_arguments() -> None:
    """A column-aligned separator row is three identical 24-char dash runs."""
    assert _feed_chunks(CyclicThinkingDetector(), _args(_PADDED_TABLE_SEPARATOR)) is True
    assert (
        _feed_chunks(
            CyclicThinkingDetector.for_tool_call_arguments(), _args(_PADDED_TABLE_SEPARATOR)
        )
        is False
    )


def test_low_entropy_runs_never_fire_for_arguments() -> None:
    """A horizontal rule, or alignment padding, is not a repetition loop --
    at any length, so this must not be merely postponed by the char floor."""
    for filler in ("-", "=", "#", " "):
        long_run = "x" + filler * (_ARGS_MIN_BUFFERED_CHARS * 2) + "y"
        detector = CyclicThinkingDetector.for_tool_call_arguments()
        assert _feed_chunks(detector, _args(long_run)) is False, filler


def test_identical_template_rows_do_not_fire_for_arguments() -> None:
    # Four rows, not the bare _MIN_REPEATS three: the checks only run on
    # fragment boundaries, so at exactly three the one offset where the tail
    # is a whole number of periods can fall between two fragments.
    row = "| Not yet determined | Not yet determined |\n"
    assert _feed_chunks(CyclicThinkingDetector(), _args(row * 4)) is True
    assert _feed_chunks(CyclicThinkingDetector.for_tool_call_arguments(), _args(row * 4)) is False


def test_arguments_profile_still_catches_a_real_loop() -> None:
    """The floors delay the verdict; they must not remove it."""
    looped = "The snake grows one segment every 10 units of food eaten.\n" * 40
    detector = CyclicThinkingDetector.for_tool_call_arguments()

    assert _feed_chunks(detector, _args(looped)) is True


def test_arguments_profile_withholds_its_verdict_below_the_char_floor() -> None:
    """Nothing fires on a sample too small to mean anything, however
    blatantly it repeats."""
    looped = "The same sentence over and over and over again.\n" * 40
    short = looped[: _ARGS_MIN_BUFFERED_CHARS - 100]
    assert _feed_chunks(CyclicThinkingDetector.for_tool_call_arguments(), short) is False
    assert _feed_chunks(CyclicThinkingDetector.for_tool_call_arguments(), looped) is True


def test_thinking_profile_is_unchanged_by_the_arguments_floors() -> None:
    """The default calibration must keep firing as early as it always did --
    a thinking block has a budget to burn, and no formatting to speak of."""
    block = "The reasoning loop keeps repeating here in exactly this way!\n"
    detector = CyclicThinkingDetector()

    assert _feed_chunks(detector, block * _MIN_REPEATS) is True
