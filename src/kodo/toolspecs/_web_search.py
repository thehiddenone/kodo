"""``web_search`` tool spec — external web search for the Investigator.

**Placeholder.** The spec is real and dispatchable, but the handler
(:class:`kodo.tools.WebSearchTool`) does not yet reach any provider — it returns
a schema-compliant "not wired yet" envelope. The intended backing is the public
DuckDuckGo search API; wiring it is a deferred follow-up. The stable contract
(input ``query``/``max_results``, output ``results``/``note``) lets the
Investigator prompt and the caller roster settle now, ahead of the real
integration.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["WEB_SEARCH"]


WEB_SEARCH: ToolSpec = ToolSpec(
    name="web_search",
    external_name="Web Search",
    user_description="Search the web",
    description=(
        "Search the public web for information relevant to a problem — library "
        "documentation, error messages, API references, known solutions. `query` "
        "is a free-text search string; `max_results` caps how many hits come "
        "back (default 5). Returns a list of results, each with a `title`, `url`, "
        "and short `snippet`, plus a `note` field carrying any status message. "
        "NOTE: this tool is not yet connected to a search provider — it currently "
        "returns an empty result list and a note saying so; fall back to "
        "code exploration until it is wired."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text web search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Cap on the number of results (default 5).",
                "exclusiveMinimum": 0,
            },
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "description": "One entry per search hit (empty while unwired).",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Result title."},
                        "url": {"type": "string", "description": "Result URL."},
                        "snippet": {
                            "type": "string",
                            "description": "Short excerpt describing the result.",
                        },
                    },
                    "required": ["title", "url", "snippet"],
                },
            },
            "note": {
                "type": "string",
                "description": "Status message (e.g. an unwired-provider notice).",
            },
        },
        "required": ["results", "note"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "query": "always",
        "max_results": "visible",
    },
    output_visibility={
        "results": "visible",
        "note": "always",
    },
    when_to_use=(
        "Looking up external information a codebase can't answer on its own — "
        "third-party library/API documentation, the meaning of an error message, "
        "or a known solution to a general programming problem.",
    ),
)
