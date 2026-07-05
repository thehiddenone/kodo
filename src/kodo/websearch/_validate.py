"""Shared SSRF guard and failure exceptions for every fetch backend.

Both ``read_webpage`` and ``query_search_engine`` accept a caller-given
target (a URL, or a search-engine query that becomes one) and fetch it with
one of several backends (Playwright browsers, ``curl_cffi``) — this module is
the one thing every backend shares: the pre-flight SSRF check and the two
failure exceptions each backend's fetch function raises.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

__all__ = ["AntiBotWallError", "InvalidUrlError", "validate_public_url"]

# Schemes any backend will navigate to / fetch.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class InvalidUrlError(Exception):
    """Raised when *url* fails validation before any request is made."""


class AntiBotWallError(Exception):
    """Raised when the page is an anti-bot wall or yields no usable content."""


async def validate_public_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts resolving to a non-public address.

    A standalone SSRF guard (DNS lookup only, no network fetch) so every
    backend can reject a bad URL before paying for a browser launch or an
    HTTP request.

    Raises:
        InvalidUrlError: *url* has a disallowed scheme, its host cannot be
            resolved, or a resolved address is private/loopback/link-local/
            reserved/unspecified.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"Unsupported URL scheme {parts.scheme!r}; only http/https are allowed."
        )
    host = parts.hostname
    if not host:
        raise InvalidUrlError(f"URL {url!r} has no host.")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise InvalidUrlError(f"Could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError as exc:
            raise InvalidUrlError(f"Could not parse resolved address for {host!r}: {exc}") from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise InvalidUrlError(
                f"URL host {host!r} resolves to a private/internal address ({addr}); "
                "only public internet pages may be fetched."
            )
