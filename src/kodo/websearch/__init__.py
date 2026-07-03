"""Playwright-backed web search: discovery + scraping for the ``web_search`` tool.

A **T0 leaf package** (imports nothing from ``kodo``) implementing the first
two phases of the ``web_search`` pipeline (doc/WEB_SEARCH.md):

1. **Discovery** (:func:`discover`) — query Google, Bing, DuckDuckGo (HTML
   endpoint), and English Wikipedia (full-text search) in parallel through one
   headless Chromium, skip sponsored results, and merge the organic hits
   rank-by-rank into ≤ :data:`MAX_SOURCES` (16) prioritized, deduplicated
   links.
2. **Scraping** (:func:`scrape_pages`) — fetch the discovered pages
   concurrently, strip UI/navigation chrome in-page, and return ≤
   :data:`MAX_SOURCES` blocks of main text content.

Phase 3 (theme summarization) is LLM work and lives above this package — the
tool hands the blocks to the engine's ``web_summarizer`` sub-agent.

Everything is best-effort and non-evasive: an engine that serves an anti-bot /
captcha wall is put on a 30-minute cooldown via :class:`CooldownStore`, whose
JSON state file path is supplied by the caller (the tool keeps it under
``~/.kodo/websearch/``) so this package stays layout-agnostic. The browser
binary is auto-installed on first use by :class:`BrowserSession`.
"""

from __future__ import annotations

from ._browser import BrowserSession, BrowserUnavailableError
from ._cooldown import COOLDOWN_SECONDS, CooldownStore
from ._discovery import discover, merge_hits
from ._engines import SEARCH_ENGINES, SearchEngine
from ._models import MAX_SOURCES, DiscoveryOutcome, PageText, ScrapeOutcome, SearchHit
from ._scrape import scrape_pages

__all__ = [
    "COOLDOWN_SECONDS",
    "SEARCH_ENGINES",
    "MAX_SOURCES",
    "BrowserSession",
    "BrowserUnavailableError",
    "CooldownStore",
    "DiscoveryOutcome",
    "SearchEngine",
    "PageText",
    "ScrapeOutcome",
    "SearchHit",
    "discover",
    "merge_hits",
    "scrape_pages",
]
