"""Value objects shared across the web-search pipeline.

Plain frozen dataclasses passed between the discovery phase (search-engine
querying), the scraping phase, and the ``web_search`` tool that orchestrates
them. This module has no behaviour and no dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MAX_SOURCES",
    "DiscoveryOutcome",
    "PageMarkdown",
    "PageText",
    "ScrapeOutcome",
    "SearchHit",
]

# One shared cap on the sources flowing through the pipeline: discovery merges
# at most this many links (4 engines × 4), and scraping hands at most this many
# text blocks to the summarizer — so every discovered link can become a block.
MAX_SOURCES = 16


@dataclass(frozen=True)
class SearchHit:
    """One organic search result from one engine.

    Attributes:
        engine: Engine that produced the hit (``"google"`` / ``"bing"`` /
            ``"duckduckgo"`` / ``"wikipedia"``).
        rank: 1-based position within that engine's organic results (ads are
            never counted).
        url: Absolute result URL (redirect wrappers already unwrapped).
        title: Result title as shown on the results page.
        snippet: Short excerpt the engine showed under the title ("" if none).
    """

    engine: str
    rank: int
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class PageText:
    """The extracted main text of one scraped page.

    Attributes:
        url: The page URL (as requested, pre-redirect).
        title: The page's ``document.title`` (falls back to the search-result
            title when the page provides none).
        text: Main textual content with UI/navigation chrome stripped,
            whitespace-normalized and truncated to the per-block budget.
    """

    url: str
    title: str
    text: str


@dataclass(frozen=True)
class PageMarkdown:
    """The extracted main content of one page, converted to Markdown.

    Produced by :func:`~kodo.websearch.read_page` for the ``read_webpage``
    tool — distinct from :class:`PageText` (plain text for the ``web_search``
    summarizer): headings, tables, plain lists, and links are preserved as
    Markdown syntax; images and video are dropped.

    Attributes:
        url: The page URL (as requested, pre-redirect).
        title: The page's ``document.title`` ("" if none).
        markdown: Main content as Markdown, chrome/ads/images/video stripped,
            truncated to the tool's per-page budget.
    """

    url: str
    title: str
    markdown: str


@dataclass
class DiscoveryOutcome:
    """Result of phase 1 — querying the engines and merging their hits.

    Attributes:
        hits: Merged, deduplicated organic hits in priority order (top ranks
            first, engines interleaved), capped at the discovery limit.
        queried: Engines that were actually queried this call.
        skipped: Engine → human-readable reason it was *not* queried (an
            active anti-bot cooldown).
        tripped: Engines that hit an anti-bot/captcha wall on this call (their
            cooldown has just been recorded).
        errors: Engine → failure reason for non-captcha failures (timeout,
            network error, layout change yielding zero results).
    """

    hits: list[SearchHit] = field(default_factory=list)
    queried: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    tripped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class ScrapeOutcome:
    """Result of phase 2 — scraping the discovered pages.

    Attributes:
        pages: Extracted text blocks in the same priority order as the input
            hits, capped at the block limit.
        failed: URL → reason for every page that produced no usable block
            (navigation error, timeout, or too little text after stripping).
    """

    pages: list[PageText] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
