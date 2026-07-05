"""``get_web_search_state`` tool spec — read the web_search agent's TTL memory.

Exclusive to the ``web_search`` agent (doc/WEB_SEARCH.md) by convention — not
listed in any other agent's frontmatter ``tools:``. Backed by
:class:`kodo.websearch.WebSearchStateStore`: a persistent key-value scratch
space (12-hour TTL per entry, refreshed on write) the agent uses to track
which engines have flagged it as a bot and when it last queried each one, so
it doesn't burst requests or repeat a query an engine already walled.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["GET_WEB_SEARCH_STATE"]

GET_WEB_SEARCH_STATE: ToolSpec = ToolSpec(
    name="get_web_search_state",
    external_name="Get Web Search State",
    user_description="Read search-pacing memory",
    description=(
        "Return your entire persistent key-value memory (see "
        "update_web_search_state). Every entry has a 12-hour TTL from its last "
        "write; an entry untouched for 12 hours is silently evicted before this "
        "call returns anything. A key you stored with the special <time_mark> "
        "value comes back as a number-as-string: the seconds elapsed since you "
        "recorded it, recomputed fresh on every call — not the raw timestamp. Use "
        "that to judge how long ago you last did something (e.g. last queried an "
        "engine). Call this before querying an engine you've used before, so you "
        "know whether it's flagged blocked and how recently you hit it."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "state": {
                "type": "object",
                "description": (
                    "Your full key-value memory. A key stored as <time_mark> comes "
                    "back holding the elapsed seconds since that mark, as a string."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["state"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={"state": "visible"},
    when_to_use=(
        "Before querying a search engine you've used before this session, to check "
        "whether it's flagged blocked or how recently you queried it.",
        "Periodically during a research loop, to keep pacing decisions grounded in "
        "what you've actually recorded rather than guesswork.",
    ),
)
