"""``query_search_engine`` tool spec — query one search engine for the web_search agent.

Backed by ``kodo.websearch`` (doc/WEB_SEARCH.md): queries one of four engines
(Google/Bing/DuckDuckGo/English Wikipedia) and returns its organic result
links, ads/sponsored results skipped. This is the ``web_search`` agent's
discovery primitive — one engine per call, so the agent decides which engine
to query, when, and how to pace itself (via the ``get_web_search_state``/
``update_web_search_state``/``wait`` tools), replacing the old deterministic
all-four-engines-in-parallel discovery phase.

Shares its ``browser`` backend choices with ``read_webpage`` (same
``kodo.websearch`` fetch machinery), including the ``curl_cffi`` TLS-
impersonation path.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["QUERY_SEARCH_ENGINE"]

_ENGINE_ENUM = ["google", "bing", "duckduckgo", "wikipedia"]
_BROWSER_ENUM = ["firefox", "chrome", "edge", "webkit", "chromium", "curl"]

QUERY_SEARCH_ENGINE: ToolSpec = ToolSpec(
    name="query_search_engine",
    external_name="Query Search Engine",
    user_description="Query one search engine",
    description=(
        "Query one search engine — `google`, `bing`, `duckduckgo`, or `wikipedia` "
        "(English full-text search) — and get back its organic result links "
        "(sponsored/ad results skipped). Returns `hits`: a list of `{url, title, "
        "snippet}`, in on-page rank order; an empty list is a legitimate 'no results' "
        "outcome. A wall/block (captcha, rate-limit) is reported as `error` instead — "
        "distinct from empty hits, since a wall means 'try again later or a different "
        "engine/browser', not 'nothing found'. `browser` picks the fetch backend, same "
        "choices as `read_webpage` (default `firefox`; `curl` impersonates a browser's "
        "TLS fingerprint with no browser process, often the fastest and least-detected "
        "choice for `bing`/`duckduckgo`/`wikipedia`, which are static HTML)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "engine": {
                "type": "string",
                "enum": _ENGINE_ENUM,
                "description": "Which search engine to query.",
            },
            "query": {
                "type": "string",
                "description": "Free-text search query.",
            },
            "browser": {
                "type": "string",
                "enum": _BROWSER_ENUM,
                "description": (
                    "Fetch backend, same choices as `read_webpage` (default `firefox`)."
                ),
            },
            "headed": {
                "type": "boolean",
                "description": (
                    "Run a visible browser window instead of headless (default false). "
                    "Ignored when `browser` is `curl`."
                ),
            },
        },
        "required": ["engine", "query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "hits": {
                "type": "array",
                "description": "Organic results in rank order; empty means no results.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                    "required": ["url", "title", "snippet"],
                },
            },
        },
        "required": ["hits"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "engine": "always",
        "query": "always",
        "browser": "visible",
        "headed": "visible",
    },
    output_visibility={
        "hits": "visible",
    },
    when_to_use=(
        "Discovering candidate pages for a topic from a specific engine, as part of a "
        "broader web-research loop that then reads the promising links in full.",
    ),
)
