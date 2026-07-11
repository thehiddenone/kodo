"""``submit_evaluation`` tool spec — a judge run's terminal verdict call.

The automated validation harness (:mod:`kodo.validator`) scores a finished run
by opening a second kodo session — the *judge* — that reads the generated code
with its tools and rates it. A session turn cannot be grammar-constrained, so
asking the judge to *print* a JSON verdict is unreliable: an agentic turn
interleaves exploration narration and tool-argument fragments, and the score
ends up unparseable. This tool gives the judge a **structured terminal call**
instead: it submits ``score`` (0–100) and a ``report`` as tool input, which the
engine surfaces on the tool-call detail event, so the harness reads the verdict
from the wire — no text parsing. Calling it ends the judge's run (it joins the
same ``stop_requested`` stop mechanism as ``return_result`` / ``escalate_blocker``).

The tool is inert outside a validation judge run: an ordinary agent has no
reason to call it, and doing so would simply end its turn with a recorded
verdict nobody reads. Dispatch lives in :mod:`kodo.tools`.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["SUBMIT_EVALUATION"]


SUBMIT_EVALUATION: ToolSpec = ToolSpec(
    name="submit_evaluation",
    external_name="Submit Evaluation",
    user_description="Submit the validation score and report",
    description=(
        "Submit your final verdict on the run under evaluation and end your review. "
        "Call this exactly once, last, after you have finished reading the generated code. "
        "`score` is an integer from 0 (nothing usable) to 100 (fully meets the request); "
        "`report` is your full written justification. Reporting the verdict through this tool "
        "is required — do not answer in prose."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "score": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall rating of the run, 0–100.",
            },
            "report": {
                "type": "string",
                "description": (
                    "Your full written assessment: what you read, what works, what is "
                    "missing or wrong, and the rubric points that drove the score."
                ),
            },
        },
        "required": ["score", "report"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Acknowledgement that the verdict was recorded.",
            },
            "score": {
                "type": "number",
                "description": "The recorded score, coerced into 0–100.",
            },
            "report": {"type": "string", "description": "The recorded report, echoed back."},
        },
        "required": ["status", "score"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"score": "always", "report": "visible"},
    output_visibility={"status": "always", "score": "always", "report": "visible"},
    when_to_use=(
        "As the final action of a validation judge run, to report the score and report.",
        "Exactly once per run; the run ends immediately after the call.",
    ),
)
