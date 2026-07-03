"""Playwright-backed web access: discovery/scraping for ``web_search``, and
single-page Markdown extraction for ``read_webpage``.

A **T0 leaf package** (imports nothing from ``kodo``). Two independent
pipelines share the browser lifecycle (:class:`BrowserSession`) but nothing
else, so a change to one cannot regress the other:

``web_search`` (doc/WEB_SEARCH.md), three phases:

1. **Discovery** (:func:`discover`) — query Google, Bing, DuckDuckGo (HTML
   endpoint), and English Wikipedia (full-text search) in parallel through one
   headless Chromium, skip sponsored results, and merge the organic hits
   rank-by-rank into ≤ :data:`MAX_SOURCES` (16) prioritized, deduplicated
   links.
2. **Scraping** (:func:`scrape_pages`) — fetch the discovered pages
   concurrently, strip UI/navigation chrome in-page, and return ≤
   :data:`MAX_SOURCES` blocks of main text content.
3. Theme summarization is LLM work and lives above this package — the tool
   hands the blocks to the engine's ``web_summarizer`` sub-agent.

   Best-effort and non-evasive: an engine that serves an anti-bot / captcha
   wall is put on a 30-minute cooldown via :class:`CooldownStore`, whose JSON
   state file path is supplied by the caller (the tool keeps it under
   ``~/.kodo/websearch/``) so this package stays layout-agnostic.

``read_webpage`` (doc/READ_WEBPAGE.md), one phase:

- **Reading** (:func:`read_page`) — fetch one caller-given URL and convert its
  main content root to Markdown in-page (headings/tables/plain lists/links
  preserved, images/video dropped). No cooldown: a blocked/anti-bot page just
  raises :class:`AntiBotWallError` for the caller to report. Because the URL
  comes directly from the agent rather than a search engine, :func:`read_page`
  also guards against SSRF (:class:`InvalidUrlError` for non-http(s) schemes
  or hosts resolving to a private/loopback/link-local address).

:class:`BrowserSession` prefers the host's own Chrome/Edge (far less likely to
trip anti-bot walls) and only falls back to a Playwright-managed browser
(bundled Firefox, then bundled Chromium as a last resort) when neither is
installed, auto-installing the needed one on first use. The fallback choice
is cached under ``~/.kodo/websearch/browser_state.json`` for a day before
host browsers are re-tried; a one-time ``example.com`` sanity check is cached
in the same file.
"""

from __future__ import annotations

from ._browser import BrowserSession, BrowserUnavailableError
from ._cooldown import COOLDOWN_SECONDS, CooldownStore
from ._discovery import discover, merge_hits
from ._engines import SEARCH_ENGINES, SearchEngine
from ._models import (
    MAX_SOURCES,
    DiscoveryOutcome,
    PageMarkdown,
    PageText,
    ScrapeOutcome,
    SearchHit,
)
from ._readpage import AntiBotWallError, InvalidUrlError, read_page, validate_public_url
from ._scrape import scrape_pages

__all__ = [
    "COOLDOWN_SECONDS",
    "SEARCH_ENGINES",
    "MAX_SOURCES",
    "AntiBotWallError",
    "BrowserSession",
    "BrowserUnavailableError",
    "CooldownStore",
    "DiscoveryOutcome",
    "InvalidUrlError",
    "SearchEngine",
    "PageMarkdown",
    "PageText",
    "ScrapeOutcome",
    "SearchHit",
    "discover",
    "merge_hits",
    "read_page",
    "scrape_pages",
    "validate_public_url",
]
