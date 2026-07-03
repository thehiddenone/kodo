"""Phase 2 — scrape the discovered pages down to their main text content.

Pages are fetched concurrently (bounded by a semaphore) in one shared browser
context. Extraction happens *in the page*: script/style/nav/header/footer and
other UI chrome elements are removed from the live DOM, then the ``innerText``
of the best content root (``<article>`` → ``<main>`` → ``[role=main]`` →
``<body>``) is taken — so hidden elements are excluded and line structure is
preserved by the browser's own layout engine. The Python side only normalizes
whitespace, enforces the per-block character budget, and drops pages whose
residual text is too thin to be worth summarizing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from playwright.async_api import Browser, BrowserContext
from playwright.async_api import Error as PlaywrightError

from ._models import PageText, ScrapeOutcome, SearchHit

__all__ = ["MAX_BLOCKS", "scrape_pages"]

_log = logging.getLogger(__name__)

# Cap on the number of text blocks handed to the summarizer.
MAX_BLOCKS = 15
# Parallel page fetches.
_CONCURRENCY = 5
# Navigation budget per page.
_NAV_TIMEOUT_MS = 20_000
# Per-block character budget (keeps the summarizer prompt bounded).
_MAX_BLOCK_CHARS = 6_000
# Pages with less residual text than this carry no summarizable content.
_MIN_BLOCK_CHARS = 200

# Strips UI and navigation chrome from the live DOM, then extracts the main
# content root's rendered text. Mutating the live DOM (rather than a detached
# clone) keeps innerText's layout-aware semantics — hidden elements stay
# excluded and block elements produce line breaks; the page is closed right
# after, so the mutation is harmless.
_EXTRACT_TEXT_JS = """
() => {
  const CHROME = [
    'script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe',
    'nav', 'header', 'footer', 'aside', 'form', 'button', 'select', 'dialog',
    '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
    '[role="complementary"]', '[role="search"]', '[aria-hidden="true"]',
  ];
  for (const sel of CHROME) {
    for (const el of document.querySelectorAll(sel)) el.remove();
  }
  const root =
    document.querySelector('article') ||
    document.querySelector('main') ||
    document.querySelector('[role="main"]') ||
    document.body;
  return {
    title: (document.title || '').trim(),
    text: root ? root.innerText : '',
  };
}
"""


async def scrape_pages(browser: Browser, hits: Sequence[SearchHit]) -> ScrapeOutcome:
    """Scrape *hits* into up to :data:`MAX_BLOCKS` text blocks.

    Args:
        browser: The session's shared headless browser.
        hits: Discovered pages in priority order; the returned blocks keep
            that order, so when more than :data:`MAX_BLOCKS` pages succeed the
            top-priority ones win.

    Returns:
        ScrapeOutcome: The extracted blocks plus a per-URL failure map.
    """
    outcome = ScrapeOutcome()
    if not hits:
        return outcome

    context = await browser.new_context(locale="en-US")
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    try:
        results = await asyncio.gather(
            *(_scrape_one(context, semaphore, hit) for hit in hits),
            return_exceptions=True,
        )
    finally:
        try:
            await context.close()
        except PlaywrightError:
            _log.debug("Scrape context close failed", exc_info=True)

    for hit, result in zip(hits, results, strict=True):
        if isinstance(result, BaseException):
            _log.debug("web_search: scrape of %s failed: %s", hit.url, result)
            outcome.failed[hit.url] = str(result)
        elif result is None:
            outcome.failed[hit.url] = "page yielded too little text content"
        elif len(outcome.pages) < MAX_BLOCKS:
            outcome.pages.append(result)
    return outcome


async def _scrape_one(
    context: BrowserContext, semaphore: asyncio.Semaphore, hit: SearchHit
) -> PageText | None:
    """Fetch one page and extract its main text; ``None`` when too thin."""
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(hit.url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            raw = await page.evaluate(_EXTRACT_TEXT_JS)
        finally:
            try:
                await page.close()
            except PlaywrightError:
                _log.debug("Page close failed for %s", hit.url, exc_info=True)

    title, text = _parse_extraction(raw)
    text = _normalize_text(text)
    if len(text) < _MIN_BLOCK_CHARS:
        return None
    return PageText(url=hit.url, title=title or hit.title, text=text[:_MAX_BLOCK_CHARS])


def _parse_extraction(raw: object) -> tuple[str, str]:
    """Pull ``(title, text)`` out of the extractor's payload, defensively."""
    if not isinstance(raw, dict):
        return "", ""
    title = raw.get("title")
    text = raw.get("text")
    return (
        title if isinstance(title, str) else "",
        text if isinstance(text, str) else "",
    )


def _normalize_text(text: str) -> str:
    """Collapse intra-line whitespace and runs of blank lines."""
    lines: list[str] = []
    blank = False
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
            blank = False
        elif not blank and lines:
            lines.append("")
            blank = True
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
