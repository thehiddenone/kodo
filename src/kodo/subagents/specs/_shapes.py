"""Declarative schema builders shared by the sub-agent specs.

These are pure schema *constructors* (no dispatch or runtime logic) used by the
one-spec-per-file modules in this package to assemble their ``input_schema`` /
``output_schema`` without copy-pasting the common envelopes. Each builder returns
a fresh dict so callers never share mutable schema state.

The shapes mirror the contracts the agent prompts already describe:

- **Pipeline input** — the structured task a file-backed sub-agent receives when
  delegated to: free-form ``instructions`` from its caller, plus the real file
  paths the **engine** resolved for it (``input_paths``, and for a revision
  round ``for_revision_paths``). Those two are engine-owned
  (:data:`~kodo.toolspecs.ENGINE_OWNED_TASK_FIELDS`): they are declared here so
  the rendered task brief can describe them, and stripped from the
  ``run_subagent_<name>`` tool so no caller can write them. Every path is
  folder-prefixed with its owning project's name (a ``get_root_paths`` entry —
  the same logical-path convention ``LogicalPathResolver`` uses everywhere
  else), since a Guided session may have more than one bound project.
  ``responsibility_code`` is the third field the engine — not the caller —
  has the last word on: it is declared **only** by the per-component stages
  (``require_responsibility=True``), and a task carrying one for any other
  agent has it dropped before anything reads it
  (:attr:`~kodo.subagents.SubAgentSpec.takes_responsibility_code`).
- **Author/solo output** — the path(s) a producing sub-agent wrote, plus which
  one is primary (what a critic reviews / what the author-critic loop tracks) —
  *or*, when the author is blocked, the escalation described next.
- **Escalation** — a blocked author reports through the same ``return_result``
  it uses for a normal result: a non-empty ``reason``, the blocking ``summary``,
  and any discrete ``options``. This is the shape the retired
  ``escalate_blocker`` tool declared, minus its ``blocking_artifact_ids``,
  promoted onto the author's own output for the same reason the critic's verdict
  was (see below): one terminal call, and a structured result the *caller*
  actually receives. The old tool stopped the run without ever setting a result,
  so a blocked sub-agent handed its delegator nothing but a compliance failure.
  ``_run_review_loop`` stops the moment ``reason`` comes back non-empty
  (``review.outcome: "escalated"``) rather than sending a blocked author to its
  critic.
- **Critic output** — the reviewed ``path`` and a list of ``findings``: new ones
  (no ``id``) and updates to existing ones (``id`` plus only the fields that
  changed). Identical for every critic (it takes no arguments). There is **no
  ``accept`` field**: a verdict is *derived* — the document is accepted when the
  round leaves nothing outstanding — so a critic returns evidence and the engine
  draws the conclusion, which removes the whole class of "accepted with concerns
  attached" results. The engine applies the list to the document's session-scoped
  findings backlog (:mod:`kodo.findings`, doc/FINDINGS.md).

Inline agents (``compactor``, ``toolchain_builder``) read and write files
directly with no structured pipeline contract; they declare their inline/path
shapes directly in their own modules rather than through these builders.
(Session titling used to be a third inline agent here; it is now
:mod:`kodo.titling`, a local summarization model with no sub-agent spec at
all.)
"""

from __future__ import annotations

from .._subagentspec import RESPONSIBILITY_CODE_KEY

__all__ = [
    "author_output",
    "critic_output",
    "finding_item",
    "pipeline_input",
]

_INSTRUCTIONS = {
    "type": "string",
    "description": (
        "What to do this round: produce a fresh document, or revise the prior "
        "one. The same instructions are re-sent every round — outstanding "
        "findings are never written in here; call `get_findings` for those."
    ),
}
_PROJECT_CODE = {
    "type": "string",
    "description": "Inherited PROJECTCODE; never invented.",
}
_RESPONSIBILITY_CODE = {
    "type": "string",
    "description": "Component codename this run is scoped to.",
}
_FOR_REVISION_PATHS = {
    "type": ["array", "null"],
    "items": {"type": "string"},
    "description": (
        "The files your previous round produced, to revise this round (authors "
        "only; omitted/null when there is no prior round). This is your whole "
        "prior work product, not just its entry point — revise it as one "
        "change. Supplied by the engine from what you last wrote, like "
        "input_paths; each path is folder-prefixed with its project's name."
    ),
}


