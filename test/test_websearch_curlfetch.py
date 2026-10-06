"""Tests for the shared SSRF guard and the ``curl_cffi`` fetch backend.

No DNS or network traffic: ``socket.getaddrinfo`` is replaced with a fake
resolver and ``curl_cffi``'s ``AsyncSession`` with a scripted fake session.
"""

from __future__ import annotations

import socket
from types import TracebackType

import pytest
from curl_cffi.requests.exceptions import RequestException

from kodo.websearch import AntiBotWallError, InvalidUrlError, curlfetch, validate_public_url

_PUBLIC_IP = "93.184.216.34"


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    def _fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


# ---------------------------------------------------------------------------
# validate_public_url
# ---------------------------------------------------------------------------


async def test_validate_accepts_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, _PUBLIC_IP)
    await validate_public_url("https://example.com/page")


async def test_validate_accepts_scoped_ipv6_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946%eth0")
    await validate_public_url("https://example.com/")


@pytest.mark.parametrize("url", ["ftp://example.com/file", "file:///etc/passwd", "javascript:x"])
async def test_validate_rejects_disallowed_scheme(url: str) -> None:
    with pytest.raises(InvalidUrlError, match="scheme"):
        await validate_public_url(url)


async def test_validate_rejects_url_without_host() -> None:
    with pytest.raises(InvalidUrlError, match="no host"):
        await validate_public_url("http:///just-a-path")


async def test_validate_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(host: str, port: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(InvalidUrlError, match="Could not resolve"):
        await validate_public_url("https://no-such-host.invalid/")


async def test_validate_rejects_unparseable_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_to(monkeypatch, "not-an-ip")
    with pytest.raises(InvalidUrlError, match="Could not parse"):
        await validate_public_url("https://example.com/")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1"],
)
async def test_validate_rejects_internal_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _resolve_to(monkeypatch, _PUBLIC_IP, address)
    with pytest.raises(InvalidUrlError, match="private/internal"):
        await validate_public_url("https://example.com/")


# ---------------------------------------------------------------------------
# curlfetch.fetch
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeSession:
    """Stands in for ``curl_cffi.requests.AsyncSession``."""

    outcome: _FakeResponse | Exception = _FakeResponse(200, "")
    requested: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        _FakeSession.requested.append((url, kwargs))
        if isinstance(_FakeSession.outcome, Exception):
            raise _FakeSession.outcome
        return _FakeSession.outcome


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSession]:
    _resolve_to(monkeypatch, _PUBLIC_IP)
    monkeypatch.setattr(_FakeSession, "requested", [])
    monkeypatch.setattr(curlfetch, "AsyncSession", _FakeSession)
    return _FakeSession


async def test_fetch_returns_status_and_body(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_session, "outcome", _FakeResponse(200, "<html>hi</html>"))
    page = await curlfetch.fetch("https://example.com/a")
    assert page == curlfetch.FetchedPage(
        url="https://example.com/a", status=200, html="<html>hi</html>"
    )
    # The request impersonates a real browser's TLS fingerprint.
    [(url, kwargs)] = fake_session.requested
    assert url == "https://example.com/a"
    assert str(kwargs["impersonate"]).startswith("chrome")


async def test_fetch_passes_through_non_blocking_error_status(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_session, "outcome", _FakeResponse(404, "not found"))
    page = await curlfetch.fetch("https://example.com/missing")
    assert page.status == 404
    assert page.html == "not found"


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_fetch_blocked_status_raises_wall(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(fake_session, "outcome", _FakeResponse(status, "go away"))
    with pytest.raises(AntiBotWallError, match=str(status)):
        await curlfetch.fetch("https://example.com/")


async def test_fetch_request_failure_raises_wall(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_session, "outcome", RequestException("TLS handshake failed"))
    with pytest.raises(AntiBotWallError, match="Request failed"):
        await curlfetch.fetch("https://example.com/")


async def test_fetch_rejects_internal_url_before_any_request(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve_to(monkeypatch, "127.0.0.1")
    with pytest.raises(InvalidUrlError):
        await curlfetch.fetch("http://localhost:8080/")
    assert fake_session.requested == []
