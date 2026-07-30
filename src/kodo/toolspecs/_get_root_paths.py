"""``get_root_paths`` tool spec — list the workspace root directories.

Dispatch lives in :mod:`kodo.tools` and simply returns the root list the
engine computed for the run — one entry per bound root (a VS Code workspace
folder, or a project created via ``scaffold_new_project``),
shared uniformly by both workflow modes. The list is sourced from the
workspace state the VS Code extension keeps synced over the WS protocol
(``workspace.folders``, pushed at startup and on every workspace-folder change),
so the tool itself needs no live round-trip.

``temporary: true`` instead reports this session's private scratch directory
(``kodo.project.session_temp_dir``) as the sole root — the same directory the
native file tools resolve into with their own ``temporary: true`` (see
:meth:`kodo.tools.Tool.resolve_path`), and that ``run_command`` accepts as an
absolute ``working_dir`` (already unrestricted for
:meth:`kodo.tools.LogicalPathResolver.resolve`, which allows any absolute
path).

Unlike most other native tools this does *not* set ``requires_project``: with
no project/workspace bound yet it simply returns an empty ``roots`` list
(``GetRootPathsTool`` is a pure read of ``ToolContext.root_paths``, which is
already ``()`` in that state) instead of the generic ``NO_PROJECT_ERROR`` —
an empty list is itself a meaningful, non-error signal to the agent that
``scaffold_new_project`` needs calling first. ``temporary: true`` never needed
a project to begin with (the scratch directory is keyed by session id alone).
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
        "root returned here.\n\n"
        "Pass `temporary: true` instead to get a single root pointing at your "
        "private scratch directory (see below) — e.g. to pass as `run_command`'s "
        "`working_dir` for throwaway work you don't want in the project "
        "itself.\n\n"
        "When to use: before searching the codebase, to discover the root(s) to "
        "pass to `find_files` / `find_text_in_files`. Also the way to check "
        "whether a project/workspace exists yet at all — with no workspace "
        "bound this returns an empty `roots` list rather than an error, which is "
        "itself the signal that `scaffold_new_project` is needed first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "temporary": {
                "type": "boolean",
                "description": (
                    "When true, return a single root for this session's private "
                    "scratch directory instead of the usual project root(s) — the "
                    "same directory `create_file` / `create_directory` / "
                    "`edit_file` / `filesystem` / `find_files` / "
                    "`find_text_in_files` resolve into when called with "
                    "`temporary: true`, and that `run_command` accepts as an "
                    "absolute `working_dir`. Use it to get that directory's "
                    "absolute path — e.g. to pass as `run_command`'s "
                    "`working_dir`, or to build an absolute path for another "
                    "tool's `temporary: true` call. Default false."
                ),
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "roots": {
                "type": "array",
                "description": (
                    "The root directories. One entry per project, unless "
                    "`temporary` was true, in which case a single entry for the "
                    "private scratch directory."
                ),
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
    input_visibility={"temporary": "visible"},
    output_visibility={"roots": "always"},
)
