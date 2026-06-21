"""``toolchain_build`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["TOOLCHAIN_BUILD"]


TOOLCHAIN_BUILD: ToolSpec = ToolSpec(
    name="toolchain_build",
    external_name="Build Project",
    user_description="Build the project",
    description=(
        "Compile or build the project in the language/tooling declared by the Tech "
        "Stack. Returns success or a list of build errors."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether the build succeeded."},
            "errors": {
                "type": "array",
                "description": "Build error messages (empty on success).",
                "items": {"type": "string"},
            },
        },
        "required": ["success", "errors"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={},
    output_visibility={"success": "always", "errors": "visible"},
    when_to_use=(
        "After publishing new or superseding `code` artifacts, to confirm "
        "the project builds before running tests.",
        "After a refactor change, to confirm the build still succeeds before re-running tests.",
    ),
)
