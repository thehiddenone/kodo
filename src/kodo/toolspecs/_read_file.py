"""``read_file`` tool spec — read a file whole, by line range, or by regex pattern."""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_FILE"]


READ_FILE: ToolSpec = ToolSpec(
    name="read_file",
    external_name="Read File",
    user_description="Read a file's content",
    description=(
        "Read the content of a file inside the project root. By default "
        "returns the whole file (like `cat`). Pass `ranges` to read one or "
        "more specific 1-based, inclusive line ranges instead of the whole "
        "file. Pass `pattern` to search the file's content with a regular "
        "expression (like ripgrep) and get back each match with "
        "`context_before`/`context_after` lines of surrounding context — "
        "`ranges` and `pattern` are mutually exclusive."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file, relative to the project root (or absolute inside it)."
                ),
            },
            "ranges": {
                "type": "array",
                "description": (
                    "One or more 1-based, inclusive line ranges to read instead of the "
                    "whole file. Mutually exclusive with `pattern`."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["start_line", "end_line"],
                },
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Regular expression to search the file's content for, like ripgrep. "
                    "Mutually exclusive with `ranges`."
                ),
            },
            "ignore_case": {
                "type": "boolean",
                "description": (
                    "Case-insensitive `pattern` match. Default false. Only used with `pattern`."
                ),
            },
            "context_before": {
                "type": "integer",
                "description": (
                    "Lines of context to include before each match. Default 0. "
                    "Only used with `pattern`."
                ),
                "minimum": 0,
            },
            "context_after": {
                "type": "integer",
                "description": (
                    "Lines of context to include after each match. Default 0. "
                    "Only used with `pattern`."
                ),
                "minimum": 0,
            },
            "max_matches": {
                "type": "integer",
                "description": (
                    "Cap on the number of matches. Default 200. Only used with `pattern`."
                ),
                "exclusiveMinimum": 0,
            },
        },
        "required": ["path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The resolved file path."},
            "total_lines": {
                "type": "integer",
                "description": "Total number of lines in the file.",
            },
            "sections": {
                "type": "array",
                "description": "Present in whole-file/`ranges` mode: the requested line ranges.",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "content": {"type": "string"},
                    },
                    "required": ["start_line", "end_line", "content"],
                },
            },
            "matches": {
                "type": "array",
                "description": "Present in `pattern` mode: one entry per match.",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_number": {"type": "integer"},
                        "line": {"type": "string"},
                        "context_before": {"type": "array", "items": {"type": "string"}},
                        "context_after": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["line_number", "line", "context_before", "context_after"],
                },
            },
            "truncated": {
                "type": "boolean",
                "description": "Present in `pattern` mode: true if `max_matches` was hit.",
            },
        },
        "required": ["path", "total_lines"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={
        "path": "always",
        "ranges": "visible",
        "pattern": "always",
        "ignore_case": "visible",
        "context_before": "visible",
        "context_after": "visible",
        "max_matches": "visible",
    },
    output_visibility={
        "path": "always",
        "total_lines": "always",
        "sections": "visible",
        "matches": "visible",
        "truncated": "always",
    },
    when_to_use=(
        "Reading a file's full content before editing it, or to understand existing context.",
        "Reading only specific line ranges of a large file, when you already know roughly "
        "where the relevant content is.",
        "Searching a single file's content for a pattern (e.g. a prior finding's excerpt, a "
        "function definition) with surrounding context, instead of reading the whole file.",
    ),
    requires_project=True,
)
