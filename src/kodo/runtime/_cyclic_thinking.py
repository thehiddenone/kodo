"""Mid-stream detection of cyclic/repetitive thinking blocks (doc/STUCK_DETECTION.md).

Some local models (a documented failure mode, especially under grammar/
structured-output constraints that can block the EOS token) fall into a
repetition loop *inside a single thinking/reasoning block* -- generating the
same few lines verbatim, over and over, until the thinking-token budget is
exhausted. :class:`CyclicThinkingDetector` is fed each streamed
``ThinkingDelta.text`` fragment as it arrives
(:mod:`kodo.runtime._engine._turns`) and flags the moment a cycle is
detected, so the caller can abort the stream early
(:meth:`kodo.llms.LLMPlugin.cancel`) instead of waiting for the model to
exhaust its budget.

Two independent checks against the growing trailing buffer:

1. **Exact block-repeat** (the primary check, and what actually catches "the
   same 3 lines over and over"): fires the instant the buffer's tail consists
   of ``_MIN_REPEATS`` back-to-back identical copies of some block whose
   length lies in ``[_MIN_PERIOD, _MAX_PERIOD]``. Admitting a full range of
   period lengths (rather than a handful of fixed sizes) matters here: real
   repeated content has whatever length its own lines happen to add up to,
   essentially never a "round" number, so a sparse fixed ladder would miss
   almost everything in practice. The candidate lengths are *found*, not
   enumerated (see :meth:`~CyclicThinkingDetector._check_exact_repeat`), so
   the usual non-repeating case costs one C-level substring search rather
   than hundreds of Python-level slice comparisons -- this check runs on
   every single streamed fragment, so its constant factor is the dominant
   cost of the whole class.
2. **Fuzzy near-duplicate** (throttled -- run only every
   ``_FUZZY_CHECK_INTERVAL_CHARS`` new characters, since it's a more
   expensive check): a shingled similarity ratio between the two most recent
   fixed-length chunks, to catch near-repeats with minor variation (e.g. an
   incrementing number) that an exact-match check would miss. A single
   highly-similar chunk pair does not fire on its own -- legitimate
   structured reasoning (a numbered list, "checking case N" for several
   values of N) can coincidentally produce one such pair without ever really
   looping, so this requires ``_MIN_REPEATS - 1`` consecutive high-similarity
   comparisons (a sustained run across multiple throttle intervals) before
   firing, mirroring the exact check's own repeat-count bar.

Only the trailing ``_MAX_NEEDED_CHARS`` characters are ever inspected by
either check, so the retained buffer is trimmed once it grows well past
that -- memory and per-call cost stay bounded no matter how long a single
thinking block runs.

One instance is constructed fresh per LLM round -- a repetition loop is
scoped to a single thinking block, never carried across rounds.
"""

from __future__ import annotations

import difflib

__all__ = ["CyclicThinkingDetector"]

# Candidate period lengths (characters) tried on every check. Floored well
# above a single word so ordinary short repetition (a doubled filler word,
# "very very", or even the same single word appearing three times in a row
# by innocent coincidence) is never even considered -- the target failure
# mode ("the same 3 lines over and over") is line-scale, not word-scale, and
# a higher floor also meaningfully cuts the odds of a short-period exact
# match ever occurring by pure chance in ordinary prose. It doubles as the
# probe length in ``_check_exact_repeat`` (a probe must fit inside a single
# period), which is the other reason not to drop it near word scale: a short
# probe recurs by chance and costs extra searches.
_MIN_PERIOD = 24
_MAX_PERIOD = 600

# How many consecutive repeats of a period are required before firing.
_MIN_REPEATS = 3

# Fuzzy near-duplicate check: chunk length compared, how many new characters
# accumulate between re-evaluations, and the similarity ratio required to
# fire. Calibrated empirically (not just picked): 0.90 with a 2-in-a-row
# streak cleanly separates a genuine near-duplicate loop (Gemma's documented
# "Wait, I found it. The 14." style repeats, where each occurrence differs
# only by a token or two) from legitimate structured-but-progressing
# reasoning -- including deliberately adversarial cases like a numbered list
# whose items share a template but differ substantively, and even a
# minimal-variation template ("Checking case A/B/C: looks fine so far.")
# that a looser threshold or interval let through as a false positive.
_FUZZY_CHUNK_LEN = 200
_FUZZY_CHECK_INTERVAL_CHARS = 200
_FUZZY_RATIO_THRESHOLD = 0.90

# Neither check ever needs to look further back than this; trim the retained
# buffer once it grows past double this, so it settles back down to exactly
# this many characters rather than being re-sliced on every single call.
_MAX_NEEDED_CHARS = max(_MIN_REPEATS * _MAX_PERIOD, 2 * _FUZZY_CHUNK_LEN)
_TRIM_TRIGGER_CHARS = _MAX_NEEDED_CHARS * 2


