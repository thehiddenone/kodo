"""``read_webpage`` tool spec — fetch one URL for the Investigator / web_search agent.

Backed by two backends (doc/READ_WEBPAGE.md): a Playwright browser
(:func:`kodo.websearch.fetch_via_browser`, any ``browser`` but ``"curl"``) or
``curl_cffi`` TLS/browser-signature impersonation
(:func:`kodo.websearch.curlfetch.fetch` + ``kodo.websearch.htmlextract`` for
``browser: "curl"``). ``content_filter`` controls how aggressively the page
is stripped: ``"off"`` (untouched), ``"html"`` (script/style/noscript
removed), or ``"text"`` (today's original behavior — content-root selection,
chrome stripped, DOM converted to Markdown).

Best-effort by design: no JS-fingerprint spoofing or CAPTCHA solving is
attempted. ``curl_cffi``'s TLS/browser-signature impersonation is a
deliberate, bounded exception to that stance — a network-layer technique, not
a JS-level one (see WEB_SEARCH.md's stance). Unlike ``query_search_engine``
there is no cooldown — a walled page simply returns an ``error`` explaining
what happened and advising against retrying the same URL/browser.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_WEBPAGE"]

_BROWSER_ENUM = ["firefox", "chrome", "edge", "webkit", "chromium", "curl"]
_CONTENT_FILTER_ENUM = ["off", "html", "text"]

READ_WEBPAGE: ToolSpec = ToolSpec(
    name="read_webpage",
    external_name="Read Webpage",
    user_description="Read a web page",
    description=(
        "Fetch one web page by URL and return its content. `content_filter` picks "
        "how aggressively it's cleaned: `text` (default) strips navigation/ads/"
        "scripts/images and converts the main content to Markdown (headings, tables, "
        "simple non-numbered lists, and embedded links `[text](url)` preserved); "
        "`html` returns the full page's HTML with only `<script>`/`<style>` removed; "
        "`off` returns the page completely untouched. `browser` picks the fetch "
        "backend: `firefox` (default, bundled), `chrome`/`edge` (host-installed, "
        "error if not present), `webkit`/`chromium` (bundled), or `curl` — a "
        "browserless HTTP client that impersonates a real browser's TLS/HTTP2 "
        "fingerprint (curl_cffi), often the fastest and least detectable choice for "
        "a page that doesn't require JavaScript. `headed` runs a visible browser "
        "window instead of headless (ignored for `curl`). Use this when you already "
        "have a specific URL and need its actual content, rather than "
        "`query_search_engine`'s search-result links. Best-effort: if the page is "
        "behind an anti-bot/captcha wall, blocked outright, the requested browser "
        "isn't available, or yields no readable content, the call returns an "
        "`error` explaining what happened. There is no cooldown like "
        "`query_search_engine` has for engines — do not keep retrying the same URL "
        "with the same browser; a retry will fail the same way (though a different "
        "`browser` choice may succeed where another was walled)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http(s) URL of the page to read.",
            },
            "browser": {
                "type": "string",
                "enum": _BROWSER_ENUM,
                "description": (
                    "Fetch backend. `firefox` (default) is the bundled browser least "
                    "likely to be flagged as a bot; `chrome`/`edge` use the host's own "
                    "install (error if absent, no fallback); `webkit`/`chromium` are "
                    "bundled; `curl` impersonates a browser's TLS fingerprint with no "
                    "browser process at all — fast, and passes some anti-bot checks "
                    "that a real headless browser doesn't."
                ),
            },
            "headed": {
                "type": "boolean",
                "description": (
                    "Run a visible browser window instead of headless (default false). "
                    "Ignored when `browser` is `curl`."
                ),
            },
            "content_filter": {
                "type": "string",
                "enum": _CONTENT_FILTER_ENUM,
                "description": (
                    "How much to strip. `text` (default): main content converted to "
                    "Markdown, navigation/ads/scripts/images removed. `html`: full page "
                    "HTML, only `<script>`/`<style>`/`<noscript>` removed. `off`: the "
                    "page exactly as fetched, nothing removed."
                ),
            },
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The page's content, shaped per `content_filter`: Markdown "
                    "(`text`), stripped HTML (`html`), or the raw page (`off`)."
                ),
            },
        },
        "required": ["content"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={
        "url": "always",
        "browser": "visible",
        "headed": "visible",
        "content_filter": "visible",
    },
    output_visibility={
        "content": "visible",
    },
    when_to_use=(
        "You already have a specific URL — from query_search_engine results, "
        "documentation links, or the user — and need its actual content rather than "
        "a search-engine snippet.",
        "Reading one known page in full, e.g. an API reference page, a changelog, or a "
        "README, rather than surveying multiple sources.",
    ),
)
