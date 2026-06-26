"""``return_result`` tool spec — every sub-agent's terminal "return" call.

A sub-agent is "a tool with agentic behavior": where a plain tool returns a value
that the engine validates against its ``output_schema``, a sub-agent does the same
through this terminal tool. The ``result`` payload is validated/normalized against
the **active sub-agent's** ``output_schema`` (see
:mod:`kodo.subagents.specs`) — which the engine injects per run — not against a
fixed schema here, so this spec's own ``input_schema`` describes only the wrapper.
Calling it ends the sub-agent's run (it joins the same stop mechanism as
``escalate_blocker``).

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["RETURN_RESULT"]


RETURN_RESULT: ToolSpec = ToolSpec(
    name="return_result",
    external_name="Return Result",
    user_description="Return the sub-agent's result",
    description=(
        "Return your final result to the agent that delegated this task, and end your run. "
        "Call this exactly once, last. The `result` object MUST conform to the output schema in "
        "the `## Your Task Contract` section of your prompt; the engine validates it and reports "
        "`schema_compliance: false` if it had to repair the payload."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "description": "Your result object, matching your declared output schema.",
            },
        },
        "required": ["result"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Acknowledgement that the result was received.",
            },
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"result": "visible"},
    output_visibility={"status": "always"},
    when_to_use=(
        "As the final action of a sub-agent run, to return the result the caller expects.",
        "Exactly once per run; the run ends immediately after the call.",
    ),
)
