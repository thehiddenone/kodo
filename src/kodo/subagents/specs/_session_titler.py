"""SubAgentSpec for ``session_titler`` (engine-driven; inline request -> title)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["SESSION_TITLER"]


SESSION_TITLER: SubAgentSpec = SubAgentSpec(
    name="session_titler",
    description="Names a session in 2-6 words from the user's first request.",
    input_schema={
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The user's first request to name the session from.",
            },
        },
        "required": ["request"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "A 2-6 word, Title Case session title naming the subject "
                    "(no emoji/quotes/paths)."
                ),
            },
        },
        "required": ["title"],
    },
)