def pipeline_input(
    *,
    input_paths: str,
    require_input_paths: bool = True,
    require_responsibility: bool = False,
    extra_properties: dict[str, object] | None = None,
    extra_required: list[str] | None = None,
) -> dict[str, object]:
    """Build the structured task a file-backed sub-agent receives.

    Args:
        input_paths: Human description of which real files this agent must
            read (rendered as the ``input_paths`` field description). Prose
            only — what is actually delivered comes from the spec's declared
            ``consumes`` roles, and the two should say the same thing.
        require_input_paths: Whether ``input_paths`` is listed in ``required``.
            Since 2026-09-05 this is documentation rather than a caller
            obligation: ``input_paths`` is engine-owned
            (:data:`~kodo.toolspecs.ENGINE_OWNED_TASK_FIELDS`) and is stripped
            from the ``run_subagent_<name>`` tool along with its ``required``
            entry, so no caller is ever asked for it.
        require_responsibility: Whether this agent runs **per component**.
            ``True`` declares ``responsibility_code`` and requires it; ``False``
            (every product-level stage and every critic) leaves the property out
            of the schema entirely, so it never appears on the
            ``run_subagent_<name>`` tool and the caller is not invited to send
            one. The engine drops a stray one anyway
            (:attr:`~kodo.subagents.SubAgentSpec.takes_responsibility_code`) —
            omitting it here is what keeps the tool honest, not what enforces
            it.
        extra_properties: Agent-specific extra input properties to merge in.
        extra_required: Agent-specific extra required field names.
    """
    properties: dict[str, object] = {
        "instructions": dict(_INSTRUCTIONS),
        "project_code": dict(_PROJECT_CODE),
        "input_paths": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                f"{input_paths} Supplied by the engine, not by your caller: it "
                "resolves the artifact roles you declared against what this "
                "project has actually produced. Each label names what the file "
                "*is* (`architecture`, `requirements`, …; `<role>_<filename>` "
                "when a role is filled by several), and each path is "
                "folder-prefixed with its project's name. These are the inputs "
                "you are guaranteed — read them. If you need more, find it with "
                "your own tools; never guess at a path."
            ),
        },
        "for_revision_paths": dict(_FOR_REVISION_PATHS),
    }
    if require_responsibility:
        properties[RESPONSIBILITY_CODE_KEY] = dict(_RESPONSIBILITY_CODE)
    if extra_properties:
        properties.update(extra_properties)
    required = ["instructions"]
    if require_input_paths:
        required.append("input_paths")
    if require_responsibility:
        required.append(RESPONSIBILITY_CODE_KEY)
    if extra_required:
        required.extend(extra_required)
    return {"type": "object", "properties": properties, "required": required}


