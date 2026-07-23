"""Unit tests for kodo.runtime._cyclic_thinking.CyclicThinkingDetector.

Pure algorithm tests, no engine involved -- see test_engine_watchdog.py for
how the detector is wired into the real streaming loop.
"""

from __future__ import annotations

import random

from kodo.runtime._cyclic_thinking import CyclicThinkingDetector

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
