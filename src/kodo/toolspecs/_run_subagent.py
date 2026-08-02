"""``run_subagent`` tool spec — guide tool (FR-ORCH-03).

Two things live here:

- :data:`RUN_SUBAGENT`, the **canonical** spec. It is never offered to a model
  as-is; it is what every ``run_subagent_<name>`` call is normalized back to
  (see :func:`kodo.tools.canonical_tool_call`), so the engine has one stable
  name/visibility map to gate, log, checkpoint, and render a tool-call card
  from regardless of which sub-agent was targeted.
- :func:`build_run_subagent_spec`, which mints the **per-sub-agent variant**
  an agent actually sees. One tool per invocable sub-agent, each declaring
  *that* sub-agent's own ``input_schema`` inline, so a caller with several
  sub-agents no longer has to guess an opaque ``task_input`` shape from prose.

The variant flattens the sub-agent's input schema to the tool's top level
(``run_subagent_coder(instructions=..., input_paths=...)``) rather than nesting
it under ``task_input``; :func:`kodo.tools.canonical_tool_call` re-wraps it on
the way in.

Dispatch lives in :mod:`kodo.tools` (one handler module per tool).
"""

from __future__ import annotations

from ._spec import VISIBILITY_ALWAYS, VISIBILITY_VISIBLE, SecurityImpact, ToolSpec

__all__ = [
    "MAX_ROUNDS_DEFAULT",
    "MAX_ROUNDS_KEY",
    "RUN_SUBAGENT",
    "RUN_SUBAGENT_PREFIX",
    "build_run_subagent_spec",
    "run_subagent_tool_name",
    "subagent_from_tool_name",
]

# Every per-sub-agent variant is named ``run_subagent_<subagent name>``.
RUN_SUBAGENT_PREFIX = "run_subagent_"

# Optional caller-supplied cap on author/critic rounds, and the engine's default
# when the caller omits it. Only meaningful for a sub-agent that declares a
# critic; the engine drives the loop, the caller only sizes its budget.
MAX_ROUNDS_KEY = "max_rounds"
MAX_ROUNDS_DEFAULT = 5


def run_subagent_tool_name(subagent_name: str) -> str:
    """Return the variant tool name that targets *subagent_name*."""
    return f"{RUN_SUBAGENT_PREFIX}{subagent_name}"


def subagent_from_tool_name(tool_name: str) -> str:
    """Return the sub-agent a ``run_subagent_<name>`` tool targets, else ``""``.

    The inverse of :func:`run_subagent_tool_name`. Returns ``""`` for the
    canonical ``run_subagent`` and for any tool that is not a variant, so
    callers can use it as a cheap "is this a variant?" test.
    """
    if not tool_name.startswith(RUN_SUBAGENT_PREFIX):
        return ""
    return tool_name[len(RUN_SUBAGENT_PREFIX) :]


RUN_SUBAGENT: ToolSpec = ToolSpec(
    name="run_subagent",
    external_name="Run Sub-Agent",
    user_description="Run a sub-agent",
    description=(
        "Canonical form of a sub-agent invocation. Agents are never offered this "
        "spec directly — they get one `run_subagent_<name>` tool per sub-agent "
        "they may invoke, each declaring that sub-agent's own input schema; the "
        "engine normalizes such a call back to this shape before dispatch."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Sub-agent name from the registry (e.g. 'narrative_author').",
            },
            "task_input": {
                "type": "object",
                "description": (
                    "Structured task for the sub-agent, conforming to that agent's input schema."
                ),
            },
            MAX_ROUNDS_KEY: {
                "type": "integer",
                "description": (
                    "Cap on author/critic rounds when the target sub-agent declares a "
                    f"critic; defaults to {MAX_ROUNDS_DEFAULT}."
                ),
            },
        },
        "required": ["name", "task_input"],
    },
    output_schema={
        "type": "object",
        "description": "The sub-agent's structured result (its declared output schema).",
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={
        "name": VISIBILITY_ALWAYS,
        "task_input": VISIBILITY_VISIBLE,
        MAX_ROUNDS_KEY: VISIBILITY_VISIBLE,
    },
    # The result is the sub-agent's own dynamic output schema, so there are no
    # fixed output properties to assign per-key visibility to.
    output_visibility={},
    requires_project=True,
)


