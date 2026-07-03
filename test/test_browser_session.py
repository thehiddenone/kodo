"""Tests for :class:`kodo.websearch.BrowserSession`'s browser-resolution logic.

No real Playwright browser is launched: :func:`kodo.websearch._browser.async_playwright`
is replaced with a fake whose ``chromium``/``firefox`` browser types record
launch attempts and can be scripted to succeed, raise a "not installed" style
error, or raise an unrelated failure. This lets the host-first / bundled-
fallback / daily-recheck-cache / one-time-sanity-check logic be exercised
without any network access or installed browser binaries.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from kodo.websearch import BrowserSession, BrowserUnavailableError
from kodo.websearch._browser import _HOST_RECHECK_INTERVAL_S


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
    """Stands in for ``playwright.chromium`` / ``playwright.firefox``."""

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
    def __init__(self, chromium: _FakeBrowserType, firefox: _FakeBrowserType) -> None:
        self.chromium = chromium
        self.firefox = firefox

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
) -> tuple[_FakeBrowserType, _FakeBrowserType]:
    chromium = _FakeBrowserType("chromium", list(chromium_script or []))
    firefox = _FakeBrowserType("firefox", list(firefox_script or []))
    playwright = _FakePlaywright(chromium, firefox)
    monkeypatch.setattr(
        "kodo.websearch._browser.async_playwright",
        lambda: _FakePlaywrightContextManager(playwright),
    )
    return chromium, firefox


def _not_installed_error() -> PlaywrightError:
    return PlaywrightError("BrowserType.launch: Executable doesn't exist. Run playwright install")


@pytest.mark.asyncio
async def test_prefers_host_chrome_over_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox = _patch_playwright(
        monkeypatch,
        chromium_script=[_FakeBrowser("chrome")],
    )
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "chrome"  # type: ignore[attr-defined]
    assert chromium.launch_calls == [{"headless": True, "channel": "chrome"}]
    assert firefox.launch_calls == []


@pytest.mark.asyncio
async def test_falls_back_to_host_edge_when_chrome_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox = _patch_playwright(
        monkeypatch,
        chromium_script=[_not_installed_error(), _FakeBrowser("msedge")],
    )
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "msedge"  # type: ignore[attr-defined]
    assert [c["channel"] for c in chromium.launch_calls] == ["chrome", "msedge"]
    assert firefox.launch_calls == []


@pytest.mark.asyncio
async def test_falls_back_to_bundled_firefox_when_no_host_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, firefox = _patch_playwright(
        monkeypatch,
        chromium_script=[_not_installed_error(), _not_installed_error()],
        firefox_script=[_FakeBrowser("firefox")],
    )
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "firefox"  # type: ignore[attr-defined]
    assert not session.installed_now

    state = json.loads(state_path.read_text())
    assert state["fallback_kind"] == "firefox"
    assert state["example_check_passed"] is True


@pytest.mark.asyncio
async def test_installs_firefox_when_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(
        monkeypatch,
        chromium_script=[_not_installed_error(), _not_installed_error()],
        firefox_script=[_not_installed_error(), _FakeBrowser("firefox")],
    )

    async def _fake_install(name: str) -> None:
        assert name == "firefox"

    monkeypatch.setattr(BrowserSession, "_BrowserSession__install", staticmethod(_fake_install))

    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path) as session:
        assert session.installed_now
        assert session.installed_browser == "firefox"


@pytest.mark.asyncio
async def test_cascades_to_chromium_when_firefox_totally_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(
        monkeypatch,
        chromium_script=[_not_installed_error(), _not_installed_error(), _FakeBrowser("chromium")],
        firefox_script=[PlaywrightError("some unrelated fatal error")],
    )
    state_path = tmp_path / "browser_state.json"
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "chromium"  # type: ignore[attr-defined]

    state = json.loads(state_path.read_text())
    assert state["fallback_kind"] == "chromium"


@pytest.mark.asyncio
async def test_raises_when_nothing_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_playwright(
        monkeypatch,
        chromium_script=[
            _not_installed_error(),
            _not_installed_error(),
            _not_installed_error(),
            _not_installed_error(),
        ],
        firefox_script=[_not_installed_error(), _not_installed_error()],
    )
    state_path = tmp_path / "browser_state.json"
    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path):
            pass


@pytest.mark.asyncio
async def test_cached_fallback_skips_host_probe_within_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "browser_state.json"
    state_path.write_text(
        json.dumps(
            {
                "example_check_passed": True,
                "last_host_check": time.time(),
                "fallback_kind": "firefox",
            }
        )
    )
    chromium, firefox = _patch_playwright(
        monkeypatch,
        firefox_script=[_FakeBrowser("firefox")],
    )
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "firefox"  # type: ignore[attr-defined]
    assert chromium.launch_calls == []
    assert len(firefox.launch_calls) == 1


@pytest.mark.asyncio
async def test_cached_fallback_expires_after_a_day_and_retries_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "browser_state.json"
    state_path.write_text(
        json.dumps(
            {
                "example_check_passed": True,
                "last_host_check": time.time() - _HOST_RECHECK_INTERVAL_S - 1,
                "fallback_kind": "firefox",
            }
        )
    )
    chromium, firefox = _patch_playwright(
        monkeypatch,
        chromium_script=[_FakeBrowser("chrome")],
    )
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "chrome"  # type: ignore[attr-defined]
    assert chromium.launch_calls == [{"headless": True, "channel": "chrome"}]
    assert firefox.launch_calls == []

    state = json.loads(state_path.read_text())
    assert state["fallback_kind"] is None


@pytest.mark.asyncio
async def test_example_check_runs_once_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chromium, _ = _patch_playwright(
        monkeypatch,
        chromium_script=[_FakeBrowser("chrome"), _FakeBrowser("chrome")],
    )
    state_path = tmp_path / "browser_state.json"

    async with BrowserSession(state_path) as session:
        pass
    state = json.loads(state_path.read_text())
    assert state["example_check_passed"] is True

    # Second session: the cached pass means no new_page()/goto() round trip is
    # required (nothing to assert on directly here besides "it still works").
    async with BrowserSession(state_path) as session:
        assert session.browser.kind == "chrome"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_example_check_failure_is_fatal_and_closes_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _FakeBrowser("chrome", page_response=_FakeResponse(ok=False, status=503))
    _patch_playwright(monkeypatch, chromium_script=[browser])
    state_path = tmp_path / "browser_state.json"

    with pytest.raises(BrowserUnavailableError):
        async with BrowserSession(state_path):
            pass

    assert browser.closed
    assert not state_path.exists() or not json.loads(state_path.read_text())["example_check_passed"]
