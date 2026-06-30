"""``toolchain_deps`` tool spec.

Dependency management is intentionally not implemented yet — the dispatch
handler (:mod:`kodo.tools._toolchain_deps`) always returns a clear
"not implemented" response so an agent gets a usable answer instead of an
unhandled-tool error.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["TOOLCHAIN_DEPS"]


TOOLCHAIN_DEPS: ToolSpec = ToolSpec(
    name="toolchain_deps",
    external_name="Manage Dependencies",
    user_description="Manage project dependencies",
    description=(
        "Add, remove, or update project dependencies in the project's dependency "
        "configuration. The only sanctioned way to change dependency files — "
        "agents do not edit them directly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "update"],
                "description": "Dependency operation to perform.",
            },
            "name": {"type": "string", "description": "Dependency package name."},
            "version": {
                "type": "string",
                "description": "Version constraint, required for add/update.",
            },
        },
        "required": ["action", "name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether the operation succeeded."},
            "message": {"type": "string", "description": "Human-readable result detail."},
        },
        "required": ["success", "message"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"action": "always", "name": "always", "version": "visible"},
    output_visibility={"success": "always", "message": "visible"},
    when_to_use=(
        "A new library (database driver, HTTP client, message queue "
        "client, parser, etc.) is needed before referencing it in an "
        "implementation.",
        "A dependency is no longer needed and should be removed, or an "
        "existing dependency needs a version bump required by the "
        "implementation.",
    ),
)
