"""Behavior tests for the web-facing leaf tools: ``read_webpage`` and
``query_search_engine``.

Nothing here touches the network. DNS (the SSRF guard's only I/O) is answered
by a fake ``socket.getaddrinfo``; the curl backend's single fetch function is
replaced on :mod:`kodo.websearch.curlfetch`; and the browser backend is
replaced by a session stand-in whose page content is canned. The real static
HTML extractors run on the canned pages, so the ``content_filter`` contract,
the "too thin" gate, and the length caps are exercised end to end.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from kodo.runtime import SessionState
from kodo.tools import ToolDispatcher
from kodo.websearch import (
    AntiBotWallError,
    BrowserContent,
    BrowserUnavailableError,
    curlfetch,
    engines_static,
    htmlextract,
)

# The tool modules bind these names at import time, so the browser backend can
# only be swapped where the tools look them up.
_READ_WEBPAGE_MODULE = "kodo.tools._read_webpage"
_QUERY_ENGINE_MODULE = "kodo.tools._query_search_engine"

_LONG_PARAGRAPH = (
    "Kodo reads this documentation page through the curl backend and turns "
    "the main article into Markdown for the agent to consume."
)
_PAGE = (
    "<html><head><title>Kodo Docs</title><script>var tracking = 1;</script></head>"
    f"<body><main><h1>Overview</h1><p>{_LONG_PARAGRAPH}</p></main></body></html>"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resolver:
    """Minimal ``PathResolver``; the web tools never resolve paths."""

    __root: Path

    def __init__(self, root: Path) -> None:
        """Bind to *root*.

        Args:
            root (Path): The default working directory.
        """
        self.__root = root

    def resolve(self, path: str) -> Path:
        """Resolve *path* under the root.

        Args:
            path (str): A root-relative path.

        Returns:
            Path: The resolved path.
        """
        return (self.__root / path).resolve()

    @property
    def default_cwd(self) -> Path:
        return self.__root


class _Services:
    """Structural ``EngineServices`` with no workspace bound."""

    def has_workspace(self) -> bool:
        """Report no bound workspace.

        Returns:
            bool: Always ``False``.
        """
        return False

    def root_paths(self) -> tuple[()]:
        """Return no roots.

        Returns:
            tuple[()]: Empty.
        """
        return ()


class _FakeBrowserSession:
    """Stands in for ``BrowserSession``: opens instantly, or fails to launch."""

    unavailable = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Accept and ignore the real session's arguments.

        Args:
            *args (object): Ignored.
            **kwargs (object): Ignored.
        """

    async def __aenter__(self) -> _FakeBrowserSession:
        if type(self).unavailable:
            raise BrowserUnavailableError("firefox is not installed")
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    @property
    def browser(self) -> object:
        return object()


class _UnavailableBrowserSession(_FakeBrowserSession):
    unavailable = True


def _make_dispatcher(tmp_path: Path) -> ToolDispatcher:
    return ToolDispatcher(
        resolver=_Resolver(tmp_path),
        gate=object(),  # type: ignore[arg-type]
        session=SessionState(),
        services=_Services(),  # type: ignore[arg-type]
        agent_name="kodo_investigator",
        session_id="sess-test",
    )


async def _call(d: ToolDispatcher, name: str, payload: dict[str, object]) -> dict[str, object]:
    result = json.loads(await d.dispatch(name, payload))
    assert isinstance(result, dict)
    return result


@pytest.fixture(autouse=True)
def _offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test offline and away from the real ``~/.kodo``."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def _public_dns(host: str, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    async def _no_network(url: str) -> object:
        raise AssertionError(f"unexpected real fetch of {url}")

    monkeypatch.setattr(curlfetch, "fetch", _no_network)


def _serve(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    """Make the curl backend return *html* for any URL."""

    async def _fetch(url: str) -> object:
        return curlfetch.FetchedPage(url=url, status=200, html=html)

    monkeypatch.setattr(curlfetch, "fetch", _fetch)


def _browser_serves(monkeypatch: pytest.MonkeyPatch, title: str, content: str) -> None:
    """Make the browser backend open instantly and return the given page."""

    async def _fetch_via_browser(browser: object, url: str, content_filter: str) -> BrowserContent:
        return BrowserContent(title=title, content=content)

    monkeypatch.setattr(f"{_READ_WEBPAGE_MODULE}.BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(f"{_READ_WEBPAGE_MODULE}.fetch_via_browser", _fetch_via_browser)


# ---------------------------------------------------------------------------
# read_webpage — input validation
# ---------------------------------------------------------------------------


async def test_read_webpage_rejects_unknown_content_filter(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "read_webpage", {"url": "https://example.com", "content_filter": "markdown"}
    )
    assert result == {"error": "Unsupported 'content_filter': 'markdown'."}


async def test_read_webpage_rejects_non_http_scheme(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "file:///etc/passwd", "browser": "curl"})
    assert "Unsupported URL scheme" in str(result["error"])


# ---------------------------------------------------------------------------
# read_webpage — curl backend
# ---------------------------------------------------------------------------


async def test_read_webpage_curl_text_returns_titled_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _PAGE)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs", "browser": "curl"})
    content = str(result["content"])
    assert content.startswith("# Kodo Docs\n\n")
    assert _LONG_PARAGRAPH in content
    assert "<p>" not in content


