"""Declarative schema builders shared by the sub-agent specs.

These are pure schema *constructors* (no dispatch or runtime logic) used by the
one-spec-per-file modules in this package to assemble their ``input_schema`` /
``output_schema`` without copy-pasting the common envelopes. Each builder returns
a fresh dict so callers never share mutable schema state.

The shapes mirror the contracts the agent prompts already describe:

- **Pipeline input** — the structured task a file-backed sub-agent receives when
  delegated to: free-form ``instructions`` plus the real file paths it should
  read (a named collection, since a single round often needs several distinct
  inputs — e.g. requirements *and* architecture) and (for authors revising
  existing work) the path being revised.
- **Author/solo output** — the path(s) a producing sub-agent wrote, plus which
  one is primary (what a critic reviews / what the author-critic loop tracks).
- **Critic output** — a ``verdict`` plus a list of structured ``concerns`` whose
  ``kind`` is drawn from that critic's own vocabulary. This shape is also reused
  verbatim as the ``concerns`` field of a ``feedback`` entry in a document's
  ``.jsonl`` evolution log (see ``kodo.guided_state``).

Inline agents (``compactor``, ``session_titler``, ``toolchain_python``) read and
write files directly with no structured pipeline contract; they declare their
inline/path shapes directly in their own modules rather than through these
builders.
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
        "What to do this round: produce a fresh document, or revise the prior "
        "one per the listed concerns."
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
_FOR_REVISION_PATH = {
    "type": ["string", "null"],
    "description": (
        "Path of the prior document to revise this round (authors only; "
        "omitted/null on the first round)."
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
            read (rendered as the ``input_paths`` field description).
        require_input_paths: Whether ``input_paths`` is required
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
        "input_paths": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                f"{input_paths} A named collection (label -> path), so several "
                "distinct inputs can be passed in one round, e.g. "
                '{"requirements": "specs/requirements/auth.md", "architecture": '
                '"specs/architecture/system.md"}.'
            ),
        },
        "for_revision_path": dict(_FOR_REVISION_PATH),
    }
    if extra_properties:
        properties.update(extra_properties)
    required = ["instructions"]
    if require_input_paths:
        required.append("input_paths")
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
    """Build the output shape for a file-writing author/solo sub-agent."""
    properties: dict[str, object] = {
        "primary_path": {
            "type": "string",
            "description": (
                "The path a critic should review / the author-critic loop tracks. "
                "Required even when only one file was touched."
            ),
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every path this agent created or edited this round.",
        },
        "summary": {
            "type": "string",
            "description": "One line: what was produced or changed. No file content.",
        },
    }
    if extra_properties:
        properties.update(extra_properties)
    required = ["primary_path", "paths", "summary"]
    if extra_required:
        required.extend(extra_required)
    return {"type": "object", "properties": properties, "required": required}


def concern_item(kinds: list[str]) -> dict[str, object]:
    """Build the schema for one structured critic concern.

    Fields: ``kind`` (constrained to the critic's own vocabulary),
    ``description``, and optional ``first_line`` / ``last_line`` / ``excerpt``.
    This same shape is reused verbatim as a ``feedback`` jsonl entry's
    ``concerns`` items (see ``kodo.guided_state``).
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
            "verdict": {"type": "string", "enum": ["accepted", "rejected"]},
            "concerns": {
                "type": "array",
                "items": concern_item(kinds),
                "description": "Empty when accepted; non-empty when rejected.",
            },
        },
        "required": ["verdict", "concerns"],
    }
