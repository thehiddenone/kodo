"""SubAgentSpec for ``compactor`` (engine-driven; inline transcript -> summary)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["COMPACTOR"]


COMPACTOR: SubAgentSpec = SubAgentSpec(
    name="compactor",
    description="Summarises a conversation transcript into a compact handoff briefing.",
    input_schema={
        "type": "object",
        "properties": {
            "transcript": {
                "type": "string",
                "description": "The conversation transcript to compact (user, agent, tool turns).",
            },
        },
        "required": ["transcript"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "The compact briefing the conversation continues from.",
            },
        },
        "required": ["summary"],
    },
)
