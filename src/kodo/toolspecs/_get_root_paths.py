"""``get_root_paths`` tool spec — list the workspace root directories.

Dispatch lives in :mod:`kodo.tools` and simply returns the mode-aware root list
the engine computed for the run (the bound project in Guided mode; every open
VS Code workspace folder in Problem Solver mode). The list is sourced from the
workspace state the VS Code extension keeps synced over the WS protocol
(``workspace.folders``, pushed at startup and on every workspace-folder change),
so the tool itself needs no live round-trip.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["GET_ROOT_PATHS"]


GET_ROOT_PATHS: ToolSpec = ToolSpec(
    name="get_root_paths",
    external_name="Get Root Paths",
    user_description="List workspace project roots",
    description=(
        "Return the filesystem root directories you are working within. In a "
        "single-project workspace this is one path; in a multi-project workspace "
        "it is one path per project. Each entry has a 'name' (a human/logical "
        "label) and an absolute 'path'. Call this FIRST when you need to search "
        "the codebase: `find_files` and `find_text_in_files` each operate inside "
        "ONE root, so to cover a multi-project workspace you call them once per "
        "root returned here. Takes no arguments."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
    output_schema={
        "type": "object",
        "properties": {
            "roots": {
                "type": "array",
                "description": "The root directories, one entry per project.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Human/logical label for the root.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the root directory.",
                        },
                    },
                    "required": ["name", "path"],
                },
            },
        },
        "required": ["roots"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={"roots": "always"},
    when_to_use=(
        "Before searching the codebase, to discover the project root(s) to pass "
        "as the `root` of `find_files` / `find_text_in_files` — especially in a "
        "multi-project workspace where each search covers only one root.",
    ),
)
