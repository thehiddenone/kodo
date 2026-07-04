"""``web_search`` tool spec — themed web research for the Investigator.

Backed by the three-phase pipeline in doc/WEB_SEARCH.md: **discovery** (query
Google/Bing/DuckDuckGo/English-Wikipedia in parallel via headless Chromium,
collect ≤ 16 organic links, ads skipped), **scraping** (extract ≤ 16 blocks of
main text content, UI/navigation chrome stripped), and **summarization** (the silent
``web_summarizer`` sub-agent groups the findings into themes). The output is a
themed report: each theme is one distinct angle — ideally an independent way to
solve the problem — with a one-sentence ``summary``, a synthesized ``details``
text, and the source ``links`` it was drawn from.

Best-effort by design: no anti-bot evasion is attempted, and an engine that
serves a captcha is skipped for the next 30 minutes (state under
``~/.kodo/websearch/``). Whatever degrades — a cooling-down engine, unreachable
pages, a failed summarization — is reported in ``note`` rather than raised.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["WEB_SEARCH"]


WEB_SEARCH: ToolSpec = ToolSpec(
    name="web_search",
    external_name="Web Search",
    user_description="Search the web",
    description=(
        "Search the public web and get back a *themed report* rather than raw hits. "
        "The tool queries Google, Bing, DuckDuckGo, and English Wikipedia in "
        "parallel, collects up to "
        "16 organic result links (ads ignored, top results prioritized), scrapes the "
        "main text content of those pages, and distills everything into `themes`: "
        "each theme has a one-sentence `summary`, a `details` text carrying the core "
        "idea (a perspective on the problem, a variant of a solution, ...), and the "
        "source `links` it was drawn from. Where possible, themes are independent "
        "alternatives — several distinct options to choose from, not one narrative. "
        "`query` is a free-text search string; `max_results` caps how many themes "
        "come back (default 5). The pipeline is best-effort: engines that hit "
        "anti-bot walls are skipped for 30 minutes, unreachable pages are dropped, "
        "and `note` reports what was searched, what was skipped, and any degradation "
        "— an empty `themes` with an explanatory `note` means the search could not "
        "be completed, not that the web has no answer."
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
                    "ideally an independent option/approach. Empty when the pipeline "
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
                    "Pipeline status: engines queried/on cooldown, links found, pages "
                    "scraped, and any degradation (e.g. summarization failure)."
                ),
            },
        },
        "required": ["themes", "note"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={
        "query": "always",
        "max_results": "visible",
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
