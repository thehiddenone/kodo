"""SubAgentSpec for ``web_summarizer`` (engine-driven; scraped pages -> themes).

Phase 3 of the ``web_search`` pipeline (doc/WEB_SEARCH.md): the tool hands the
scraped text blocks to this silent sub-agent, which groups them into themes.
Like ``session_titler``/``compactor`` it is engine-driven only — spawned via the
engine's ungated ``run_web_summarizer`` service (holding the ``web_search`` tool
is the authorization), never through ``run_subagent``.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["WEB_SUMMARIZER"]


WEB_SUMMARIZER: SubAgentSpec = SubAgentSpec(
    name="web_summarizer",
    description=(
        "Groups scraped web-page text into themes — distinct angles/options on the "
        "search query, each with a one-sentence summary, a details text, and its "
        "source links."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The web search query the sources were gathered for.",
            },
            "max_themes": {
                "type": "integer",
                "description": "Upper bound on the number of themes to produce.",
                "exclusiveMinimum": 0,
            },
            "sources": {
                "type": "array",
                "description": "Scraped pages: one entry per source document.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Source page URL."},
                        "title": {"type": "string", "description": "Source page title."},
                        "text": {
                            "type": "string",
                            "description": "Extracted main text content of the page.",
                        },
                    },
                    "required": ["url", "title", "text"],
                },
            },
        },
        "required": ["query", "max_themes", "sources"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "description": (
                    "Themed groups of findings, each a distinct angle on the query — "
                    "ideally an independent option/approach to choose from."
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
                            "description": (
                                "URLs of the input sources this theme was drawn from."
                            ),
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["summary", "details", "links"],
                },
            },
        },
        "required": ["themes"],
    },
)
