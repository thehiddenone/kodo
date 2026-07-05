"""Browser-backed single-engine query for ``query_search_engine`` (doc/WEB_SEARCH.md).

Companion to ``_engines_static.py`` (the ``curl`` backend's equivalent): runs
one ``_engines.py`` :class:`~kodo.websearch.SearchEngine`'s wall-detection +
extraction JS in a live Playwright page. This is what the old
all-four-engines-in-parallel discovery phase used to do internally — now
``query_search_engine`` queries exactly one engine per call, so the calling
agent decides which engine, when, and how to pace itself.
"""

from __future__ import annotations

import logging

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ._engines import SearchEngine, is_engine_internal

__all__ = ["query_via_browser"]

_log = logging.getLogger(__name__)

# Navigation budget for the results page.
_NAV_TIMEOUT_MS = 20_000
# Post-navigation budget for the organic results to appear in the DOM (some
# engines hydrate them after domcontentloaded).
_READY_TIMEOUT_MS = 8_000
# HTTP statuses that mean "you are rate-limited / blocked" without a captcha page.
_BLOCKED_STATUSES = frozenset({403, 429, 503})
# Cap on hits taken from one query (mirrors the old discovery phase's per-engine cap).
_MAX_HITS = 20


async def query_via_browser(
    browser: Browser, engine: SearchEngine, query: str
) -> list[dict[str, str]] | None:
    """Query *engine* for *query* via a live browser page.

    Returns:
        list[dict[str, str]] | None: Organic hits (``url``/``title``/
        ``snippet``, ads and engine-internal links skipped), or ``None`` when
        the engine served an anti-bot/captcha wall.

    Raises:
        playwright.async_api.Error: A non-captcha navigation failure (e.g. a
            timeout) — the caller reports this as a generic tool error.
    """
    context = await browser.new_context(locale="en-US")
    try:
        page = await context.new_page()
        try:
            response = await page.goto(
                engine.search_url(query), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
            )
            if response is not None and response.status in _BLOCKED_STATUSES:
                return None
            if bool(await page.evaluate(engine.blocked_js)):
                return None
            # Results may hydrate after domcontentloaded; wait for them
            # briefly. A timeout is not fatal — extraction still runs on
            # whatever is there, after re-checking for a late-rendered wall.
            try:
                await page.wait_for_selector(
                    engine.ready_selector, state="attached", timeout=_READY_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                if bool(await page.evaluate(engine.blocked_js)):
                    return None
            raw = await page.evaluate(engine.extract_js)
        finally:
            try:
                await page.close()
            except PlaywrightError:
                _log.debug("Page close failed for %s", engine.name, exc_info=True)
    finally:
        try:
            await context.close()
        except PlaywrightError:
            _log.debug("Context close failed for %s", engine.name, exc_info=True)
    return _parse_hits(raw)


def _parse_hits(raw: object) -> list[dict[str, str]]:
    """Validate the extractor's ``[{url, title, snippet}]`` payload into hits."""
    hits: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return hits
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("title")
        if not isinstance(url, str) or not url or not isinstance(title, str) or not title:
            continue
        if url in seen or is_engine_internal(url):
            continue
        seen.add(url)
        snippet = entry.get("snippet")
        hits.append(
            {"url": url, "title": title, "snippet": snippet if isinstance(snippet, str) else ""}
        )
        if len(hits) >= _MAX_HITS:
            break
    return hits
