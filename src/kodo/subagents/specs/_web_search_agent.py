"""SubAgentSpec for ``web_search`` (engine-driven; drives its own research loop).

Behind the ``web_search`` tool (doc/WEB_SEARCH.md): unlike the retired
``web_summarizer`` (a single silent synthesis pass over pre-gathered text),
this agent drives the entire discovery → read → synthesize loop itself via
``query_search_engine``/``read_webpage`` plus the pacing tools
(``get_web_search_state``/``update_web_search_state``/``wait``/
``remaining_time``). Spawned only through the engine's ungated
``run_web_search_agent`` service (holding the ``web_search`` tool is the
authorization) as a silent, non-nesting tool-loop turn — never through
``run_subagent``.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["WEB_SEARCH_AGENT"]


WEB_SEARCH_AGENT: SubAgentSpec = SubAgentSpec(
    name="web_search",
    description=(
        "Researches a web query end to end — plans which engines to query and when, "
        "reads the promising pages, and synthesizes a themed report — pacing itself "
        "to avoid anti-bot walls and wrapping up within its time budget."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text web search query.",
            },
            "max_themes": {
                "type": "integer",
                "description": "Upper bound on the number of themes to produce.",
                "exclusiveMinimum": 0,
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Total seconds this run has before it must wrap up (already "
                    "clamped by the tool to at most 600). Check remaining_time "
                    "periodically rather than tracking this yourself."
                ),
            },
        },
        "required": ["query", "max_themes", "timeout"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "description": (
                    "Themed groups of findings, each a distinct angle on the query — "
                    "ideally an independent option/approach to choose from. Empty when "
                    "the search could not be completed (see note)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "One-sentence description of the theme.",
                        },
                        "details": {
                            "type": "string",
                            "description": (
                                "What the sources say about this theme — the core idea, "
                                "perspective, or solution variant, synthesized across "
                                "sources."
                            ),
                        },
                        "links": {
                            "type": "array",
                            "description": "Source URLs this theme was drawn from.",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["summary", "details", "links"],
                },
            },
            "note": {
                "type": "string",
                "description": (
                    "What was searched, what was skipped or blocked, and any "
                    "degradation — an empty themes list with an explanatory note means "
                    "the search could not be completed, not that the web has no answer."
                ),
            },
        },
        "required": ["themes", "note"],
    },
)
