"""Mid-stream detection of a stray ``<think>`` tag inside tool-call arguments
(doc/STUCK_DETECTION.md §2.9).

A documented local-model failure mode distinct from
:mod:`kodo.runtime._cyclic_thinking`'s repetition loop: instead of (or in
addition to) using its own thinking channel, a model sometimes emits a
literal ``<think>...</think>`` block *inside the JSON arguments of a tool
call* -- e.g. narrating its reasoning inside a ``run_subagent`` call's task
string instead of putting it in a real thinking block first. This is never
valid output (the arguments are meant to be consumed as structured data, not
prose), and it is exactly the shape of the incident that motivated this
guard: a model embedded a thinking block inside a sub-agent task description
and then, still inside that same tool-call argument text, degenerated into
repeating the same sentence forever.

:class:`ThinkTagDetector` is fed each streamed ``ToolCallArgDelta.text``
fragment as it arrives (:mod:`kodo.runtime._engine._turns`) and flags the
instant a ``<think>`` open tag appears anywhere in the accumulated argument
text, so the caller can abort the stream immediately
(:meth:`kodo.llms.LLMPlugin.cancel`) -- unlike
:class:`~kodo.runtime._cyclic_thinking.CyclicThinkingDetector`, there is
nothing to wait for or calibrate here: a single occurrence is already a
protocol violation, not a heuristic judgment call.
"""

from __future__ import annotations

__all__ = ["ThinkTagDetector"]

# Only the open tag matters -- the moment this appears, the arguments are
# already malformed; there is no need to wait for a matching close tag.
_THINK_OPEN = "<think>"


class ThinkTagDetector:
    """Fed each streamed tool-call-argument fragment; flags a literal ``<think>`` tag.

    One instance per LLM round -- construct fresh, never reused/reset across
    rounds. Boundary-safe: a tag split across two fragments (e.g. ``"<thi"``
    then ``"nk>"``) is still caught, since a short tail of each fragment is
    retained and prefixed onto the next one before searching.
    """

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, fragment: str) -> bool:
        """Incorporate one streamed fragment; return True the instant the tag is seen."""
        if not fragment:
            return False
        combined = self._tail + fragment
        hit = _THINK_OPEN in combined
        self._tail = combined[-(len(_THINK_OPEN) - 1) :]
        return hit
