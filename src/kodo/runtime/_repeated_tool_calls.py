"""Round-boundary detection of a repeated, no-progress tool call
(doc/STUCK_DETECTION.md §2.11).

Every other stuck-agent check in :mod:`kodo.runtime._engine._watchdog` is
anchored on a round that produced *no* tool call (``_DETECTORS`` only ever
runs from ``on_stall``) or on content streaming *within* one round
(:mod:`kodo.runtime._cyclic_thinking`, :mod:`kodo.runtime._think_tag_guard`).
That left one failure mode completely invisible: a model that keeps calling
the *same tool with the same arguments*, getting the *same result* back,
round after round after round. Every such round ends with
``stop_reason="tool_use"``, so nothing flagged it — and worse, the turn loop
counted each lap as evidence of progress and cleared every other detector's
streak.

The traced instance (session ``1788543589``, subsession
``7cc7034e6bf44e1a885614407cfa442a``): the ``requirements_critic`` sub-agent
on a local model issued ``read_file{"path": "kodo-snake/specs/architecture/
system.md"}`` for ~1133 consecutive rounds, each returning the identical
``File not found`` error — 32 minutes and 62.4M cumulative input tokens
before the run was cut short by hand.

The check is deliberately the narrowest one that catches this: a round's
signature covers the tool calls *and* their results, so it fires only when
the round was provably incapable of having taught the model anything. Two
consequences worth keeping in mind when changing it:

- Re-reading a file that an intervening edit changed, or polling a build that
  is still running, produces the same *call* with a different *result* — real
  progress, and correctly never flagged.
- Repeats must be strictly back-to-back. An alternating ``A, B, A, B`` cycle
  is not caught here by design; that shape has never been observed, and the
  window state a looser rule needs is a false-positive surface this does not
  need to take on.

One instance is constructed per ``_run_agent_turn`` call — the state is a
single previous signature plus a counter, so a loop is scoped to one turn and
never leaks across turns.
"""

from __future__ import annotations

import hashlib
import json

__all__ = [
    "_MIN_TOOL_CALL_REPEATS",
    "RepeatedToolCallDetector",
    "call_preview",
    "round_signature",
]

# How many back-to-back identical rounds (same calls, same results) count as a
# loop rather than a retry. Deliberately the same number as
# :data:`kodo.runtime._cyclic_thinking._MIN_REPEATS` -- both answer the same
# question ("how many identical repetitions stop being a coincidence?"), and a
# test pins them together so they cannot drift apart. Two identical calls in a
# row is a plausible retry; three is a loop.
_MIN_TOOL_CALL_REPEATS = 3


def round_signature(calls: list[tuple[str, dict[str, object]]], results: list[object]) -> str:
    """Fingerprint one tool-calling round: every call *and* every result.

    ``json.dumps(..., sort_keys=True)`` makes the fingerprint independent of
    key ordering (a model can emit the same arguments in a different order
    between rounds), and ``default=str`` keeps a stray non-serializable value
    from ever raising inside the turn loop — a fingerprint that degrades to
    ``repr`` is fine; an exception mid-turn is not. The digest, rather than
    the string itself, is what gets retained: signatures include full tool
    results, which can be large, and only equality is ever asked of them.
    """
    payload = json.dumps(
        {"calls": [[name, args] for name, args in calls], "results": results},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


class RepeatedToolCallDetector:
    """Counts how many times a round's signature has repeated back-to-back."""

    def __init__(self) -> None:
        self._last: str | None = None
        self._count = 0

    def feed(self, signature: str) -> int:
        """Record one round; return its consecutive-repeat count.

        Returns ``1`` for a novel round (which is what the caller reads as
        "this round made progress"), ``2`` for the first repeat, and so on —
        the caller compares against its own threshold rather than having one
        baked in here, so the detector stays a pure counter.
        """
        if signature == self._last:
            self._count += 1
        else:
            self._last = signature
            self._count = 1
        return self._count


def call_preview(calls: list[tuple[str, dict[str, object]]]) -> str:
    """Short, human- and LLM-readable rendering of a round's tool calls.

    Goes into the nudge the model reads back and into the user-facing critical
    notice, so it names each tool and shows enough of its arguments to identify
    *which* call is looping, then stops -- a full argument dump would swamp
    both audiences.
    """
    parts = []
    for name, args in calls:
        rendered = json.dumps(args, sort_keys=True, default=str)
        if len(rendered) > 120:
            rendered = rendered[:117] + "..."
        parts.append(f"{name}{rendered}")
    return "; ".join(parts)[:300]
