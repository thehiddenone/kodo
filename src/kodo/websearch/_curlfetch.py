"""``curl_cffi``-backed fetch (doc/READ_WEBPAGE.md, doc/WEB_SEARCH.md).

The ``"curl"`` browser choice for ``read_webpage``/``query_search_engine``:
no browser process at all, just an HTTP client that replays a real browser's
TLS/HTTP2 fingerprint (JA3) via ``curl_cffi``. Per the botlab investigation
(doc/hidden/WEB_SEARCH_TOOL_REPORT.md), this passes DuckDuckGo/Bing/Wikipedia
where Playwright's own Chrome/Chromium signature gets walled, and it is far
cheaper than launching a browser for engines that are plain static HTML.

This is a deliberate, bounded exception to the project's historical
"no anti-bot circumvention" stance: TLS/browser-signature impersonation is a
network-layer technique, not JS-fingerprint spoofing, proxying, or CAPTCHA
solving — see WEB_SEARCH.md's revised stance.
"""

from __future__ import annotations

from dataclasses import dataclass

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from ._validate import AntiBotWallError, validate_public_url

__all__ = ["FetchedPage", "fetch"]

# Navigation budget, matching the browser paths' nav timeout.
_TIMEOUT_S = 20.0
# Impersonated browser signature (TLS/HTTP2/JA3) — a recent stable Chrome,
# per the investigation's recommendation.
_IMPERSONATE = "chrome131"
# HTTP statuses that mean "you are rate-limited / blocked" without a captcha page.
_BLOCKED_STATUSES = frozenset({403, 429, 503})


@dataclass(frozen=True)
class FetchedPage:
    """One page fetched via ``curl_cffi``.

    Attributes:
        url: The requested URL.
        status: HTTP status code.
        html: Raw response body, decoded as text.
    """

    url: str
    status: int
    html: str


async def fetch(url: str) -> FetchedPage:
    """Fetch *url* impersonating a real browser's TLS/HTTP2 fingerprint.

    Args:
        url: Absolute ``http``/``https`` URL.

    Returns:
        FetchedPage: Status + raw HTML body.

    Raises:
        InvalidUrlError: *url* fails the shared SSRF guard
            (:func:`~kodo.websearch._validate.validate_public_url`).
        AntiBotWallError: The response is HTTP 403/429/503, or the request
            otherwise failed (network error, timeout, TLS handshake failure).
    """
    await validate_public_url(url)
    try:
        async with AsyncSession() as session:
            response = await session.get(url, impersonate=_IMPERSONATE, timeout=_TIMEOUT_S)
    except RequestException as exc:
        raise AntiBotWallError(f"Request failed for {url}: {exc}") from exc
    if response.status_code in _BLOCKED_STATUSES:
        raise AntiBotWallError(
            f"The page responded with HTTP {response.status_code}, typical of an "
            "anti-bot or rate-limit wall."
        )
    return FetchedPage(url=url, status=response.status_code, html=response.text)
