"""Tests for ``kodo.websearch._enginequery`` -- browser-backed search queries.

Covers:
* :func:`_parse_hits` -- the pure validation/dedup/filter logic.
* :func:`query_via_browser` -- happy path with mocked Playwright browser/page.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.websearch._enginequery import _MAX_HITS, _parse_hits, query_via_browser

# ---------------------------------------------------------------------------
# _parse_hits -- pure
# ---------------------------------------------------------------------------


def test_parse_hits_empty_list() -> None:
    assert _parse_hits([]) == []


def test_parse_hits_non_list_returns_empty() -> None:
    assert _parse_hits("not a list") == []
    assert _parse_hits(42) == []
    assert _parse_hits(None) == []
    assert _parse_hits({"url": "x"}) == []


def test_parse_hits_valid_entries() -> None:
    raw = [
        {"url": "https://example.com/1", "title": "First", "snippet": "snip1"},
        {"url": "https://example.com/2", "title": "Second", "snippet": "snip2"},
    ]
    result = _parse_hits(raw)
    assert len(result) == 2
    assert result[0]["url"] == "https://example.com/1"
    assert result[0]["title"] == "First"
    assert result[0]["snippet"] == "snip1"
    assert result[1]["url"] == "https://example.com/2"
    assert result[1]["title"] == "Second"


def test_parse_hits_missing_url_or_title_skipped() -> None:
    raw = [
        {"url": "", "title": "No URL"},
        {"url": "https://x.com", "title": ""},
        {"title": "No URL key"},
        {"url": "https://x.com/ok", "title": "OK"},
    ]
    result = _parse_hits(raw)
    assert len(result) == 1
    assert result[0]["url"] == "https://x.com/ok"


def test_parse_hits_deduplication() -> None:
    raw = [
        {"url": "https://example.com/1", "title": "First"},
        {"url": "https://example.com/1", "title": "Duplicate"},
        {"url": "https://example.com/2", "title": "Second"},
    ]
    result = _parse_hits(raw)
    assert len(result) == 2
    assert result[0]["title"] == "First"
    assert result[1]["title"] == "Second"


def test_parse_hits_skips_engine_internal_urls() -> None:
    """URLs that look like engine internals (google.com, bing.com, etc.) are skipped."""

    raw = [
        {"url": "https://www.google.com/search?q=test", "title": "Google internal"},
        {"url": "https://example.com/real", "title": "Real hit"},
        {"url": "https://www.bing.com/search?q=test", "title": "Bing internal"},
    ]
    result = _parse_hits(raw)
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/real"


def test_parse_hits_missing_snippet_defaults_to_empty() -> None:
    raw = [{"url": "https://example.com", "title": "Test"}]
    result = _parse_hits(raw)
    assert result[0]["snippet"] == ""


def test_parse_hits_non_string_snippet_defaults_to_empty() -> None:
    raw = [{"url": "https://example.com", "title": "Test", "snippet": 42}]
    result = _parse_hits(raw)
    assert result[0]["snippet"] == ""


def test_parse_hits_non_dict_entries_skipped() -> None:
    raw = [
        "just a string",
        42,
        None,
        {"url": "https://example.com", "title": "Real"},
    ]
    result = _parse_hits(raw)
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com"


def test_parse_hits_max_hits_cap() -> None:
    """Capped at _MAX_HITS entries."""
    raw = [{"url": f"https://example.com/{i}", "title": f"Title {i}"} for i in range(30)]
    result = _parse_hits(raw)
    assert len(result) == _MAX_HITS


def test_parse_hits_mixed_valid_invalid() -> None:
    raw = [
        {"url": "https://valid.com", "title": "Valid"},
        {"url": None, "title": "No URL"},  # url is not a string
        {"url": "https://also-valid.com", "title": "Also valid"},
        {"url": 123, "title": "Bad URL type"},  # url is not a string
    ]
    result = _parse_hits(raw)
    assert len(result) == 2
    assert result[0]["url"] == "https://valid.com"
    assert result[1]["url"] == "https://also-valid.com"


# ---------------------------------------------------------------------------
# query_via_browser -- mocked Playwright
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_browser() -> tuple[MagicMock, MagicMock]:
    """A fake Browser that returns a fake Context with a fake Page."""
    browser = MagicMock()
    context = MagicMock()
    page = MagicMock()

    browser.new_context = AsyncMock(return_value=context)
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    page.close = AsyncMock()
    page.wait_for_selector = AsyncMock()

    return browser, context, page


def _make_engine(
    name: str = "google", url_prefix: str = "https://www.google.com/search?q="
) -> MagicMock:
    """Build a mock SearchEngine with all required attributes."""
    engine = MagicMock()
    engine.name = name
    engine.search_url = MagicMock(return_value=f"{url_prefix}test+query")
    engine.blocked_js = "() => false"
    engine.ready_selector = ".result"
    engine.extract_js = "() => [{url: 'https://example.com', title: 'Test', snippet: 'desc'}]"
    return engine


@pytest.mark.asyncio
async def test_query_via_browser_returns_parsed_hits(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """Happy path: the engine returns hits and query_via_browser parses them."""
    browser, context, page = _mock_browser
    engine = _make_engine()

    # Mock page.goto to return a response with status 200.
    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)

    # The engine calls evaluate 3 times: blocked_js, extract_js.
    # blocked_js is "() => false" and extract_js contains "[url:"
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            [{"url": "https://example.com/1", "title": "Hit 1", "snippet": "desc1"}],
        ]
    )

    result = await query_via_browser(browser, engine, "test query")
    assert result is not None
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/1"
    assert result[0]["title"] == "Hit 1"
    assert result[0]["snippet"] == "desc1"


@pytest.mark.asyncio
async def test_query_via_browser_returns_none_on_blocked_status(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """HTTP 403/429/503 means blocked -- returns None."""
    browser, context, page = _mock_browser
    engine = _make_engine()

    response = MagicMock()
    response.status = 429
    page.goto = AsyncMock(return_value=response)

    result = await query_via_browser(browser, engine, "test query")
    assert result is None


@pytest.mark.asyncio
async def test_query_via_browser_returns_none_on_captcha_wall(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """blocked_js returning True means captcha -- returns None."""
    browser, context, page = _mock_browser
    engine = _make_engine()

    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(return_value=True)  # blocked_js returns True

    result = await query_via_browser(browser, engine, "test query")
    assert result is None


@pytest.mark.asyncio
async def test_query_via_browser_handles_timeout_then_rechecks_blocked(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """After a wait_for_selector timeout, if blocked_js is True, return None."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    browser, context, page = _mock_browser
    engine = _make_engine()

    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)
    page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))

    # 1st evaluate: blocked_js -> True (captcha wall)
    # 2nd evaluate: extract_js -> [] (never reached, already returned None)
    evaluate_count = 0

    async def _evaluate(js: str) -> Any:
        nonlocal evaluate_count
        evaluate_count += 1
        if evaluate_count == 1:
            # First call is blocked_js check; after timeout we re-check.
            return True
        return []

    page.evaluate = _evaluate

    result = await query_via_browser(browser, engine, "test query")
    assert result is None


