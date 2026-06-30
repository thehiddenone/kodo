"""``document_feedback`` tool spec — a critic records its verdict on one file."""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["DOCUMENT_FEEDBACK"]

# Matches kodo.subagents.specs._shapes.concern_item's shape by convention
# (kodo.toolspecs is below kodo.subagents in the import graph, so this is a
# deliberate, small duplication rather than a cross-tier import).
_CONCERN_ITEM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": "Concern category, from the critic's own vocabulary.",
        },
        "description": {
            "type": "string",
            "description": "Plain English: what's wrong and the concrete fix.",
        },
        "first_line": {"type": ["integer", "null"]},
        "last_line": {"type": ["integer", "null"]},
        "excerpt": {"type": ["string", "null"]},
    },
    "required": ["kind", "description"],
}


DOCUMENT_FEEDBACK: ToolSpec = ToolSpec(
    name="document_feedback",
    external_name="Document Feedback",
    user_description="Record a review verdict on a file",
    description=(
        "Record a critic's review verdict on one file, appending a `feedback` "
        "entry to that file's evolution log. Set `accept` to true when the "
        "file passes review (with empty `concerns`) or false when it needs "
        "revision (with one or more `concerns`). This is the only output a "
        "critic produces per review — it never edits the file itself, and it "
        "never decides what happens next: when `accept` is true, the engine "
        "alone handles presenting the file to the user (if interactive) and "
        "recording acceptance."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file under review (delivered as task input).",
            },
            "accept": {
                "type": "boolean",
                "description": "True if the file passes review; false if it needs revision.",
            },
            "concerns": {
                "type": "array",
                "description": (
                    "Required (non-empty) when `accept` is false; empty/omitted when true."
                ),
                "items": _CONCERN_ITEM_SCHEMA,
            },
            "summary": {
                "type": "string",
                "description": "Optional one-line summary of the review.",
            },
        },
        "required": ["path", "accept"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'recorded'."},
            "path": {"type": "string", "description": "The reviewed file's path."},
        },
        "required": ["status", "path"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "path": "always",
        "accept": "always",
        "concerns": "visible",
        "summary": "visible",
    },
    output_visibility={"status": "always", "path": "always"},
    when_to_use=(
        "After reviewing a file a critic was asked to review — exactly one call per review, "
        "aggregating every concern.",
        "To accept a file (`accept: true`, empty concerns) once it has no remaining concerns.",
        "To reject a file (`accept: false`, one or more concerns) so its author revises it.",
    ),
)
