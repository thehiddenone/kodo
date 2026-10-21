"""``report_agent_review`` tool spec — hand one agent review's verdict to the engine.

doc/AGENT_BUILDER.md §5.3. Agent Review (``kodo_agent_review``) calls it once
with Agent Critic's result. The engine shows the review in the session and,
when it holds concerns, offers to open Agent Builder on them.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["REPORT_AGENT_REVIEW"]

_STR = {"type": "string"}

REPORT_AGENT_REVIEW: ToolSpec = ToolSpec(
    name="report_agent_review",
    external_name="Report Agent Review",
    user_description="Show the agent review",
    description=(
        "Report Agent Critic's review of one agent, exactly as the critic returned it. The "
        "engine shows every concern and the summary to the user — you write no callout for "
        "them. When `accept` is false, the engine then asks the user whether to open Agent "
        "Builder to fix the concerns. `accept` is true exactly when `concerns` is empty. "
        "One call per session; a second call is refused.\n\n"
        "When to use: once, right after `run_subagent_kodo_agent_critic` returns a verdict."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {**_STR, "description": "The reviewed agent's exact name."},
            "accept": {"type": "boolean", "description": "The critic's `accept`."},
            "concerns": {
                "type": "array",
                "description": "The critic's `concerns`, in its order, each copied verbatim.",
                "items": {
                    "type": "object",
                    "properties": {
                        "rule": _STR,
                        "quote": _STR,
                        "location": _STR,
                        "fix": _STR,
                    },
                    "required": ["rule", "quote", "location", "fix"],
                },
            },
            "summary": {**_STR, "description": "The critic's `summary`."},
        },
        "required": ["name", "accept", "concerns", "summary"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "reported": {"type": "boolean", "description": "Always true."},
            "concerns": {"type": "integer", "description": "How many concerns were shown."},
        },
        "required": ["reported", "concerns"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"name": "always", "accept": "always", "summary": "visible"},
    output_visibility={"reported": "hidden", "concerns": "always"},
)
