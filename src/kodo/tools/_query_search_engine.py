"""``query_search_engine`` tool — query one engine, one call (doc/WEB_SEARCH.md).

Dispatch handler for :data:`kodo.toolspecs.QUERY_SEARCH_ENGINE`. Branches on
``browser`` exactly like ``read_webpage``: every value but ``"curl"`` opens a
:class:`~kodo.websearch.BrowserSession` and queries through
:func:`kodo.websearch.query_via_browser` (the per-engine JS extractors in
:mod:`kodo.websearch._engines`, evaluated in a live page); ``"curl"`` fetches
the results page via :mod:`kodo.websearch.curlfetch` and extracts through
:mod:`kodo.websearch.engines_static` (a static HTML-parser port of the same
per-engine logic).

This is the ``web_search`` agent's discovery primitive — one engine per call,
so the agent decides which engine, when, and how to pace itself. A wall is
reported as an ``error`` (distinct from an empty ``hits`` list, which is a
legitimate "no organic results" outcome) so the agent knows to record a block
via ``update_web_search_state`` rather than conclude nothing was found.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, cast

from kodo.project import kodo_user_dir
from kodo.websearch import (
    SEARCH_ENGINES,
    AntiBotWallError,
    BrowserKind,
    BrowserSession,
    BrowserUnavailableError,
    curlfetch,
    engines_static,
    query_via_browser,
)

from ._tool import Tool

__all__ = ["QuerySearchEngineTool"]

_log = logging.getLogger(__name__)

_ReadBrowser = Literal["firefox", "chrome", "edge", "webkit", "chromium", "curl"]

_VALID_ENGINES = frozenset({"google", "bing", "duckduckgo", "wikipedia"})
_VALID_BROWSERS = frozenset({"firefox", "chrome", "edge", "webkit", "chromium", "curl"})
_DEFAULT_BROWSER: _ReadBrowser = "firefox"

_SEARCH_ENGINES_BY_NAME = {engine.name: engine for engine in SEARCH_ENGINES}


class QuerySearchEngineTool(Tool):
    """Query one search engine and return its organic hits."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        engine = tool_input.get("engine")
        if engine not in _VALID_ENGINES:
            return json.dumps({"error": f"Unsupported 'engine': {engine!r}."})
        query = tool_input.get("query")
        if not query or not isinstance(query, str):
            return json.dumps({"error": "query_search_engine requires a non-empty 'query'."})

        raw_browser = tool_input.get("browser")
        if raw_browser is None:
            browser: _ReadBrowser = _DEFAULT_BROWSER
        elif raw_browser in _VALID_BROWSERS:
            browser = cast(_ReadBrowser, raw_browser)
        else:
            return json.dumps({"error": f"Unsupported 'browser': {raw_browser!r}."})
        headed = bool(tool_input.get("headed", False))
        _log.info(
            "query_search_engine from %s: %s / %r (browser=%s)",
            self.context.agent_name,
            engine,
            query,
            browser,
        )

        try:
            hits = (
                await self.__query_curl(engine, query)
                if browser == "curl"
                else await self.__query_browser(engine, query, browser, headed)
            )
        except BrowserUnavailableError as exc:
            return json.dumps({"error": f"query_search_engine is unavailable: {exc}"})
        except Exception as exc:  # noqa: BLE001 — best-effort tool, never crash the run
            _log.warning(
                "query_search_engine failed for %s/%s: %s", engine, query, exc, exc_info=True
            )
            return json.dumps({"error": f"Could not query {engine}: {exc}"})

        if hits is None:
            return json.dumps(
                {
                    "error": (
                        f"{engine} appears to be showing an anti-bot/captcha wall for this "
                        "query. Record this via update_web_search_state before trying again "
                        "later or with a different engine/browser."
                    )
                }
            )
        return json.dumps({"hits": hits})

    async def __query_curl(self, engine: str, query: str) -> list[dict[str, str]] | None:
        url = engines_static.search_url(engine, query)
        try:
            fetched = await curlfetch.fetch(url)
        except AntiBotWallError:
            return None
        if engines_static.is_blocked(engine, fetched.html):
            return None
        return engines_static.extract_hits(engine, fetched.html, url)

    async def __query_browser(
        self, engine: str, query: str, browser: BrowserKind, headed: bool
    ) -> list[dict[str, str]] | None:
        search_engine = _SEARCH_ENGINES_BY_NAME[engine]
        browser_state_path = kodo_user_dir() / "websearch" / "browser_state.json"
        async with BrowserSession(browser_state_path, browser, headed=headed) as session:
            return await query_via_browser(session.browser, search_engine, query)
