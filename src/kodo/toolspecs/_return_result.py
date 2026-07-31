"""``return_result`` tool spec — every sub-agent's terminal "return" call.

A sub-agent is "a tool with agentic behavior": where a plain tool returns a value
that the engine validates against its ``output_schema``, a sub-agent does the same
through this terminal tool. Calling it ends the sub-agent's run (it joins the same
``stop_requested`` mechanism as ``submit_evaluation``). It is the *only* way out:
an author that is blocked returns an escalation through this same call rather
than a separate tool (see :mod:`kodo.subagents.specs._shapes`).

:data:`RETURN_RESULT` is the **canonical** spec — the catalog entry the registry
validates a ``tools:`` reference against and the engine normalizes results with.
What a sub-agent actually sees is :func:`build_return_result_spec`'s output: the
same tool with ``result`` bound to *that* agent's declared ``output_schema``, so
the shape it must produce reaches the model as a real JSON Schema in the tool
definition rather than as prose it has to remember from its prompt.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._compliance import augment_output_schema
from ._spec import SecurityImpact, ToolSpec

__all__ = ["RETURN_RESULT", "build_return_result_spec"]


_DESCRIPTION = (
    "Return your final result to the agent that delegated this task, and end your run. "
    "Call this exactly once, last. The engine validates `result` against your declared "
    "output schema and reports `schema_compliance: false` if it had to repair the "
    "payload (missing fields backfilled with empty strings, undeclared fields dropped)."
)


RETURN_RESULT: ToolSpec = ToolSpec(
    name="return_result",
    external_name="Return Result",
    user_description="Return the sub-agent's result",
    description=(
        f"{_DESCRIPTION} This is the canonical spec; each sub-agent is offered a copy "
        "with `result` bound to its own output schema."
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
)


def build_return_result_spec(output_schema: dict[str, object]) -> ToolSpec:
    """Return ``return_result`` with ``result`` bound to *output_schema*.

    The schema is passed through :func:`~kodo.toolspecs.augment_output_schema`
    first, so the engine-owned ``schema_compliance`` field the agent is expected
    to echo back is declared where the agent will actually read it.

    Args:
        output_schema: The running sub-agent's declared ``output_schema`` (from
            its :class:`~kodo.subagents.SubAgentSpec`), *without*
            ``schema_compliance``.

    Returns:
        ToolSpec: The specialized spec — same name, dispatch, and output shape as
        :data:`RETURN_RESULT`; only ``result``'s schema differs.
    """
    result_schema = augment_output_schema(output_schema)
    result_schema["description"] = "Your result object. Every field below is described inline."
    return ToolSpec(
        name=RETURN_RESULT.name,
        external_name=RETURN_RESULT.external_name,
        user_description=RETURN_RESULT.user_description,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {"result": result_schema},
            "required": ["result"],
        },
        output_schema=RETURN_RESULT.output_schema,
        security_impact=RETURN_RESULT.security_impact,
        input_visibility=dict(RETURN_RESULT.input_visibility),
        output_visibility=dict(RETURN_RESULT.output_visibility),
    )