def author_output(
    *,
    extra_properties: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the output shape for a file-writing author/solo sub-agent.

    Carries **both** terminal outcomes an author can reach, because
    ``return_result`` is its only way out (see the module docstring's
    "Escalation" note):

    - the normal one — ``paths`` / ``summary`` plus whatever the spec adds
      through ``extra_properties``;
    - the blocked one — a non-empty ``reason`` (plus the blocker's ``summary``
      and any ``options``), which the engine reads as an escalation.

    Only ``summary`` is *schema*-required: an author blocked before it wrote
    anything has no ``paths`` to report, and forcing them would make every
    escalation non-compliant (:func:`~kodo.toolspecs.normalize_output` backfills
    a missing required field with ``""`` and flags the whole result), which is
    exactly the "sub-agent failed" signal an escalation is not. The obligation
    is therefore stated per field in prose — "required unless you are
    escalating" — including for the fields individual specs add.
    """
    properties: dict[str, object] = {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "EVERY path you created or edited this round, folder-prefixed with the "
                "owning project's name. This is your work product: the whole set is "
                "reviewed, accepted and (for code) built together, so a change spanning "
                "several files must list all of them — a file you omit is never reviewed. "
                "List your entry point first; it is what the UI leads with. Required "
                "unless you are escalating (see `reason`); [] when a blocker stopped you "
                "before any write."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "Always required. One line: what was produced or changed. When "
                "escalating (see `reason`), this is instead the plain-English "
                "summary of where the work stands and what is blocking it — "
                "written for whoever has to unblock you. No file content either way."
            ),
        },
    }
    if extra_properties:
        properties.update(extra_properties)
    properties.update(_escalation_properties())
    return {"type": "object", "properties": properties, "required": ["summary"]}


def _escalation_properties() -> dict[str, object]:
    """The escalation half of :func:`author_output` (see :func:`author_output`)."""
    return {
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Set this ONLY to escalate a blocker you cannot defensibly resolve: "
                "a short identifier of what is blocking you (e.g. "
                "'critic_iteration_cap', 'spec_ambiguity', 'missing_tech_stack_field'). "
                "Returning it ends your run and hands the blocker to whoever "
                "delegated to you, who owns the resolution — it triages "
                "procedurally, decides itself in autonomous mode, or puts the "
                "matter to the user in interactive mode; the resolution comes back "
                "as the instructions of a later round. Omit it (or null) on a "
                "normal result. Escalate when an iteration cap is exhausted, when a "
                "back-and-forth cannot be reconciled, or when the inputs are "
                "insufficient — never for a stylistic or close-but-defensible call "
                "you can make yourself."
            ),
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Escalations only: concrete options to choose between, when the "
                "blocker admits discrete alternatives. Empty/omitted when you are "
                "asking for free direction."
            ),
        },
    }


def finding_item() -> dict[str, object]:
    """Build the schema for one entry of a critic's ``findings`` list.

    The same object expresses both operations, told apart by ``id``:

    - **no ``id``** — a new finding. ``kind``/``description`` describe it, and
      the engine mints its id.
    - **an ``id``** — an update to that finding. Only the fields present change;
      everything omitted is preserved verbatim by the store itself
      (:func:`kodo.findings.apply_findings`). Setting ``state: "fixed"`` is how a
      critic closes one.

    Nothing is schema-``required``, because an update legitimately carries as
    little as ``{"id": "<some-id>", "state": "fixed"}``. The obligations that do exist
    ("a *new* finding needs kind and description") are stated per field in
    prose and in ``{SHARED:findings_critic}`` — :func:`~kodo.toolspecs.normalize_output`
    would otherwise backfill the missing halves of every update with ``""`` and
    flag the whole result non-compliant.

    ``kind`` stays a free-form string rather than an ``enum``: each critic's own
    catalogue is prose in its ``### Concern vocabulary`` prompt section, which is
    where the per-kind explanations and routing rules a bare enum cannot carry
    belong. Nothing ever enforced an enum anyway —
    :func:`~kodo.toolspecs.normalize_output` validates declared keys and required
    fields, never value constraints.
    """
    return {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "The id of an EXISTING finding you are updating, exactly as "
                    "get_findings reported it (e.g. 'F3'). OMIT it entirely to raise a "
                    "new finding — never invent one."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Concern category, from your own concern vocabulary (the "
                    "'### Concern vocabulary' section of your prompt). Required on a new "
                    "finding; never invent a kind outside your vocabulary."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Plain English: what's wrong and the concrete fix. Required on a new finding."
                ),
            },
            "locations": {
                "type": "array",
                "description": (
                    "Where this finding is, as a list — one entry per place it appears. "
                    "Use several entries when a single problem spans files (a function "
                    "defined in one and mis-called in another): that is ONE finding with "
                    "two locations, never two findings. Required on a new finding unless "
                    "it is genuinely about the work as a whole."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "The file this location is in, folder-prefixed with its "
                                "project's name, exactly as it appeared in your input_paths."
                            ),
                        },
                        "first_line": {
                            "type": ["integer", "null"],
                            "description": "First line of the span this location covers.",
                        },
                        "last_line": {
                            "type": ["integer", "null"],
                            "description": (
                                "Last line of that span (equal to first_line for one line)."
                            ),
                        },
                        "excerpt": {
                            "type": ["string", "null"],
                            "description": "The text at that location, verbatim.",
                        },
                    },
                    "required": ["path"],
                },
            },
            "state": {
                "type": "string",
                "enum": ["outstanding", "fixed"],
                "description": (
                    "Set 'fixed' (with the finding's `id`) once you have re-read the file "
                    "and confirmed it is genuinely resolved. Omit on a new finding — it "
                    "starts outstanding. A finding you do not mention keeps its current "
                    "state; silence never closes anything."
                ),
            },
        },
        "required": [],
    }


def critic_output() -> dict[str, object]:
    """Build the output shape every critic sub-agent returns.

    One shape for all critics: the round's ``findings`` (new ones and updates,
    see :func:`finding_item`) and a one-line ``summary``.

    There is deliberately **no ``path``** either, as of 2026-09-04. A critic
    reviews a whole work product — several files — so a single reviewed path
    could not describe it; and the engine already knows which work product it
    spawned the critic against, so asking the model to restate it only created
    a way for findings to land in the wrong backlog.

    There is deliberately **no ``accept``**. The verdict is derived by the engine
    from the resulting backlog — nothing outstanding means accepted — so a critic
    cannot report a passing verdict while leaving problems open, and the two can
    never disagree. The engine applies the list to the document's session-scoped
    findings log and, when that leaves nothing outstanding, drives the acceptance
    flow; the critic itself never writes to the store and never decides what
    happens next.
    """
    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": finding_item(),
                "description": (
                    "This round's findings: every NEW problem you found (no `id`), plus an "
                    "update for every EXISTING finding whose state or wording changed "
                    '(its `id` plus only the changed fields — `state: "fixed"` to close '
                    "one you verified). Empty only when you found nothing new AND had "
                    "nothing outstanding to act on."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One line summarizing the review.",
            },
        },
        "required": ["findings"],
    }
