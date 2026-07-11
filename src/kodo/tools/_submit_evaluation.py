"""``submit_evaluation`` tool — a validation judge's terminal verdict call.

Coerces the submitted ``score`` into 0–100, echoes it and the ``report`` back
(so both ride the tool-call detail event the harness reads), and ends the run
by setting ``stop_requested`` — the same stop mechanism ``return_result`` and
``escalate_blocker`` use. See :mod:`kodo.toolspecs._submit_evaluation` for why
the judge submits its verdict through a tool instead of printing JSON.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["SubmitEvaluationTool"]

_log = logging.getLogger(__name__)


def _coerce_score(value: object) -> float:
    """Coerce a submitted score into a 0–100 float, defaulting to 0.0.

    The LLM may pass the score as a number or a numeric string; anything that
    is not a finite number becomes 0.0, and the result is clamped to 0–100.

    Args:
        value (object): The raw ``score`` input.

    Returns:
        float: The coerced, clamped score.
    """
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(100.0, score))


class SubmitEvaluationTool(Tool):
    """Record a judge's ``{score, report}`` verdict and end the run."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        score = _coerce_score(tool_input.get("score"))
        report = str(tool_input.get("report") or "")
        self.context.stop_requested = True
        _log.info(
            "submit_evaluation from %s: score=%.3g report_chars=%d",
            self.context.agent_name,
            score,
            len(report),
        )
        return json.dumps({"status": "recorded", "score": score, "report": report})
