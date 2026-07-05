"""``read_webpage`` tool — fetch one URL and return its content (doc/READ_WEBPAGE.md).

Dispatch handler for :data:`kodo.toolspecs.READ_WEBPAGE`. Branches on the
``browser`` input: every value but ``"curl"`` opens a
:class:`~kodo.websearch.BrowserSession` and fetches through
:func:`kodo.websearch.fetch_via_browser`; ``"curl"`` fetches through
:mod:`kodo.websearch.curlfetch` (TLS/browser-signature impersonation, no
browser process) and extracts via :mod:`kodo.websearch.htmlextract` (a
static HTML parser standing in for the live DOM). Either way the same
``content_filter`` contract applies (``off``/``html``/``text``) and the same
length cap + "too thin" quality gate (shared here, not duplicated per
backend) decide the final ``content``.

Best-effort like ``query_search_engine``, but with a simpler failure
contract: there is no per-host cooldown state, so an anti-bot wall,
SSRF-guarded URL, or unavailable browser just comes back as an ``error``
telling the caller not to retry the same URL with the same browser.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, cast

from kodo.project import kodo_user_dir
from kodo.websearch import (
    AntiBotWallError,
    BrowserKind,
    BrowserSession,
    BrowserUnavailableError,
    ContentFilter,
    InvalidUrlError,
    curlfetch,
    fetch_via_browser,
    htmlextract,
    validate_public_url,
)

from ._tool import Tool

__all__ = ["ReadWebpageTool"]

_log = logging.getLogger(__name__)

_ReadBrowser = Literal["firefox", "chrome", "edge", "webkit", "chromium", "curl"]

_RETRY_ADVICE = (
    " Do not retry this exact URL with the same browser — unlike query_search_engine "
    "there is no cooldown here, so an immediate retry will fail the same way; a "
    "different `browser` choice may succeed, or try a different source, or ask the user."
)

_DEFAULT_BROWSER: _ReadBrowser = "firefox"
_DEFAULT_CONTENT_FILTER: ContentFilter = "text"
_VALID_BROWSERS = frozenset({"firefox", "chrome", "edge", "webkit", "chromium", "curl"})
_VALID_CONTENT_FILTERS = frozenset({"off", "html", "text"})

# content_filter: "text" — a synthesized Markdown extraction; kept small so a
# thin/walled page (that slipped past the wall heuristics) is still caught,
# and bounded so one page can't blow out the tool result.
_MAX_TEXT_CHARS = 20_000
_MIN_TEXT_CHARS = 40
# content_filter: "html"/"off" — the page's own markup, much bulkier than the
# text extraction; still capped as a safety valve, not a quality gate (there
# is no "too thin" check for these — the page's own content is whatever it is).
_MAX_RAW_CHARS = 50_000


class ReadWebpageTool(Tool):
    """Fetch one URL and return its content, shaped per ``content_filter``."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        url = tool_input.get("url")
        if not url or not isinstance(url, str):
            return json.dumps({"error": "read_webpage requires a non-empty 'url'."})

        raw_browser = tool_input.get("browser")
        if raw_browser is None:
            browser: _ReadBrowser = _DEFAULT_BROWSER
        elif raw_browser in _VALID_BROWSERS:
            browser = cast(_ReadBrowser, raw_browser)
        else:
            return json.dumps({"error": f"Unsupported 'browser': {raw_browser!r}."})

        raw_filter = tool_input.get("content_filter")
        if raw_filter is None:
            content_filter: ContentFilter = _DEFAULT_CONTENT_FILTER
        elif raw_filter in _VALID_CONTENT_FILTERS:
            content_filter = cast(ContentFilter, raw_filter)
        else:
            return json.dumps({"error": f"Unsupported 'content_filter': {raw_filter!r}."})

        headed = bool(tool_input.get("headed", False))
        _log.info("read_webpage from %s: %s (browser=%s)", self.context.agent_name, url, browser)

        try:
            # Validated before touching any backend (a browser launch or a
            # curl_cffi request): a bad/private-network URL should fail
            # without paying for either.
            await validate_public_url(url)
            if browser == "curl":
                title, content = await self.__fetch_curl(url, content_filter)
            else:
                title, content = await self.__fetch_browser(url, browser, headed, content_filter)
        except InvalidUrlError as exc:
            return json.dumps({"error": str(exc)})
        except AntiBotWallError as exc:
            return json.dumps({"error": str(exc) + _RETRY_ADVICE})
        except BrowserUnavailableError as exc:
            return json.dumps({"error": f"read_webpage is unavailable: {exc}"})
        except Exception as exc:  # noqa: BLE001 — best-effort tool, never crash the run
            _log.warning("read_webpage failed for %s: %s", url, exc, exc_info=True)
            return json.dumps({"error": f"Could not read {url}: {exc}"})

        return json.dumps({"content": self.__finalize(content_filter, title, content)})

    async def __fetch_browser(
        self, url: str, browser: BrowserKind, headed: bool, content_filter: ContentFilter
    ) -> tuple[str, str]:
        browser_state_path = kodo_user_dir() / "websearch" / "browser_state.json"
        async with BrowserSession(browser_state_path, browser, headed=headed) as session:
            result = await fetch_via_browser(session.browser, url, content_filter)
        return result.title, result.content

    async def __fetch_curl(self, url: str, content_filter: ContentFilter) -> tuple[str, str]:
        fetched = await curlfetch.fetch(url)
        if htmlextract.is_blocked(fetched.html):
            raise AntiBotWallError(
                "The page appears to be an anti-bot/captcha wall (e.g. a "
                "Cloudflare, reCAPTCHA, or hCaptcha challenge)."
            )
        if content_filter == "off":
            return "", htmlextract.extract_off(fetched.html)
        if content_filter == "html":
            return "", htmlextract.extract_html(fetched.html)
        return htmlextract.extract_text(fetched.html, url)

    @staticmethod
    def __finalize(content_filter: ContentFilter, title: str, content: str) -> str:
        """Apply the shared length cap + "too thin" gate, prepend a title heading."""
        if content_filter == "text":
            if len(content) < _MIN_TEXT_CHARS:
                raise AntiBotWallError(
                    "The page yielded almost no readable content after stripping "
                    "navigation/ads/scripts; it may be gated behind an anti-bot check, "
                    "a login wall, or a JavaScript-only app shell this tool can't render."
                )
            full = f"# {title}\n\n{content}" if title else content
            return full[:_MAX_TEXT_CHARS]
        return content[:_MAX_RAW_CHARS]
