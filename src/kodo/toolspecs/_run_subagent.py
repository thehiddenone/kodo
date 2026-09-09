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
    "ENGINE_OWNED_TASK_FIELDS",
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

# Optional caller-supplied cap on review rounds, and the engine's default when
# the caller omits it. Only meaningful for a sub-agent whose call is a loop --
# one that declares a ``critic:``, a ``user_review: true``, or both; the engine
# drives the loop, the caller only sizes its budget.
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
                    "Cap on review rounds when the target sub-agent runs a review "
                    "loop (it declares a critic, a user review gate, or both); "
                    f"defaults to {MAX_ROUNDS_DEFAULT}."
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


#: Task fields the **engine** fills in, which therefore never appear on the
#: ``run_subagent_<name>`` tool of an agent whose inputs the engine resolves.
#: They stay declared on the sub-agent's own ``input_schema`` — that is what
#: gives them a description in the rendered task brief — but such a caller can
#: neither set nor be required to supply them.
#:
#: ``input_paths`` is resolved from the sub-agent's declared artifact roles
#: against the work-product ledger, and ``for_revision_paths`` from the prior
#: round's membership (doc/FINDINGS.md). Leaving either caller-writable was how
#: a model came to be the source of a file path at all: a critic promised the
#: architecture and handed a hardcoded single path invented the rest and read a
#: nonexistent file ~1133 times. Mirrors ``schema_compliance`` on the output
#: side — engine-owned, and specs must not let a model write it.
#:
#: The stripping is conditional for one honest reason: it is only right where
#: something else fills the gap. An agent that declares no artifact roles has
#: no resolution behind it, so removing the field would leave nobody able to
#: name a file — see *engine_resolves_inputs* in
#: :func:`build_run_subagent_spec`.
ENGINE_OWNED_TASK_FIELDS: frozenset[str] = frozenset({"input_paths", "for_revision_paths"})


def build_run_subagent_spec(
    *,
    subagent_name: str,
    display_name: str,
    description: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    critic_name: str = "",
    user_review: bool = False,
    standalone: bool = False,
    engine_resolves_inputs: bool = True,
) -> ToolSpec:
    """Build the ``run_subagent_<name>`` tool one caller sees for one sub-agent.

    The sub-agent's ``input_schema`` becomes the tool's input schema, minus the
    :data:`ENGINE_OWNED_TASK_FIELDS` when the engine resolves this agent's
    inputs (plus an optional ``max_rounds`` when the call runs a review loop —
    i.e. when *critic_name* or *user_review* is set). For a
    pipeline agent a caller says *what* to do and the engine works out which
    files that means; for an agent with no artifact roles behind it, naming the
    files is still the caller's job (*engine_resolves_inputs*). Its
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
        user_review: ``True`` when this sub-agent's work product needs the
            **user's** sign-off before it is accepted (frontmatter
            ``user_review: true``). Orthogonal to *critic_name*: either one on
            its own makes the call a bounded loop rather than a single pass —
            which is why ``max_rounds`` is offered for both — and together they
            mean critic rounds followed by the gate.
        standalone: ``True`` for an on-demand specialist that depends on no
            other agent's output; ``False`` for a workflow stage that consumes
            the artifacts of the stage before it. Stated in the description
            because it is what tells a caller whether ordering matters.
        engine_resolves_inputs: ``True`` when this sub-agent declares artifact
            roles the engine resolves for it, which is what makes hiding
            :data:`ENGINE_OWNED_TASK_FIELDS` safe — something else fills them
            in. ``False`` for an agent with no ``consumes`` (``developer``,
            driven by the Problem Solver, which has already read the tree and
            knows which files it means): nothing would resolve its
            ``input_paths``, so stripping the field would silently take away
            the only way to point it at a file.

    Returns:
        ToolSpec: The variant spec, ready to hand to the LLM.
    """
    hidden = ENGINE_OWNED_TASK_FIELDS if engine_resolves_inputs else frozenset()
    props_raw = input_schema.get("properties")
    properties: dict[str, object] = {
        k: v
        for k, v in (dict(props_raw) if isinstance(props_raw, dict) else {}).items()
        if k not in hidden
    }
    required_raw = input_schema.get("required")
    required = [
        str(r)
        for r in (required_raw if isinstance(required_raw, list) else [])
        if str(r) not in hidden
    ]

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
            f"`{subagent_name}`, hands every file it wrote to `{critic_name}` as one "
            f"work product, and — while findings are outstanding — re-runs "
            f"`{subagent_name}` on the same brief, until the backlog is clear or the "
            f"round budget runs out. The two exchange findings directly through their "
            f"own tool; nothing about them passes through you. One call is the whole "
            f"loop; do not call it again to 'iterate'. Call it again only to start a "
            f"*new* piece of work, or to resume one the `review` block reports as "
            f"unfinished."
        )
    if user_review and critic_name:
        prose.append(
            "Once nothing is outstanding, the work product goes to the **user** for "
            "sign-off before it is accepted. A rejection is added to the same backlog "
            "as a finding, so the loop simply continues; you are not asked anything."
        )
    elif user_review:
        prose.append(
            f"This runs a review loop, not a single pass — but the reviewer is the "
            f"**user**, not a critic. The engine spawns `{subagent_name}`, records "
            f"every file it wrote as one work product, and puts that to the user for "
            f"sign-off. A rejection is minted as a finding for `{subagent_name}` to "
            f"resolve, and the loop re-runs it until the user accepts or the round "
            f"budget runs out. One call is the whole loop; do not call it again to "
            f"'iterate'. Call it again only to start a *new* piece of work, or to "
            f"resume one the `review` block reports as unfinished."
        )
    if critic_name or user_review:
        properties[MAX_ROUNDS_KEY] = {
            "type": "integer",
            "description": (
                f"Optional cap on review rounds (default {MAX_ROUNDS_DEFAULT}). "
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
