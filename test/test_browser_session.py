"""Tests for :class:`kodo.websearch.BrowserSession`'s per-kind launch logic.

No real Playwright browser is launched: :func:`kodo.websearch._browser.async_playwright`
is replaced with a fake whose ``chromium``/``firefox``/``webkit`` browser types
record launch attempts and can be scripted to succeed, raise a "not installed"
style error, or raise an unrelated failure. Since the caller now names the
exact ``kind`` to launch (no cascade), these tests focus on: host channels
(chrome/edge) launch via the chromium type with the right channel; bundled
kinds (firefox/webkit/chromium) auto-install on a missing binary; any other
failure raises immediately with no fallback to a different kind; and the
one-time ``example.com`` sanity check is cached **per kind**.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from kodo.websearch import BrowserSession, BrowserUnavailableError


class _FakePage:
    def __init__(self, response: _FakeResponse | None, raise_on_goto: Exception | None) -> None:
        self._response = response
        self._raise_on_goto = raise_on_goto
        self.closed = False

    async def goto(self, url: str, timeout: float) -> _FakeResponse | None:
        if self._raise_on_goto is not None:
            raise self._raise_on_goto
        return self._response

    async def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(self, ok: bool, status: int = 200) -> None:
        self.ok = ok
        self.status = status


class _FakeBrowser:
    def __init__(
        self,
        kind: str,
        *,
        page_response: _FakeResponse | None = None,
        page_raises: Exception | None = None,
    ) -> None:
        self.kind = kind
        self.closed = False
        self._page_response = page_response if page_response is not None else _FakeResponse(ok=True)
        self._page_raises = page_raises

    async def new_page(self) -> _FakePage:
        return _FakePage(self._page_response, self._page_raises)

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserType:
    """Stands in for ``playwright.chromium`` / ``.firefox`` / ``.webkit``."""

    def __init__(self, name: str, script: list[Any]) -> None:
        self.name = name
        self._script = script
        self.launch_calls: list[dict[str, Any]] = []

    async def launch(self, *, headless: bool = True, channel: str | None = None) -> _FakeBrowser:
        self.launch_calls.append({"headless": headless, "channel": channel})
        if not self._script:
            raise AssertionError(f"{self.name}.launch() called with no script left")
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakePlaywright:
    def __init__(
        self, chromium: _FakeBrowserType, firefox: _FakeBrowserType, webkit: _FakeBrowserType
    ) -> None:
        self.chromium = chromium
        self.firefox = firefox
        self.webkit = webkit

    async def stop(self) -> None:
        pass


class _FakePlaywrightContextManager:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self._playwright


def _patch_playwright(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chromium_script: list[Any] | None = None,
    firefox_script: list[Any] | None = None,
    webkit_script: list[Any] | None = None,
) -> tuple[_FakeBrowserType, _FakeBrowserType, _FakeBrowserType]:
    chromium = _FakeBrowserType("chromium", list(chromium_script or []))
    firefox = _FakeBrowserType("firefox", list(firefox_script or []))
    webkit = _FakeBrowserType("webkit", list(webkit_script or []))
    playwright = _FakePlaywright(chromium, firefox, webkit)
    monkeypatch.setattr(
        "kodo.websearch._browser.async_playwright",
        lambda: _FakePlaywrightContextManager(playwright),
    )
    return chromium, firefox, webkit


def _not_installed_error() -> PlaywrightError:
    return PlaywrightError("BrowserType.launch: Executable doesn't exist. Run playwright install")


@pytest.mark.asyncio
async def test_launches_host_chrome_via_chromium_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox, _ = _patch_playwright(monkeypatch, chromium_script=[_FakeBrowser("chrome")])
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path, "chrome") as session:
        assert session.browser.kind == "chrome"  # type: ignore[attr-defined]
    assert chromium.launch_calls == [{"headless": True, "channel": "chrome"}]
    assert firefox.launch_calls == []


@pytest.mark.asyncio
async def test_launches_host_edge_via_chromium_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, _, _ = _patch_playwright(monkeypatch, chromium_script=[_FakeBrowser("msedge")])
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path, "edge") as session:
        assert session.browser.kind == "msedge"  # type: ignore[attr-defined]
    assert chromium.launch_calls == [{"headless": True, "channel": "msedge"}]


@pytest.mark.asyncio
async def test_headed_flag_maps_to_headless_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, _, _ = _patch_playwright(monkeypatch, chromium_script=[_FakeBrowser("chrome")])
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path, "chrome", headed=True):
        pass
    assert chromium.launch_calls == [{"headless": False, "channel": "chrome"}]


@pytest.mark.asyncio
async def test_host_kind_missing_raises_immediately_no_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox, _ = _patch_playwright(monkeypatch, chromium_script=[_not_installed_error()])
    state_path = tmp_path / "browser_state.json"
    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path, "chrome"):
            pass
    # No cascade to firefox or any other kind.
    assert firefox.launch_calls == []


@pytest.mark.asyncio
async def test_bundled_firefox_installs_on_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(monkeypatch, firefox_script=[_not_installed_error(), _FakeBrowser("firefox")])

    async def _fake_install(name: str) -> None:
        assert name == "firefox"

    monkeypatch.setattr(BrowserSession, "_BrowserSession__install", staticmethod(_fake_install))

    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path, "firefox") as session:
        assert session.installed_now
        assert session.browser.kind == "firefox"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bundled_kind_other_failure_raises_without_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(monkeypatch, webkit_script=[PlaywrightError("some unrelated fatal error")])
    state_path = tmp_path / "browser_state.json"
    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path, "webkit"):
            pass


@pytest.mark.asyncio
async def test_bundled_kind_still_failing_after_install_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(monkeypatch, chromium_script=[_not_installed_error(), _not_installed_error()])

    async def _fake_install(name: str) -> None:
        return None

    monkeypatch.setattr(BrowserSession, "_BrowserSession__install", staticmethod(_fake_install))

    state_path = tmp_path / "browser_state.json"
    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path, "chromium"):
            pass


@pytest.mark.asyncio
async def test_sanity_check_cached_per_kind_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox, _ = _patch_playwright(
        monkeypatch,
        chromium_script=[_FakeBrowser("chrome")],
        firefox_script=[_FakeBrowser("firefox")],
    )
    state_path = tmp_path / "browser_state.json"

    async with BrowserSession(state_path, "chrome"):
        pass
    state = json.loads(state_path.read_text())
    assert state["sanity_passed"] == {"chrome": True}

    # A different kind still gets its own sanity check — the cache is keyed
    # per kind, not a single "we've checked once" flag.
    async with BrowserSession(state_path, "firefox"):
        pass
    state = json.loads(state_path.read_text())
    assert state["sanity_passed"] == {"chrome": True, "firefox": True}


@pytest.mark.asyncio
async def test_sanity_check_skipped_once_cached_for_that_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, _, _ = _patch_playwright(
        monkeypatch, chromium_script=[_FakeBrowser("chrome"), _FakeBrowser("chrome")]
    )
    state_path = tmp_path / "browser_state.json"

    async with BrowserSession(state_path, "chrome"):
        pass
    # Second session for the same kind: no new_page()/goto() round trip is
    # required (nothing to assert on directly besides "it still works").
    async with BrowserSession(state_path, "chrome") as session:
        assert session.browser.kind == "chrome"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sanity_check_failure_is_fatal_and_closes_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _FakeBrowser("chrome", page_response=_FakeResponse(ok=False, status=503))
    _patch_playwright(monkeypatch, chromium_script=[browser])
    state_path = tmp_path / "browser_state.json"

    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path, "chrome"):
            pass

    assert browser.closed
    assert not state_path.exists() or "chrome" not in json.loads(state_path.read_text()).get(
        "sanity_passed", {}
    )
