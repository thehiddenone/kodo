"""``web_search`` tool spec — themed web research for the Investigator.

Backed by the ``web_search`` sub-agent (doc/WEB_SEARCH.md): a real agent that
plans its own research loop — deciding which engines to query and when
(``query_search_engine``), reading the pages worth reading (``read_webpage``),
pacing itself to avoid anti-bot walls (``get_web_search_state``/
``update_web_search_state``/``wait``), and wrapping up within `timeout`
(``remaining_time``) — rather than a fixed discover-then-scrape-then-
summarize pipeline. The output is a themed report: each theme is one distinct
angle — ideally an independent way to solve the problem — with a
one-sentence ``summary``, a synthesized ``details`` text, and the source
``links`` it was drawn from.

Best-effort by design: whatever the agent couldn't do — an engine wall it
worked around or gave up on, a page it couldn't read, running out of time —
is reported in ``note`` rather than raised.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["WEB_SEARCH"]

# Bounds on the `timeout` input, seconds.
_DEFAULT_TIMEOUT_S = 180
_MAX_TIMEOUT_S = 600


WEB_SEARCH: ToolSpec = ToolSpec(
    name="web_search",
    external_name="Web Search",
    user_description="Search the web",
    description=(
        "Search the public web and get back a *themed report* rather than raw hits. "
        "A dedicated research agent plans its own search — which engines to query, "
        "which pages to read in full, how to pace itself around anti-bot walls — and "
        "distills what it finds into `themes`: each theme has a one-sentence "
        "`summary`, a `details` text carrying the core idea (a perspective on the "
        "problem, a variant of a solution, ...), and the source `links` it was drawn "
        "from. Where possible, themes are independent alternatives — several "
        "distinct options to choose from, not one narrative. `query` is a free-text "
        "search string; `max_results` caps how many themes come back (default 5); "
        f"`timeout` caps how many seconds the search may take (default "
        f"{_DEFAULT_TIMEOUT_S}, hard max {_MAX_TIMEOUT_S}) — a harder problem may "
        "warrant a longer timeout. Best-effort: `note` reports what was searched, "
        "what was skipped or blocked, and any degradation — an empty `themes` with "
        "an explanatory `note` means the search could not be completed, not that the "
        "web has no answer."
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
                "description": "Cap on the number of themes in the report (default 5).",
                "exclusiveMinimum": 0,
            },
            "timeout": {
                "type": "number",
                "description": (
                    f"Cap on how many seconds the search may take (default "
                    f"{_DEFAULT_TIMEOUT_S}, hard max {_MAX_TIMEOUT_S})."
                ),
                "exclusiveMinimum": 0,
            },
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "themes": {
                "type": "array",
                "description": (
                    "Themed groups of findings, each a distinct angle on the query — "
                    "ideally an independent option/approach. Empty when the search "
                    "could not complete (see note)."
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
                                "What the sources say about this theme — the core "
                                "idea, perspective, or solution variant, synthesized "
                                "across sources."
                            ),
                        },
                        "links": {
                            "type": "array",
                            "description": "Source URLs this theme was generated from.",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["summary", "details", "links"],
                },
            },
            "note": {
                "type": "string",
                "description": (
                    "Search status: engines queried/skipped/blocked, pages read, and "
                    "any degradation (e.g. running out of time)."
                ),
            },
        },
        "required": ["themes", "note"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "query": "always",
        "max_results": "visible",
        "timeout": "visible",
    },
    output_visibility={
        "themes": "visible",
        "note": "always",
    },
    when_to_use=(
        "Looking up external information a codebase can't answer on its own — "
        "third-party library/API documentation, the meaning of an error message, "
        "or a known solution to a general programming problem.",
        "Surveying the option space of a problem: the themed report groups the "
        "web's answers into distinct approaches to choose between.",
    ),
)
