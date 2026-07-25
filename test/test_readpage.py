"""Tests for ``kodo.websearch._readpage`` -- browser-backed page fetching.

Covers the pure functions:
* :func:`_parse_extraction` -- pulling (title, markdown) from extractor payload.
* :func:`_normalize_markdown` -- trailing-whitespace trim, blank-line collapse.

Plus the end of :func:`fetch_via_browser`'s "text" path with mocked Playwright.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kodo.websearch._readpage import (
    BrowserContent,
    _normalize_markdown,
    _parse_extraction,
    fetch_via_browser,
)

# ---------------------------------------------------------------------------
# _parse_extraction -- pure
# ---------------------------------------------------------------------------


def test_parse_extraction_valid_dict() -> None:
    raw = {"title": "Page Title", "markdown": "Some **markdown** here."}
    title, markdown = _parse_extraction(raw)
    assert title == "Page Title"
    assert markdown == "Some **markdown** here."


def test_parse_extraction_missing_keys() -> None:
    title, markdown = _parse_extraction({})
    assert title == ""
    assert markdown == ""


def test_parse_extraction_non_string_values() -> None:
    title, markdown = _parse_extraction({"title": 42, "markdown": None})
    assert title == ""
    assert markdown == ""


def test_parse_extraction_non_dict_returns_empty() -> None:
    title, markdown = _parse_extraction("just a string")
    assert title == "" and markdown == ""
    title, markdown = _parse_extraction(42)
    assert title == "" and markdown == ""
    title, markdown = _parse_extraction(None)
    assert title == "" and markdown == ""


def test_parse_extraction_partial_data() -> None:
    title, markdown = _parse_extraction({"title": "Only title"})
    assert title == "Only title"
    assert markdown == ""

    title, markdown = _parse_extraction({"markdown": "Only markdown"})
    assert title == ""
    assert markdown == "Only markdown"


# ---------------------------------------------------------------------------
# _normalize_markdown -- pure
# ---------------------------------------------------------------------------


def test_normalize_markdown_trailing_whitespace() -> None:
    """Trailing whitespace is stripped from each line."""
    assert _normalize_markdown("hello   \nworld  \n") == "hello\nworld"


def test_normalize_markdown_collapses_long_blank_runs() -> None:
    """Runs of 3+ blank lines collapse to a single blank line."""
    text = "line1\n\n\n\n\nline2"
    assert _normalize_markdown(text) == "line1\n\nline2"


def test_normalize_markdown_keeps_double_blank_lines() -> None:
    """Two consecutive blank lines are preserved (single blank separator)."""
    text = "line1\n\n\nline2"
    assert _normalize_markdown(text) == "line1\n\nline2"


def test_normalize_markdown_strips_trailing_blank_lines() -> None:
    """Trailing blank lines are removed."""
    text = "line1\nline2\n\n\n"
    assert _normalize_markdown(text) == "line1\nline2"


def test_normalize_markdown_empty_input() -> None:
    assert _normalize_markdown("") == ""


def test_normalize_markdown_no_trailing_whitespace() -> None:
    text = "no trailing spaces here"
    assert _normalize_markdown(text) == "no trailing spaces here"


def test_normalize_markdown_mixed_content() -> None:
    text = "heading\n\n\npara with   trailing   spaces   \n\n\n\nmore text\n"
    # trailing whitespace stripped, 3+ blank runs collapsed to 1, trailing blanks removed
    result = _normalize_markdown(text)
    # heading followed by 1 blank, then the para (with internal trailing spaces kept),
    # then 1 blank, then more text.
    assert result.startswith("heading\n\n")
    assert "para with   trailing   spaces" in result
    assert result.endswith("more text")


def test_normalize_markdown_single_line() -> None:
    assert _normalize_markdown("just one line") == "just one line"
    assert _normalize_markdown("just one line   ") == "just one line"


# ---------------------------------------------------------------------------
# fetch_via_browser -- mocked Playwright
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_browser() -> tuple[MagicMock, MagicMock, MagicMock]:
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


def _make_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    return resp


def _make_engine_extract_text_js(raw: Any) -> str:
    """Return a JS string that the evaluate mock can identify as extract_text_js."""
    return f"() => {raw}"


@pytest.mark.asyncio
async def test_fetch_via_browser_text_mode_returns_browser_content(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """Happy path: content_filter='text' returns BrowserContent(title, content)."""
    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            {"title": "Page Title", "markdown": "Some **markdown**."},
        ]
    )

    content = await fetch_via_browser(browser, "https://example.com", "text")
    assert isinstance(content, BrowserContent)
    assert content.title == "Page Title"
    assert "markdown" in content.content
    assert "Page Title" not in content.content  # title not in content for text mode


@pytest.mark.asyncio
async def test_fetch_via_browser_off_mode_returns_raw_html(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """content_filter='off' returns raw HTML, no title."""
    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            "<html><body>raw html</body></html>",
        ]
    )

    content = await fetch_via_browser(browser, "https://example.com", "off")
    assert content.title == ""
    assert content.content == "<html><body>raw html</body></html>"


@pytest.mark.asyncio
async def test_fetch_via_browser_html_mode_strips_scripts(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """content_filter='html' returns HTML with scripts removed."""
    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    # The _EXTRACT_HTML_JS removes <script>, <style>, <noscript> before returning
    # outerHTML. Simulate the post-extraction result.
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            "<html><head></head><body>content</body></html>",
        ]
    )

    content = await fetch_via_browser(browser, "https://example.com", "html")
    assert content.title == ""
    assert "<script>" not in content.content
    assert "content" in content.content


@pytest.mark.asyncio
async def test_fetch_via_browser_blocked_status_returns_anti_bot(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """HTTP 429 raises AntiBotWallError."""
    from kodo.websearch._validate import AntiBotWallError

    browser, context, page = _mock_browser

    response = _make_response(429)
    page.goto = AsyncMock(return_value=response)

    with pytest.raises(AntiBotWallError):
        await fetch_via_browser(browser, "https://example.com", "text")


@pytest.mark.asyncio
async def test_fetch_via_browser_captcha_wall_raises_anti_bot(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """blocked_js returning True raises AntiBotWallError."""
    from kodo.websearch._validate import AntiBotWallError

    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(return_value=True)

    with pytest.raises(AntiBotWallError):
        await fetch_via_browser(browser, "https://example.com", "text")


@pytest.mark.asyncio
async def test_fetch_via_browser_context_closed_on_exception(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """If an exception occurs, context is always closed."""

    browser, context, page = _mock_browser

    page.goto = AsyncMock(side_effect=RuntimeError("network error"))
    context.close = AsyncMock()

    with pytest.raises(RuntimeError, match="network error"):
        await fetch_via_browser(browser, "https://example.com", "text")

    context.close.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_via_browser_text_mode_normalizes_markdown(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """The markdown content is normalized (trailing whitespace stripped, blank lines collapsed)."""
    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            {"title": "Title", "markdown": "line1   \n\n\n\nline2\n"},
        ]
    )

    content = await fetch_via_browser(browser, "https://example.com", "text")
    # _normalize_markdown should have stripped trailing spaces and collapsed blanks.
    assert "   " not in content.content  # no trailing whitespace
    assert "\n\n\n" not in content.content  # no 3+ blank runs


@pytest.mark.asyncio
async def test_fetch_via_browser_page_close_failure_handled(
    _mock_browser: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    """If page.close raises PlaywrightError, it's logged but not propagated."""
    from playwright.async_api import Error as PlaywrightError

    browser, context, page = _mock_browser

    response = _make_response(200)
    page.goto = AsyncMock(return_value=response)
    page.evaluate = AsyncMock(
        side_effect=[
            False,  # blocked_js
            {"title": "T", "markdown": "M"},
        ]
    )
    page.close = AsyncMock(side_effect=PlaywrightError("close failed"))

    # Should not raise.
    content = await fetch_via_browser(browser, "https://example.com", "text")
    assert content.title == "T"