@pytest.mark.asyncio
async def test_query_via_browser_handles_none_response_gracefully(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """If goto returns None (no response), proceed to extraction."""
    browser, context, page = _mock_browser
    engine = _make_engine()

    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock()

    evaluate_count = 0

    async def _evaluate(js: str) -> Any:
        nonlocal evaluate_count
        evaluate_count += 1
        if evaluate_count == 1:
            return False  # blocked_js
        return [{"url": "https://example.com", "title": "Result"}]  # extract_js

    page.evaluate = _evaluate

    result = await query_via_browser(browser, engine, "test query")
    assert result is not None
    assert len(result) == 1


@pytest.mark.asyncio
async def test_query_via_browser_closes_page_and_context_on_error(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """Even if page close fails, context is still closed."""
    from playwright.async_api import Error as PlaywrightError

    browser, context, page = _mock_browser
    engine = _make_engine()

    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(
        side_effect=[
            False,
            None,
            [{"url": "https://example.com", "title": "Result"}],
        ]
    )
    page.close = AsyncMock(side_effect=PlaywrightError("close failed"))
    context.close = AsyncMock()

    result = await query_via_browser(browser, engine, "test query")
    assert result is not None
    page.close.assert_called_once()
    context.close.assert_called_once()


@pytest.mark.asyncio
async def test_query_via_browser_closes_context_on_exception(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """If an exception occurs, context is always closed."""

    browser, context, page = _mock_browser
    engine = _make_engine()

    page.goto = AsyncMock(side_effect=RuntimeError("network error"))
    page.close = AsyncMock()
    context.close = AsyncMock()

    with pytest.raises(RuntimeError, match="network error"):
        await query_via_browser(browser, engine, "test query")

    context.close.assert_called_once()