async def test_read_webpage_curl_html_strips_scripts_but_keeps_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _PAGE)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d,
        "read_webpage",
        {"url": "https://example.com/docs", "browser": "curl", "content_filter": "html"},
    )
    content = str(result["content"])
    assert "<p>" in content
    assert "tracking" not in content


async def test_read_webpage_curl_off_returns_the_page_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _PAGE)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d,
        "read_webpage",
        {"url": "https://example.com/docs", "browser": "curl", "content_filter": "off"},
    )
    assert result == {"content": _PAGE}


async def test_read_webpage_curl_wall_is_reported_with_retry_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _PAGE)
    monkeypatch.setattr(htmlextract, "is_blocked", lambda html: True)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs", "browser": "curl"})
    error = str(result["error"])
    assert "anti-bot/captcha wall" in error
    assert "Do not retry this exact URL" in error


async def test_read_webpage_thin_text_is_treated_as_a_wall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, "<html><body><main><p>Hi</p></main></body></html>")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs", "browser": "curl"})
    error = str(result["error"])
    assert "almost no readable content" in error
    assert "Do not retry this exact URL" in error


async def test_read_webpage_unexpected_failure_becomes_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(url: str) -> object:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(curlfetch, "fetch", _boom)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs", "browser": "curl"})
    assert result == {"error": "Could not read https://example.com/docs: connection reset"}


# ---------------------------------------------------------------------------
# read_webpage — browser backend
# ---------------------------------------------------------------------------


async def test_read_webpage_browser_text_prepends_title_and_caps_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _browser_serves(monkeypatch, "Huge Page", "x" * 30_000)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "read_webpage", {"url": "https://example.com/huge", "browser": "chrome"}
    )
    content = str(result["content"])
    assert content.startswith("# Huge Page\n\n")
    assert len(content) == 20_000


async def test_read_webpage_browser_text_without_title_has_no_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _browser_serves(monkeypatch, "", _LONG_PARAGRAPH)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs"})
    assert result == {"content": _LONG_PARAGRAPH}


async def test_read_webpage_browser_raw_content_is_capped_not_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _browser_serves(monkeypatch, "", "<b>" * 20_000)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "read_webpage", {"url": "https://example.com/raw", "content_filter": "off"}
    )
    assert len(str(result["content"])) == 50_000

    _browser_serves(monkeypatch, "", "<i/>")
    result = await _call(
        d, "read_webpage", {"url": "https://example.com/raw", "content_filter": "html"}
    )
    # No "too thin" gate for raw markup — a tiny page is still a success.
    assert result == {"content": "<i/>"}


async def test_read_webpage_unavailable_browser_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{_READ_WEBPAGE_MODULE}.BrowserSession", _UnavailableBrowserSession)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_webpage", {"url": "https://example.com/docs"})
    assert result == {"error": "read_webpage is unavailable: firefox is not installed"}


# ---------------------------------------------------------------------------
# query_search_engine
# ---------------------------------------------------------------------------


async def test_query_search_engine_rejects_unknown_browser(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "query_search_engine", {"engine": "bing", "query": "x", "browser": "lynx"}
    )
    assert result == {"error": "Unsupported 'browser': 'lynx'."}


async def test_query_search_engine_curl_returns_extracted_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched_urls: list[str] = []

    async def _fetch(url: str) -> object:
        fetched_urls.append(url)
        return curlfetch.FetchedPage(url=url, status=200, html="<html>results</html>")

    hits = [{"url": "https://docs.python.org/asyncio", "title": "asyncio", "snippet": "..."}]
    monkeypatch.setattr(curlfetch, "fetch", _fetch)
    monkeypatch.setattr(engines_static, "is_blocked", lambda engine, html: False)
    monkeypatch.setattr(engines_static, "extract_hits", lambda engine, html, base_url: hits)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "query_search_engine", {"engine": "bing", "query": "asyncio", "browser": "curl"}
    )
    assert result == {"hits": hits}
    assert fetched_urls == [engines_static.search_url("bing", "asyncio")]


async def test_query_search_engine_curl_wall_page_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, "<html>captcha</html>")
    monkeypatch.setattr(engines_static, "is_blocked", lambda engine, html: True)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "query_search_engine", {"engine": "google", "query": "x", "browser": "curl"}
    )
    assert "anti-bot/captcha wall" in str(result["error"])


async def test_query_search_engine_curl_blocked_status_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _walled(url: str) -> object:
        raise AntiBotWallError("HTTP 429")

    monkeypatch.setattr(curlfetch, "fetch", _walled)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "query_search_engine", {"engine": "duckduckgo", "query": "x", "browser": "curl"}
    )
    assert "anti-bot/captcha wall" in str(result["error"])


async def test_query_search_engine_unavailable_browser_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{_QUERY_ENGINE_MODULE}.BrowserSession", _UnavailableBrowserSession)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "query_search_engine", {"engine": "wikipedia", "query": "x"})
    assert result == {"error": "query_search_engine is unavailable: firefox is not installed"}


async def test_query_search_engine_unexpected_failure_becomes_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(url: str) -> object:
        raise RuntimeError("tls handshake failed")

    monkeypatch.setattr(curlfetch, "fetch", _boom)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "query_search_engine", {"engine": "bing", "query": "x", "browser": "curl"}
    )
    assert result == {"error": "Could not query bing: tls handshake failed"}
