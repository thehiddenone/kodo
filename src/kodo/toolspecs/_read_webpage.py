"""``read_webpage`` tool spec — fetch one URL as Markdown for the Investigator.

Backed by :func:`kodo.websearch.read_page` (doc/READ_WEBPAGE.md): fetch one
page, strip navigation/ads/scripts/images/video, and convert the remaining
main content to Markdown (headings, tables, plain non-numbered lists, and
embedded links preserved).

Best-effort by design, like ``web_search``: no anti-bot evasion is attempted.
Unlike ``web_search`` there is no cooldown — a walled page simply returns an
``error`` explaining what happened and advising against retrying the same URL.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_WEBPAGE"]


READ_WEBPAGE: ToolSpec = ToolSpec(
    name="read_webpage",
    external_name="Read Webpage",
    user_description="Read a web page",
    description=(
        "Fetch one web page by URL and return its main content as Markdown. "
        "Navigation, ads/banners, scripts, and images/video are stripped; headings, "
        "tables, simple (non-numbered) lists, and embedded links `[text](url)` are "
        "preserved. Use this when you already have a specific URL and need its actual "
        "content, rather than `web_search`'s cross-source themed summary. Best-effort "
        "and non-evasive: if the page is behind an anti-bot/captcha wall, blocked "
        "outright, or yields no readable content, the call returns an `error` "
        "explaining what happened. There is no cooldown like `web_search` has for "
        "engines — do not keep retrying the same URL; a retry will fail the same way."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL of the page to read.",
            },
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "markdown": {
                "type": "string",
                "description": "The page's main content converted to Markdown.",
            },
        },
        "required": ["markdown"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "url": "always",
    },
    output_visibility={
        "markdown": "visible",
    },
    when_to_use=(
        "You already have a specific URL — from web_search results, documentation "
        "links, or the user — and need its actual content rather than a search-engine "
        "snippet or a cross-source summary.",
        "Reading one known page in full, e.g. an API reference page, a changelog, or a "
        "README, rather than surveying multiple sources.",
    ),
)
