"""SubAgentSpec for ``investigator`` — a standalone read-only investigator.

The Investigator answers questions about a problem by exploring the existing
code (read-only) and/or searching the web. Two operating modes, selected per
call by ``mode``:

- ``"qa"`` — answer a specific list of ``questions``; the result is a matching
  list of ``answers``.
- ``"report"`` — write one continuous investigative ``report`` on the topic in
  ``instructions`` (used when the caller wants a full write-up to document,
  not point answers).

Its input carries a context-setting prompt (``instructions``) from the caller,
the ``questions`` to answer (qa mode), and the local filesystem ``roots`` whose
code should be investigated (omitted for a web-only investigation).
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["INVESTIGATOR"]


INVESTIGATOR: SubAgentSpec = SubAgentSpec(
    name="investigator",
    description=(
        "Read-only investigator: explores existing code and/or searches the web to answer "
        "specific questions (qa mode) or produce a full investigative report (report mode)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["qa", "report"],
                "description": (
                    "'qa' to answer the listed questions; 'report' to write one continuous "
                    "investigative report on the topic in instructions."
                ),
            },
            "instructions": {
                "type": "string",
                "description": (
                    "Context-setting prompt from the caller: what problem is being "
                    "investigated, what is already known, and what the investigation should "
                    "establish. The single most important input — frame the whole task here."
                ),
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific questions to answer (qa mode). One clear question per entry. "
                    "Leave empty/omit in report mode."
                ),
            },
            "roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute local filesystem paths that are roots of code to investigate "
                    "(each a path from get_root_paths, or a subdirectory of one). Omit/empty "
                    "for a web-only investigation."
                ),
            },
        },
        "required": ["mode", "instructions"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "description": ("One entry per input question (qa mode); empty in report mode."),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question being answered (echoed from the input).",
                        },
                        "answer": {
                            "type": "string",
                            "description": (
                                "The answer, grounded in what was found. State plainly when "
                                "the evidence was inconclusive."
                            ),
                        },
                    },
                    "required": ["question", "answer"],
                },
            },
            "report": {
                "type": "string",
                "description": ("The full investigative report (report mode); empty in qa mode."),
            },
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "What the findings rest on: file paths (with line refs where useful) and "
                    "any web URLs consulted."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One line: what the investigation established. No file content.",
            },
        },
        "required": ["summary"],
    },
)
