"""``get_findings`` tool spec — the shared author/critic backlog (doc/FINDINGS.md).

Guided-mode only, and auto-scoped: it takes no path, because the engine binds
the document the current author/critic round targets to the run. Outside a
review round the scope is empty and the tool returns an empty list rather than
an error — which is what lets one prompt be correct on a first pass and a tenth
alike.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["GET_FINDINGS"]


GET_FINDINGS: ToolSpec = ToolSpec(
    name="get_findings",
    external_name="Get Findings",
    user_description="List outstanding review findings",
    description=(
        "List the review findings recorded against the document you are working on. "
        "A finding is one defect a critic raised, with a stable `id`, a `kind` "
        "(category), a plain-English `description` of what is wrong and the concrete "
        "fix, the `excerpt` where it was found, its `first_line`/`last_line` span, and "
        "a `state` of `outstanding` or `fixed`. By default only `outstanding` findings "
        "are returned; pass `show_all: true` to also see the ones already fixed.\n\n"
        "The list is scoped automatically to the document under review — there is no "
        "path argument, and you cannot see another file's findings. An empty list "
        "means there is nothing outstanding against it (a first pass, or everything "
        "fixed), which is a normal answer and not an error.\n\n"
        "When to use: as your FIRST call, on every pass without exception. The "
        "backlog is not carried in your conversation — this tool is the only place it "
        "exists, and a pass that skips it is working blind. Only available in Guided "
        "mode."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "show_all": {
                "type": "boolean",
                "description": (
                    "False (default) returns only findings still outstanding. True "
                    "also returns findings already marked fixed — use it to check "
                    "what was previously closed before raising something similar."
                ),
            },
        },
        "required": [],
    },
    output_schema={
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": "One entry per finding, oldest first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string"},
                        "description": {"type": "string"},
                        "excerpt": {"type": "string"},
                        "first_line": {"type": ["integer", "null"]},
                        "last_line": {"type": ["integer", "null"]},
                        "state": {"type": "string", "enum": ["outstanding", "fixed"]},
                        "reported_by": {
                            "type": "string",
                            "description": "The critic that raised it, or 'user'.",
                        },
                    },
                    "required": ["id", "kind", "description", "state"],
                },
            },
        },
        "required": ["findings"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"show_all": "visible"},
    output_visibility={"findings": "always"},
    requires_project=True,
)
