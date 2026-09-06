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
scoped to a single thinking block, never carried across rounds. The same
class is reused for the mid-stream tool-call-argument repetition detector
(doc/STUCK_DETECTION.md §2.10, :mod:`kodo.runtime._engine._watchdog`'s
``_make_tool_call_cyclic_handler``), which simply feeds it
``ToolCallArgDelta.text`` fragments instead of ``ThinkingDelta.text`` ones
-- but *not* unmodified: see :meth:`CyclicThinkingDetector.for_tool_call_arguments`.
The algorithm is not thinking-specific; the calibration above very much is.
Thinking blocks are prose, where a 24-character block repeating three times
running really is a loop. Tool-call arguments are JSON payloads carrying
markdown, source code, tables, padding and indentation, where the very same
pattern is ordinary *formatting* -- a run of 72 identical characters (a
``---`` rule, or 72 spaces of alignment), a column-aligned markdown table
separator, or three identical template rows all satisfy the exact check
inside the first ~150 characters. Firing there killed a real turn that was
writing a perfectly good architecture document (session 1788649506), so the
tool-call-argument profile adds two floors of its own.
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

# The tool-call-argument profile (doc/STUCK_DETECTION.md §2.10) -- see the
# module docstring for why that stream needs floors a thinking block does not.
#
# _ARGS_MIN_DISTINCT_CHARS: how many *distinct* characters the repeated block
# must contain before a repeat counts as content rather than formatting. This
# is what disqualifies the low-entropy cases outright -- a rule of dashes, a
# run of alignment spaces, a padded `| ---- | ---- |` separator row -- while
# leaving any repeat of real prose or code (which clears 8 distinct
# characters within a few words) fully detectable.
#
# _ARGS_MIN_BUFFERED_CHARS: no hit counts at all until this many characters of
# arguments have streamed, so a verdict is never reached on a sample too small
# to mean anything. Deliberately generous: the whole point of the detector is
# to save a turn from a loop that would otherwise run for minutes, and letting
# it write ~1200 characters first costs a couple of seconds. Kept under
# _MAX_NEEDED_CHARS so the floor is always reached before the buffer is ever
# trimmed -- though the counter below is a true running total either way.
_ARGS_MIN_DISTINCT_CHARS = 8
_ARGS_MIN_BUFFERED_CHARS = 1200


class CyclicThinkingDetector:
    """Fed each streamed thinking-delta fragment; flags an in-progress repetition loop.

    One instance per LLM round -- construct fresh, never reused/reset across
    rounds.

    Args:
        min_distinct_chars: A repeat only counts when its period contains at
            least this many distinct characters. ``1`` (the thinking-block
            default) accepts anything, including a run of one character.
        min_buffered_chars: No check may fire until this many characters have
            been fed. ``0`` (the thinking-block default) fires as early as the
            checks themselves allow.
    """

    def __init__(self, *, min_distinct_chars: int = 1, min_buffered_chars: int = 0) -> None:
        self._buf = ""
        self._chars_since_fuzzy_check = 0
        self._fuzzy_streak = 0
        self._min_distinct_chars = min_distinct_chars
        self._min_buffered_chars = min_buffered_chars
        # A true running total, not len(self._buf), which stops growing once
        # the buffer starts being trimmed.
        self._total_chars = 0

    @classmethod
    def for_tool_call_arguments(cls) -> CyclicThinkingDetector:
        """The §2.10 instance: same algorithm, calibrated for JSON tool arguments.

        The single source of truth for that calibration -- ``_turns.py`` builds
        its tool-call-argument detector through here rather than passing the
        constants itself, so the two profiles can never drift apart in the
        caller.
        """
        return cls(
            min_distinct_chars=_ARGS_MIN_DISTINCT_CHARS,
            min_buffered_chars=_ARGS_MIN_BUFFERED_CHARS,
        )

    def feed(self, fragment: str) -> bool:
        """Incorporate one streamed fragment; return True the instant a cycle fires."""
        if not fragment:
            return False
        self._buf += fragment
        self._chars_since_fuzzy_check += len(fragment)
        self._total_chars += len(fragment)

        # Below the evidence floor nothing may fire, but the buffer is still
        # accumulated (and the fuzzy throttle still counts down) so the checks
        # start from a full window the moment the floor is cleared.
        if self._total_chars < self._min_buffered_chars:
            if self._chars_since_fuzzy_check >= _FUZZY_CHECK_INTERVAL_CHARS:
                self._chars_since_fuzzy_check = 0
            self._trim()
            return False

        if self._check_exact_repeat():
            return True

        if self._chars_since_fuzzy_check >= _FUZZY_CHECK_INTERVAL_CHARS:
            self._chars_since_fuzzy_check = 0
            if self._check_fuzzy_repeat():
                return True

        self._trim()
        return False

    def _trim(self) -> None:
        """Settle the retained buffer back to exactly the window both checks need."""
        if len(self._buf) > _TRIM_TRIGGER_CHARS:
            self._buf = self._buf[-_MAX_NEEDED_CHARS:]

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
            if (
                n >= _MIN_REPEATS * p
                and buf[n - _MIN_REPEATS * p : n - p] == buf[n - (_MIN_REPEATS - 1) * p : n]
                and self._carries_content(buf[n - p :])
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
        if not self._carries_content(chunk):
            # Same bar as the exact check: 200 characters of one repeated
            # character are similar to the 200 before them by definition, and
            # that says nothing about whether the model is looping.
            self._fuzzy_streak = 0
            return False
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

    def _carries_content(self, block: str) -> bool:
        """True iff *block* is varied enough for a repeat of it to mean anything.

        Formatting repeats; so does a loop. Distinct-character count is what
        separates them cheaply: a rule, an indentation run or a padded table
        separator is built from one or two characters, whereas any repeat of
        real prose, code or JSON clears the bar within a few words. Always
        true for the thinking-block profile, whose floor is 1.
        """
        if self._min_distinct_chars <= 1:
            return True
        return len(set(block)) >= self._min_distinct_chars
