"""``toolchain_deps`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import ToolSpec

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
    when_to_use=(
        "A new library (database driver, HTTP client, message queue "
        "client, parser, etc.) is needed before referencing it in an "
        "implementation.",
        "A dependency is no longer needed and should be removed, or an "
        "existing dependency needs a version bump required by the "
        "implementation.",
    ),
)
