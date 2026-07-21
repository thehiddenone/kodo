"""``find_text_in_files`` tool spec — a thin wrapper around the bundled ``rg``.

Dispatch lives in :mod:`kodo.tools`: it resolves ``root`` against the active
path resolver (so the search stays inside the agent's allowed roots), then runs
the pinned ripgrep (``rg``) binary from ``~/.kodo/bin/`` under that root with
``--json`` output. ripgrep searches one directory tree, so a multi-project
workspace is covered by calling this once per root from ``get_root_paths``.

``temporary: true`` resolves ``root`` under the session's private scratch
directory instead (see :meth:`~kodo.tools.Tool.resolve_path`).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["FIND_TEXT_IN_FILES"]


FIND_TEXT_IN_FILES: ToolSpec = ToolSpec(
    name="find_text_in_files",
    external_name="Find Text In Files",
    user_description="Search file contents",
    description=(
        "Search the CONTENTS of files for text under a single root directory, "
        "using ripgrep (`rg`). `root` MUST be one of the paths returned by "
        "`get_root_paths` (or a subdirectory of one) — unless `temporary` is "
        "true, in which case `root` resolves under the session's scratch "
        "directory instead; the search never escapes it. `query` is a regular "
        "expression (smart-case) unless `fixed_strings` is true, in which case "
        "it is matched literally. This searches ONE root — to cover a "
        "multi-project workspace, call it once per root. Hidden files and "
        "anything ignored by .gitignore are skipped unless you opt in. Each "
        "match reports the file (relative to `root`), 1-based line number, and "
        "the line text; results are capped (see `truncated`)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search pattern: a regular expression by default, or a literal "
                    "string when `fixed_strings` is true."
                ),
            },
            "root": {
                "type": "string",
                "description": (
                    "Absolute path to search under — a root from `get_root_paths` "
                    "(or a subdirectory of one). When `temporary` is true, this instead "
                    "resolves relative to the session's scratch directory (pass `.` to "
                    "search the whole thing)."
                ),
            },
            "glob": {
                "type": "string",
                "description": (
                    "Only search files whose path matches this glob (e.g. "
                    "'*.py'). Prefix with '!' to exclude."
                ),
            },
            "fixed_strings": {
                "type": "boolean",
                "description": "Treat `query` as a literal string, not a regex. Default false.",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": (
                    "Force a case-insensitive search (overrides the default "
                    "smart-case behaviour). Default false."
                ),
            },
            "hidden": {
                "type": "boolean",
                "description": "Include hidden files/directories (dotfiles). Default false.",
            },
            "no_ignore": {
                "type": "boolean",
                "description": "Search files ignored by .gitignore / .ignore. Default false.",
            },
            "max_results": {
                "type": "integer",
                "description": "Cap on the number of matches (default 1000).",
                "exclusiveMinimum": 0,
            },
            "temporary": {
                "type": "boolean",
                "description": (
                    "When true, `root` resolves under this session's private scratch "
                    "directory instead of requiring one of `get_root_paths`'s roots. "
                    "Use this to search throwaway work you don't want in the project "
                    "itself; this call is always allowed regardless of Command Control "
                    "posture. Default false."
                ),
            },
        },
        "required": ["query", "root"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "The resolved absolute search root."},
            "matches": {
                "type": "array",
                "description": "One entry per matching line.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to `root`.",
                        },
                        "line": {
                            "type": "integer",
                            "description": "1-based line number of the match.",
                        },
                        "text": {
                            "type": "string",
                            "description": "The matching line's text (trailing newline stripped).",
                        },
                    },
                    "required": ["path", "line", "text"],
                },
            },
            "count": {"type": "integer", "description": "Number of matches returned."},
            "truncated": {
                "type": "boolean",
                "description": "True if matches were capped at `max_results`.",
            },
        },
        "required": ["root", "matches", "count", "truncated"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={
        "query": "always",
        "root": "always",
        "glob": "visible",
        "fixed_strings": "visible",
        "case_insensitive": "visible",
        "hidden": "visible",
        "no_ignore": "visible",
        "max_results": "visible",
        "temporary": "visible",
    },
    output_visibility={
        "root": "visible",
        "matches": "visible",
        "count": "always",
        "truncated": "always",
    },
    when_to_use=(
        "Finding where a symbol, string, or pattern appears across a project's "
        "files within one root — e.g. tracing a function's call sites or locating "
        "a config key before editing.",
        "Pass `temporary: true` to search the session's private scratch "
        "directory instead of a project root.",
    ),
    requires_project=True,
)
