"""Playwright-backed web search: discovery + scraping for the ``web_search`` tool.

A **T0 leaf package** (imports nothing from ``kodo``) implementing the first
two phases of the ``web_search`` pipeline (doc/WEB_SEARCH.md):

1. **Discovery** (:func:`discover`) — query Google, Bing, and DuckDuckGo
   (HTML endpoint) in parallel through one headless Chromium, skip sponsored
   results, and merge the organic hits rank-by-rank into ≤ 15 prioritized,
   deduplicated links.
2. **Scraping** (:func:`scrape_pages`) — fetch the discovered pages
   concurrently, strip UI/navigation chrome in-page, and return ≤ 15 blocks of
   main text content.

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
from ._discovery import MAX_LINKS, discover, merge_hits
from ._models import DiscoveryOutcome, PageText, ScrapeOutcome, SearchHit
from ._scrape import MAX_BLOCKS, scrape_pages

__all__ = [
    "COOLDOWN_SECONDS",
    "MAX_BLOCKS",
    "MAX_LINKS",
    "BrowserSession",
    "BrowserUnavailableError",
    "CooldownStore",
    "DiscoveryOutcome",
    "PageText",
    "ScrapeOutcome",
    "SearchHit",
    "discover",
    "merge_hits",
    "scrape_pages",
]
