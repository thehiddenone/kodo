"""Declarative schema builders shared by the sub-agent specs.

These are pure schema *constructors* (no dispatch or runtime logic) used by the
one-spec-per-file modules in this package to assemble their ``input_schema`` /
``output_schema`` without copy-pasting the common envelopes. Each builder returns
a fresh dict so callers never share mutable schema state.

The shapes mirror the contracts the agent prompts already describe:

- **Pipeline input** — the structured task an artifact-backed sub-agent receives
  when delegated to: free-form ``instructions`` plus the artifact IDs it should
  read and (for authors) any prior outputs to revise.
- **Author/solo output** — the artifact IDs a producing sub-agent published.
- **Critic output** — a ``verdict`` plus a list of structured ``concerns`` whose
  ``kind`` is drawn from that critic's own vocabulary (matches
  :class:`kodo.workspace.Concern` / :class:`kodo.workspace.Verdict`).

Inline agents (``compactor``, ``session_titler``, ``python_toolchain``) read and
write no Workspace artifacts; they declare their inline/path shapes directly in
their own modules rather than through these builders.
"""

from __future__ import annotations

__all__ = [
    "author_output",
    "concern_item",
    "critic_output",
    "pipeline_input",
]

_INSTRUCTIONS = {
    "type": "string",
    "description": (
        "What to do this round: produce a fresh artifact, or revise the prior one "
        "per the listed concerns."
    ),
}
_PROJECT_CODE = {
    "type": "string",
    "description": "Inherited PROJECTCODE; never invented.",
}
_RESPONSIBILITY_CODE = {
    "type": "string",
    "description": "Component codename (per-codename stages only).",
}
_FOR_REVISION = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Prior outputs to revise this round (authors only; empty/omitted on the "
        "first round). A list because an author may have published several "
        "artifacts that all need revision."
    ),
}


def pipeline_input(
    *,
    input_artifacts: str,
    require_input_artifacts: bool = True,
    require_responsibility: bool = False,
    extra_properties: dict[str, object] | None = None,
    extra_required: list[str] | None = None,
) -> dict[str, object]:
    """Build the structured task an artifact-backed sub-agent receives.

    Args:
        input_artifacts: Human description of which artifact TYPES this agent must
            read (rendered as the ``input_artifact_ids`` field description).
        require_input_artifacts: Whether ``input_artifact_ids`` is required
            (``narrative_author`` works from the user prompt, so it is not).
        require_responsibility: Whether ``responsibility_code`` is required
            (per-codename stages).
        extra_properties: Agent-specific extra input properties to merge in.
        extra_required: Agent-specific extra required field names.
    """
    properties: dict[str, object] = {
        "instructions": dict(_INSTRUCTIONS),
        "project_code": dict(_PROJECT_CODE),
        "responsibility_code": dict(_RESPONSIBILITY_CODE),
        "input_artifact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": input_artifacts,
        },
        "for_revision_artifact_ids": dict(_FOR_REVISION),
    }
    if extra_properties:
        properties.update(extra_properties)
    required = ["instructions"]
    if require_input_artifacts:
        required.append("input_artifact_ids")
    if require_responsibility:
        required.append("responsibility_code")
    if extra_required:
        required.extend(extra_required)
    return {"type": "object", "properties": properties, "required": required}


def author_output(
    *,
    extra_properties: dict[str, object] | None = None,
    extra_required: list[str] | None = None,
) -> dict[str, object]:
    """Build the output shape for an artifact-publishing author/solo sub-agent."""
    properties: dict[str, object] = {
        "artifact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs of the artifacts this agent published this round.",
        },
        "summary": {
            "type": "string",
            "description": "One line: what was produced or changed. No artifact content.",
        },
    }
    if extra_properties:
        properties.update(extra_properties)
    required = ["artifact_ids", "summary"]
    if extra_required:
        required.extend(extra_required)
    return {"type": "object", "properties": properties, "required": required}


def concern_item(kinds: list[str]) -> dict[str, object]:
    """Build the schema for one structured critic concern.

    Matches :class:`kodo.workspace.Concern` (``kind``, ``description``, optional
    ``first_line`` / ``last_line`` / ``excerpt``); ``kind`` is constrained to the
    critic's own vocabulary.
    """
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(kinds)},
            "description": {"type": "string"},
            "first_line": {"type": ["integer", "null"]},
            "last_line": {"type": ["integer", "null"]},
            "excerpt": {"type": ["string", "null"]},
        },
        "required": ["kind", "description"],
    }


def critic_output(kinds: list[str]) -> dict[str, object]:
    """Build the output shape for a critic sub-agent over its concern vocabulary."""
    return {
        "type": "object",
        "properties": {
            "feedback_artifact_id": {
                "type": "string",
                "description": "ID of the published feedback artifact (durable record).",
            },
            "verdict": {"type": "string", "enum": ["accepted", "rejected"]},
            "concerns": {
                "type": "array",
                "items": concern_item(kinds),
                "description": "Empty when accepted; non-empty when rejected.",
            },
        },
        "required": ["verdict", "concerns"],
    }
