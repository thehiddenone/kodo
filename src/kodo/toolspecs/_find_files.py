"""``find_files`` tool spec — a thin wrapper around the bundled ``fd`` util.

Dispatch lives in :mod:`kodo.tools`: it resolves ``root`` against the active
path resolver (so the search stays inside the agent's allowed roots), then runs
the pinned ``fd`` binary from ``~/.kodo/bin/`` under that root. ``fd`` searches
one directory tree, so a multi-project workspace is covered by calling this once
per root returned by ``get_root_paths``.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["FIND_FILES"]


FIND_FILES: ToolSpec = ToolSpec(
    name="find_files",
    external_name="Find Files",
    user_description="Find files by name",
    description=(
        "Find files and directories by name under a single root directory, using "
        "the `fd` tool. `root` MUST be one of the paths returned by "
        "`get_root_paths` (or a subdirectory of one); the search never escapes "
        "it. `pattern` is matched against each entry's name as a regular "
        "expression (smart-case), or as a literal glob when `glob` is true; omit "
        "it to list everything. This searches ONE root — to cover a multi-project "
        "workspace, call it once per root. Hidden files and anything ignored by "
        ".gitignore are skipped unless you opt in. Results are paths relative to "
        "`root` and are capped (see `truncated`)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": (
                    "Absolute path to search under — a root from `get_root_paths` "
                    "(or a subdirectory of one)."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Name pattern: a regular expression by default, or a glob when "
                    "`glob` is true. Omit to list every entry."
                ),
            },
            "glob": {
                "type": "boolean",
                "description": "Treat `pattern` as a glob instead of a regex. Default false.",
            },
            "type": {
                "type": "string",
                "enum": ["file", "directory"],
                "description": "Restrict results to files or directories only.",
            },
            "extension": {
                "type": "string",
                "description": "Filter by file extension (without the dot, e.g. 'py').",
            },
            "hidden": {
                "type": "boolean",
                "description": "Include hidden files/directories (dotfiles). Default false.",
            },
            "no_ignore": {
                "type": "boolean",
                "description": ("Include files ignored by .gitignore / .ignore. Default false."),
            },
            "max_results": {
                "type": "integer",
                "description": "Cap on the number of results (default 1000).",
                "exclusiveMinimum": 0,
            },
        },
        "required": ["root"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "The resolved absolute search root."},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Matching paths, relative to `root`.",
            },
            "count": {"type": "integer", "description": "Number of paths returned."},
            "truncated": {
                "type": "boolean",
                "description": "True if results were capped at `max_results`.",
            },
        },
        "required": ["root", "files", "count", "truncated"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={
        "root": "always",
        "pattern": "always",
        "glob": "visible",
        "type": "visible",
        "extension": "visible",
        "hidden": "visible",
        "no_ignore": "visible",
        "max_results": "visible",
    },
    output_visibility={
        "root": "visible",
        "files": "visible",
        "count": "always",
        "truncated": "always",
    },
    when_to_use=(
        "Locating files or directories by name within one project root — e.g. "
        "finding where a module, config, or test lives before reading it.",
    ),
)
