"""``toolchain_build`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import ToolSpec

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
    when_to_use=(
        "After publishing new or superseding `code` artifacts, to confirm "
        "the project builds before running tests.",
        "After a refactor change, to confirm the build still succeeds before re-running tests.",
    ),
)
