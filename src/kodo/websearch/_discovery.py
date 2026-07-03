"""Phase 1 — query the engines in parallel and merge their organic hits.

Every engine not on an anti-bot cooldown is queried concurrently (one isolated
browser context each). A blocked engine trips its 30-minute cooldown in the
:class:`~kodo.websearch.CooldownStore`; a failed one is recorded as an error;
the survivors' hits are merged rank-by-rank (all the #1 hits first, then the
#2s, …) so the engines' top results are prioritized, deduplicated by
normalized URL, and capped at :data:`MAX_LINKS`.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ._cooldown import CooldownStore
from ._engines import ENGINES, Engine
from ._models import DiscoveryOutcome, SearchHit

__all__ = ["MAX_LINKS", "discover", "merge_hits"]

_log = logging.getLogger(__name__)

# Cap on the merged link list handed to the scraping phase.
MAX_LINKS = 15
# Cap on hits taken from any single engine (before merging).
_PER_ENGINE_CAP = 10
# Navigation budget for one results page.
_NAV_TIMEOUT_MS = 20_000
# Post-navigation budget for the organic results to appear in the DOM (some
# engines hydrate them after domcontentloaded).
_READY_TIMEOUT_MS = 8_000
# HTTP statuses that mean "you are rate-limited / blocked" without a captcha page.
_BLOCKED_STATUSES = frozenset({403, 429, 503})
# Hosts that are engine-internal (support pages, image/maps verticals, the DDG
# redirector) — never useful as scrape targets.
_ENGINE_HOST_MARKERS = ("google.", "bing.com", "duckduckgo.com", "microsoft.com/en-us/bing")


async def discover(browser: Browser, query: str, cooldowns: CooldownStore) -> DiscoveryOutcome:
    """Run the discovery phase for *query* and return the merged outcome.

    Args:
        browser: The session's shared headless browser.
        query: Free-text search query.
        cooldowns: Persistent per-engine anti-bot cooldown state; engines with
            time remaining are skipped, and engines that serve a wall during
            this call are tripped.

    Returns:
        DiscoveryOutcome: Merged hits (≤ :data:`MAX_LINKS`) plus the per-engine
        bookkeeping the tool folds into its ``note``.
    """
    outcome = DiscoveryOutcome()
    active: list[Engine] = []
    for engine in ENGINES:
        remaining = cooldowns.remaining(engine.name)
        if remaining > 0:
            minutes = max(1, round(remaining / 60))
            outcome.skipped[engine.name] = f"anti-bot cooldown, ~{minutes}m left"
        else:
            active.append(engine)

    results = await asyncio.gather(
        *(_query_engine(browser, engine, query) for engine in active),
        return_exceptions=True,
    )

    per_engine: list[list[SearchHit]] = []
    for engine, result in zip(active, results, strict=True):
        outcome.queried.append(engine.name)
        if isinstance(result, BaseException):
            _log.warning("web_search: %s query failed: %s", engine.name, result)
            outcome.errors[engine.name] = str(result)
            continue
        status, hits = result
        if status == "blocked":
            cooldowns.trip(engine.name)
            outcome.tripped.append(engine.name)
        elif not hits:
            outcome.errors[engine.name] = "no organic results extracted"
        else:
            per_engine.append(hits)

    outcome.hits = merge_hits(per_engine, MAX_LINKS)
    return outcome


def merge_hits(per_engine: list[list[SearchHit]], max_links: int) -> list[SearchHit]:
    """Interleave per-engine hit lists rank-by-rank, dedupe, and cap.

    Taking all the rank-1 hits (in engine order), then the rank-2s, …
    prioritizes every engine's top results over any engine's tail. URLs are
    deduplicated on a normalized form so the same page found by two engines
    appears once (first engine wins).
    """
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for tier in range(max(map(len, per_engine), default=0)):
        for hits in per_engine:
            if tier >= len(hits):
                continue
            hit = hits[tier]
            key = _normalize_url(hit.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
            if len(merged) >= max_links:
                return merged
    return merged


def _normalize_url(url: str) -> str:
    """Canonical dedupe key: lowercase scheme/host, no fragment, no trailing /."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _is_engine_internal(url: str) -> bool:
    """True for links pointing back into a search engine's own properties."""
    host = urlsplit(url).netloc.lower()
    return any(marker in host for marker in _ENGINE_HOST_MARKERS)


async def _query_engine(
    browser: Browser, engine: Engine, query: str
) -> tuple[str, list[SearchHit]]:
    """Query one engine; return ``("ok", hits)`` or ``("blocked", [])``.

    Non-captcha failures (navigation errors, timeouts) propagate as
    :class:`playwright.async_api.Error` / :class:`TimeoutError` and are
    collected by :func:`discover` as engine errors.
    """
    context = await browser.new_context(locale="en-US")
    try:
        page = await context.new_page()
        response = await page.goto(
            engine.search_url(query), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
        )
        if response is not None and response.status in _BLOCKED_STATUSES:
            return "blocked", []
        if bool(await page.evaluate(engine.blocked_js)):
            return "blocked", []
        # Results may hydrate after domcontentloaded; wait for them briefly. A
        # timeout is not fatal — extraction still runs on whatever is there.
        try:
            await page.wait_for_selector(
                engine.ready_selector, state="attached", timeout=_READY_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            # No results appeared: either a layout change (empty extraction →
            # engine error) or a late-rendered wall — re-check the latter.
            if bool(await page.evaluate(engine.blocked_js)):
                return "blocked", []
        raw = await page.evaluate(engine.extract_js)
        return "ok", _parse_hits(engine.name, raw)
    finally:
        try:
            await context.close()
        except PlaywrightError:
            _log.debug("Context close failed for %s", engine.name, exc_info=True)


def _parse_hits(engine_name: str, raw: object) -> list[SearchHit]:
    """Validate the extractor's ``[{url, title, snippet}]`` payload into hits."""
    hits: list[SearchHit] = []
    if not isinstance(raw, list):
        return hits
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("title")
        if not isinstance(url, str) or not url or not isinstance(title, str) or not title:
            continue
        if _is_engine_internal(url):
            continue
        snippet = entry.get("snippet")
        hits.append(
            SearchHit(
                engine=engine_name,
                rank=len(hits) + 1,
                url=url,
                title=title,
                snippet=snippet if isinstance(snippet, str) else "",
            )
        )
        if len(hits) >= _PER_ENGINE_CAP:
            break
    return hits
