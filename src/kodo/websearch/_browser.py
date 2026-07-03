"""Browser lifecycle for the web-search pipeline (doc/WEB_SEARCH.md §7).

:class:`BrowserSession` is an async context manager that starts Playwright and
launches one browser shared by the discovery and scraping phases of a single
``web_search`` (or ``read_webpage``) call.

Host browsers are attracted to far less anti-bot scrutiny than Playwright's
own bundled builds, so on every call the session first tries the machine's own
Google Chrome (``channel="chrome"``) and Microsoft Edge (``channel="msedge"``)
before ever touching a bundled download. Only when neither is installed does
it fall back to a Playwright-managed browser — bundled Firefox first, then
bundled Chromium as the last resort — auto-installing whichever one is needed
(a one-time ~150 MB download) via ``python -m playwright install <name>``.

Once a call has had to fall back, the outcome is cached in the caller-supplied
state file for :data:`_HOST_RECHECK_INTERVAL_S` (24h): subsequent sessions
skip straight to the last-known-good fallback browser instead of re-probing
Chrome/Edge every time. The cache expires once a day so a host browser
installed later is picked back up automatically.

The very first successful launch on a machine also runs a one-time sanity
check — navigating to ``https://example.com/`` — to catch a Playwright
install that starts a browser process but can't actually load a page (e.g. a
missing system dependency). Once this passes it is recorded in the same state
file and never repeated; if it fails, the whole session fails with
:class:`BrowserUnavailableError` rather than silently handing back a browser
that can't be trusted.

No anti-bot evasion is attempted anywhere: this is a best-effort pipeline, and
an engine that walls us off is simply put on cooldown by the discovery phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType

from playwright.async_api import Browser, BrowserType, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

__all__ = ["BrowserSession", "BrowserUnavailableError"]

_log = logging.getLogger(__name__)

# Upper bound on a one-time `playwright install <browser>` download.
_INSTALL_TIMEOUT_S = 600.0

# Host-installed browsers tried (in order) before any bundled fallback.
_HOST_CHANNELS = ("chrome", "msedge")

# How long a fallback decision is trusted before host browsers are re-tried.
_HOST_RECHECK_INTERVAL_S = 24 * 60 * 60

# One-time sanity check that Playwright can actually load a page.
_VALIDATION_URL = "https://example.com/"
_VALIDATION_TIMEOUT_MS = 15_000


class BrowserUnavailableError(Exception):
    """Raised when no browser could be launched, or the sanity check failed."""


@dataclass
class _BrowserState:
    """Persisted across calls under the caller-supplied ``state_path``."""

    example_check_passed: bool = False
    last_host_check: float = 0.0
    # Which bundled browser to use directly while the host-recheck cache is
    # still warm; ``None`` means the last successful launch was a host browser.
    fallback_kind: str | None = None


def _load_state(path: Path) -> _BrowserState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _BrowserState()
    except (OSError, ValueError):
        _log.warning("Unreadable browser state file %s; treating as fresh", path)
        return _BrowserState()
    if not isinstance(raw, dict):
        return _BrowserState()
    fallback_kind = raw.get("fallback_kind")
    if fallback_kind not in ("firefox", "chromium", None):
        fallback_kind = None
    last_host_check = raw.get("last_host_check")
    return _BrowserState(
        example_check_passed=bool(raw.get("example_check_passed", False)),
        last_host_check=(
            float(last_host_check) if isinstance(last_host_check, (int, float)) else 0.0
        ),
        fallback_kind=fallback_kind,
    )


def _save_state(path: Path, state: _BrowserState) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Best effort — a failed save only means the caching is forgotten.
        _log.warning("Could not persist browser state file %s", path)


class BrowserSession:
    """Owns one Playwright + browser pair for one ``web_search``/``read_webpage`` call.

    Usage::

        async with BrowserSession(state_path) as session:
            page = await session.browser.new_page()

    Args:
        state_path: JSON file used to cache the fallback decision and the
            one-time sanity-check result across calls (parent directories are
            created on first write).

    Attributes:
        installed_now: ``True`` when this session had to download a bundled
            browser before it could launch (first use on this machine, or the
            first time a given fallback kind was needed).
        installed_browser: Which bundled browser was installed (``"firefox"``
            or ``"chromium"``), or ``None`` if nothing was installed.
    """

    installed_now: bool
    installed_browser: str | None

    __playwright: Playwright | None
    __browser: Browser | None
    __state_path: Path

    def __init__(self, state_path: Path) -> None:
        self.installed_now = False
        self.installed_browser = None
        self.__playwright = None
        self.__browser = None
        self.__state_path = state_path

    @property
    def browser(self) -> Browser:
        """The launched browser (only valid inside the context)."""
        if self.__browser is None:
            raise BrowserUnavailableError("BrowserSession is not open.")
        return self.__browser

    async def __aenter__(self) -> BrowserSession:
        self.__playwright = await async_playwright().start()
        try:
            self.__browser = await self.__launch()
        except BaseException:
            await self.__playwright.stop()
            self.__playwright = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.__browser is not None:
            await self.__safe_close(self.__browser)
            self.__browser = None
        if self.__playwright is not None:
            await self.__playwright.stop()
            self.__playwright = None

    async def __launch(self) -> Browser:
        """Resolve a browser (host-first, bundled-fallback) and sanity-check it.

        Raises:
            BrowserUnavailableError: Nothing could be launched, or the launched
                browser failed the one-time ``example.com`` sanity check.
        """
        state = _load_state(self.__state_path)
        now = time.time()
        cache_is_fresh = now - state.last_host_check < _HOST_RECHECK_INTERVAL_S
        browser: Browser
        if state.fallback_kind is not None and cache_is_fresh:
            browser = await self.__launch_fallback(state, start_from=state.fallback_kind)
        else:
            state.last_host_check = now
            host_browser = await self.__try_host_browsers()
            if host_browser is not None:
                browser = host_browser
                state.fallback_kind = None
            else:
                browser = await self.__launch_fallback(state, start_from=None)
        _save_state(self.__state_path, state)

        try:
            await self.__validate(browser, state)
        except BaseException:
            await self.__safe_close(browser)
            raise
        return browser

    async def __try_host_browsers(self) -> Browser | None:
        """Try the machine's own Chrome, then Edge; ``None`` if neither exists."""
        assert self.__playwright is not None
        for channel in _HOST_CHANNELS:
            try:
                return await self.__playwright.chromium.launch(headless=True, channel=channel)
            except PlaywrightError:
                _log.debug("Host browser channel=%r unavailable", channel, exc_info=True)
        return None

    async def __launch_fallback(self, state: _BrowserState, start_from: str | None) -> Browser:
        """Launch a Playwright-managed browser: Firefox first, Chromium last.

        ``start_from`` lets the 24h-cached fast path jump straight to the
        last-known-good kind without retrying Firefox when it was Chromium.
        """
        assert self.__playwright is not None
        if start_from == "chromium":
            browser = await self.__launch_bundled(self.__playwright.chromium, "chromium")
            state.fallback_kind = "chromium"
            return browser
        try:
            browser = await self.__launch_bundled(self.__playwright.firefox, "firefox")
            state.fallback_kind = "firefox"
            return browser
        except BrowserUnavailableError:
            _log.warning("Bundled Firefox unavailable; falling back to bundled Chromium")
            browser = await self.__launch_bundled(self.__playwright.chromium, "chromium")
            state.fallback_kind = "chromium"
            return browser

    async def __launch_bundled(self, browser_type: BrowserType, name: str) -> Browser:
        """Launch a Playwright-bundled browser, auto-installing it on first use.

        Raises:
            BrowserUnavailableError: The launch failed and the automatic
                ``playwright install <name>`` could not fix it.
        """
        try:
            return await browser_type.launch(headless=True)
        except PlaywrightError as exc:
            # Playwright's missing-binary error says "Executable doesn't exist"
            # and suggests running install; anything else is not fixable here.
            if "install" not in str(exc).lower():
                raise BrowserUnavailableError(f"{name} failed to launch: {exc}") from exc
        _log.info("%s not installed; running one-time `playwright install %s`", name, name)
        await self.__install(name)
        self.installed_now = True
        self.installed_browser = name
        try:
            return await browser_type.launch(headless=True)
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                f"{name} still failed to launch after install: {exc}"
            ) from exc

    async def __validate(self, browser: Browser, state: _BrowserState) -> None:
        """One-time ``example.com`` sanity check; cached in ``state`` once passed.

        Raises:
            BrowserUnavailableError: The check has not passed before and this
                attempt failed — the browser cannot be trusted to load pages.
        """
        if state.example_check_passed:
            return
        try:
            page = await browser.new_page()
            try:
                response = await page.goto(_VALIDATION_URL, timeout=_VALIDATION_TIMEOUT_MS)
                if response is None or not response.ok:
                    status = response.status if response is not None else "no response"
                    raise BrowserUnavailableError(
                        f"Playwright sanity check failed: {_VALIDATION_URL} returned {status}."
                    )
            finally:
                await page.close()
        except PlaywrightError as exc:
            raise BrowserUnavailableError(f"Playwright sanity check failed: {exc}") from exc
        state.example_check_passed = True
        _save_state(self.__state_path, state)

    @staticmethod
    async def __install(name: str) -> None:
        """Run ``python -m playwright install <name>`` (one-time download).

        Raises:
            BrowserUnavailableError: The installer exited non-zero or timed out.
        """
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise BrowserUnavailableError(
                f"Automatic {name} install timed out; run `playwright install {name}` manually."
            ) from exc
        if proc.returncode != 0:
            tail = output.decode(errors="replace")[-500:]
            raise BrowserUnavailableError(
                f"Automatic {name} install failed; run `playwright install {name}` "
                f"manually. Installer output tail: {tail}"
            )
        _log.info("%s installed for web_search", name)

    @staticmethod
    async def __safe_close(browser: Browser) -> None:
        try:
            await browser.close()
        except PlaywrightError:
            _log.debug("Browser close failed", exc_info=True)