def build_run_subagent_spec(
    *,
    subagent_name: str,
    display_name: str,
    description: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    critic_name: str = "",
    standalone: bool = False,
) -> ToolSpec:
    """Build the ``run_subagent_<name>`` tool one caller sees for one sub-agent.

    The sub-agent's ``input_schema`` becomes the tool's input schema verbatim
    (plus an optional ``max_rounds`` when *critic_name* is set). Its
    ``output_schema`` — already merged with the review block by the caller when
    a critic is involved — is carried on the spec and reaches the model through
    :func:`~kodo.toolspecs.tool_description`, exactly like every other tool's;
    this builder must not pre-render it, or the description would carry two
    ``Returns:`` blocks.

    This spec is the **only** place a caller learns what a sub-agent is for.
    The prompt-side roster that used to restate it is gone (doc/TOOLS.md §7:
    tools are described through the ``tools`` argument, never in a prompt), so
    the *kind* and *review* facts its table columns carried are appended here
    as prose.

    Args:
        subagent_name: Registry name of the sub-agent this tool invokes.
        display_name: The sub-agent's user-facing name, used for
            ``external_name`` (which never reaches the model).
        description: The sub-agent's caller-facing summary — what it does and
            when to delegate to it. This is its ``## Purpose`` body.
        input_schema: The sub-agent's declared ``input_schema``.
        output_schema: What this tool returns to the caller: the sub-agent's own
            output schema, plus the ``review`` block when *critic_name* is set.
        critic_name: The critic paired with this sub-agent, or ``""`` when it
            has none. A non-empty value means the engine runs the whole
            author→critic loop inside one call, so the description says so and
            ``max_rounds`` is offered.
        standalone: ``True`` for an on-demand specialist that depends on no
            other agent's output; ``False`` for a workflow stage that consumes
            the artifacts of the stage before it. Stated in the description
            because it is what tells a caller whether ordering matters.

    Returns:
        ToolSpec: The variant spec, ready to hand to the LLM.
    """
    props_raw = input_schema.get("properties")
    properties: dict[str, object] = dict(props_raw) if isinstance(props_raw, dict) else {}
    required_raw = input_schema.get("required")
    required = [str(r) for r in required_raw] if isinstance(required_raw, list) else []

    prose = [description.strip()]
    prose.append(
        "A **standalone specialist**: invoke it whenever the need arises. It "
        "sits outside the pipeline and depends on no other agent's output."
        if standalone
        else "A **workflow stage**: it consumes the artifacts produced by the "
        "stage before it, so it runs in a fixed order and depends on upstream "
        "output being in place."
    )
    if critic_name:
        prose.append(
            f"This runs the full review loop, not a single pass: the engine spawns "
            f"`{subagent_name}`, hands its primary file to `{critic_name}`, and — while "
            f"the critic rejects — re-runs `{subagent_name}` with the concerns folded "
            f"into its instructions, until the critic accepts or the round budget runs "
            f"out. One call is the whole loop; do not call it again to 'iterate'. Call "
            f"it again only to start a *new* piece of work, or to resume one the "
            f"`review` block reports as unfinished."
        )
        properties[MAX_ROUNDS_KEY] = {
            "type": "integer",
            "description": (
                f"Optional cap on author/critic rounds (default {MAX_ROUNDS_DEFAULT}). "
                "Size it to the work: fewer for a simple file, more only when rounds "
                "are still making real progress."
            ),
        }
    return ToolSpec(
        name=run_subagent_tool_name(subagent_name),
        external_name=f"Run {display_name}" if display_name else "Run Sub-Agent",
        user_description=f"Run the {display_name or subagent_name} sub-agent",
        description="\n\n".join(prose),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
        },
        output_schema=output_schema,
        security_impact=RUN_SUBAGENT.security_impact,
        # Every field of a delegated task is customer-visible; none of it is a
        # secret, and a permission prompt for a spawn should show the whole brief.
        input_visibility=dict.fromkeys(properties, VISIBILITY_VISIBLE),
        output_visibility={},
        requires_project=RUN_SUBAGENT.requires_project,
    )