class CyclicThinkingDetector:
    """Fed each streamed thinking-delta fragment; flags an in-progress repetition loop.

    One instance per LLM round -- construct fresh, never reused/reset across
    rounds.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._chars_since_fuzzy_check = 0
        self._fuzzy_streak = 0

    def feed(self, fragment: str) -> bool:
        """Incorporate one streamed fragment; return True the instant a cycle fires."""
        if not fragment:
            return False
        self._buf += fragment
        self._chars_since_fuzzy_check += len(fragment)

        if self._check_exact_repeat():
            return True

        if self._chars_since_fuzzy_check >= _FUZZY_CHECK_INTERVAL_CHARS:
            self._chars_since_fuzzy_check = 0
            if self._check_fuzzy_repeat():
                return True

        if len(self._buf) > _TRIM_TRIGGER_CHARS:
            self._buf = self._buf[-_MAX_NEEDED_CHARS:]

        return False

    def _check_exact_repeat(self) -> bool:
        """True iff the buffer's tail is ``_MIN_REPEATS`` copies of one block.

        Candidate period lengths are located instead of enumerated. Any
        qualifying period ``p`` makes the buffer's last ``_MIN_PERIOD``
        characters (the *probe*) reappear verbatim exactly ``p`` characters
        earlier -- the probe is no longer than one period, and stepping one
        period back stays inside the repeated region -- so the candidate
        periods are precisely the probe's earlier occurrences, which
        ``str.rfind`` locates at C speed. In ordinary non-repeating prose the
        probe never recurs, so the whole check is one failed search. Walking
        every length in ``[_MIN_PERIOD, _MAX_PERIOD]`` instead would copy
        hundreds of kilobytes per streamed fragment (~170us a call, enough to
        stall the streaming event loop for most of a second on a long
        thinking block); this costs under a microsecond.
        """
        buf = self._buf
        n = len(buf)
        if n < _MIN_REPEATS * _MIN_PERIOD:
            return False

        probe = buf[n - _MIN_PERIOD :]
        probe_start = n - _MIN_PERIOD
        # An occurrence starting at `idx` means period == probe_start - idx,
        # so these two bounds are exactly the `p <= _MAX_PERIOD` and
        # `p >= _MIN_PERIOD` constraints.
        earliest_start = max(0, probe_start - _MAX_PERIOD)
        search_end = probe_start  # exclusive end of the region rfind may match in
        while search_end - earliest_start >= _MIN_PERIOD:
            idx = buf.rfind(probe, earliest_start, search_end)
            if idx < 0:
                return False
            p = probe_start - idx
            # A run of `_MIN_REPEATS` identical period-p blocks is exactly a
            # tail of `_MIN_REPEATS * p` characters that equals itself shifted
            # by one period -- one comparison instead of `_MIN_REPEATS - 1`.
            if n >= _MIN_REPEATS * p and (
                buf[n - _MIN_REPEATS * p : n - p] == buf[n - (_MIN_REPEATS - 1) * p : n]
            ):
                return True
            search_end = idx + _MIN_PERIOD - 1  # keep looking strictly further back
        return False

    def _check_fuzzy_repeat(self) -> bool:
        buf = self._buf
        if len(buf) < 2 * _FUZZY_CHUNK_LEN:
            return False
        chunk = buf[-_FUZZY_CHUNK_LEN:]
        prev_chunk = buf[-2 * _FUZZY_CHUNK_LEN : -_FUZZY_CHUNK_LEN]
        # autojunk=False is required, not optional: the default (True) marks
        # any character occurring in >1% of a sequence longer than 200 chars
        # as "popular"/junk and excludes it from matching, which would
        # understate similarity on exactly the repetitive text this exists
        # to catch.
        ratio = difflib.SequenceMatcher(None, prev_chunk, chunk, autojunk=False).ratio()
        if ratio < _FUZZY_RATIO_THRESHOLD:
            self._fuzzy_streak = 0
            return False

        # A single highly-similar chunk pair is not enough to fire on its
        # own -- structured-but-legitimate reasoning (a numbered list, a
        # sequence of "checking case N" steps) can easily produce one
        # coincidentally-similar pair without ever actually looping. Require
        # _MIN_REPEATS - 1 consecutive high-similarity comparisons (i.e. a
        # sustained run across multiple throttle intervals), mirroring the
        # exact check's own >= _MIN_REPEATS requirement.
        self._fuzzy_streak += 1
        return self._fuzzy_streak >= _MIN_REPEATS - 1
