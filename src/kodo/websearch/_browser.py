"""Browser lifecycle for ``read_webpage`` / ``query_search_engine``
(doc/READ_WEBPAGE.md, doc/WEB_SEARCH.md).

:class:`BrowserSession` launches exactly the browser kind the caller asks
for — there is no cascade/fallback. Both tools take an explicit ``browser``
input, so the caller (an agent) is making a deliberate choice (e.g.
``curl``/Firefox to dodge a particular anti-bot signature); silently
substituting a different browser would defeat that choice and could mask a
broken host-browser setup. If the requested kind can't be launched,
:class:`BrowserUnavailableError` is raised immediately — no fallback to any
other kind.

Bundled kinds (``firefox``/``webkit``/``chromium``) auto-install on first use
(one-time ~90-150 MB download via ``python -m playwright install <name>``).
Host kinds (``chrome``/``edge``) are launched via
``chromium.launch(channel=...)`` and are never installed — if missing, the
call fails.

Each kind gets its own one-time ``example.com`` sanity check (catches a
Playwright install that starts a browser process but can't actually load a
page, e.g. a missing system dependency), cached **per kind** in the
caller-supplied state file so a kind that has already proven itself is never
re-checked.

``curl`` is not a Playwright browser at all and never touches this module —
see :mod:`kodo.websearch._curlfetch`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal

from playwright.async_api import Browser, BrowserType, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

__all__ = ["BrowserKind", "BrowserSession", "BrowserUnavailableError"]

_log = logging.getLogger(__name__)

# Upper bound on a one-time `playwright install <browser>` download.
_INSTALL_TIMEOUT_S = 600.0

# One-time sanity check that Playwright can actually load a page.
_VALIDATION_URL = "https://example.com/"
_VALIDATION_TIMEOUT_MS = 15_000

BrowserKind = Literal["firefox", "chrome", "edge", "webkit", "chromium"]

# Bundled kinds Playwright manages itself (auto-installed on first use).
_BUNDLED_KINDS = frozenset({"firefox", "webkit", "chromium"})
# Host kinds launched via a Chromium channel; never auto-installed.
_HOST_CHANNELS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


class BrowserUnavailableError(Exception):
    """Raised when the requested browser kind could not be launched."""


@dataclass
class _BrowserState:
    """Persisted across calls under the caller-supplied ``state_path``.

    One-time ``example.com`` sanity-check result, cached **per browser
    kind** — unlike the old cascade-era state, there is no single "fallback
    decision" to remember, just which kinds have already proven themselves.
    """

    sanity_passed: dict[str, bool] = field(default_factory=dict)


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
    sanity_raw = raw.get("sanity_passed")
    sanity = (
        {k: bool(v) for k, v in sanity_raw.items() if isinstance(k, str)}
        if isinstance(sanity_raw, dict)
        else {}
    )
    return _BrowserState(sanity_passed=sanity)


def _save_state(path: Path, state: _BrowserState) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"sanity_passed": state.sanity_passed}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        # Best effort — a failed save only means the caching is forgotten.
        _log.warning("Could not persist browser state file %s", path)


class BrowserSession:
    """Owns one Playwright + browser pair for one tool call.

    Usage::

        async with BrowserSession(state_path, "firefox", headed=False) as session:
            page = await session.browser.new_page()

    Args:
        state_path: JSON file used to cache the one-time per-kind
            sanity-check result across calls (parent directories are created
            on first write).
        kind: Which browser to launch — ``firefox``/``webkit``/``chromium``
            are Playwright-bundled (auto-installed on first use);
            ``chrome``/``edge`` are the host's own installs, launched via a
            Chromium channel.
        headed: Launch with a visible window instead of headless.

    Attributes:
        installed_now: ``True`` when this session had to download *kind*
            before it could launch (first use of that bundled browser on
            this machine).
    """

    installed_now: bool

    __playwright: Playwright | None
    __browser: Browser | None
    __state_path: Path
    __kind: BrowserKind
    __headed: bool

    def __init__(self, state_path: Path, kind: BrowserKind, headed: bool = False) -> None:
        self.installed_now = False
        self.__playwright = None
        self.__browser = None
        self.__state_path = state_path
        self.__kind = kind
        self.__headed = headed

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
        """Launch :attr:`__kind` and sanity-check it.

        Raises:
            BrowserUnavailableError: *kind* could not be launched, or it
                failed the one-time ``example.com`` sanity check.
        """
        browser = (
            await self.__launch_host(self.__kind)
            if self.__kind in _HOST_CHANNELS
            else await self.__launch_bundled(self.__kind)
        )
        state = _load_state(self.__state_path)
        try:
            await self.__validate(browser, state)
        except BaseException:
            await self.__safe_close(browser)
            raise
        return browser

    async def __launch_host(self, kind: str) -> Browser:
        """Launch a host-installed browser via its Chromium channel.

        Raises:
            BrowserUnavailableError: The channel isn't installed on this
                host — no fallback to any other kind.
        """
        assert self.__playwright is not None
        channel = _HOST_CHANNELS[kind]
        try:
            return await self.__playwright.chromium.launch(
                headless=not self.__headed, channel=channel
            )
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                f"{kind!r} is not installed on this host (channel={channel!r}): {exc}"
            ) from exc

    async def __launch_bundled(self, kind: str) -> Browser:
        """Launch a Playwright-bundled browser, auto-installing it on first use.

        Raises:
            BrowserUnavailableError: The launch failed and the automatic
                ``playwright install <kind>`` could not fix it.
        """
        assert self.__playwright is not None
        browser_type: BrowserType = getattr(self.__playwright, kind)
        try:
            return await browser_type.launch(headless=not self.__headed)
        except PlaywrightError as exc:
            # Playwright's missing-binary error says "Executable doesn't exist"
            # and suggests running install; anything else is not fixable here.
            if "install" not in str(exc).lower():
                raise BrowserUnavailableError(f"{kind} failed to launch: {exc}") from exc
        _log.info("%s not installed; running one-time `playwright install %s`", kind, kind)
        await self.__install(kind)
        self.installed_now = True
        try:
            return await browser_type.launch(headless=not self.__headed)
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                f"{kind} still failed to launch after install: {exc}"
            ) from exc

    async def __validate(self, browser: Browser, state: _BrowserState) -> None:
        """One-time ``example.com`` sanity check; cached per kind in ``state``.

        Raises:
            BrowserUnavailableError: :attr:`__kind` has not passed the check
                before and this attempt failed.
        """
        if state.sanity_passed.get(self.__kind, False):
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
        state.sanity_passed[self.__kind] = True
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
        _log.info("%s installed", name)

    @staticmethod
    async def __safe_close(browser: Browser) -> None:
        try:
            await browser.close()
        except PlaywrightError:
            _log.debug("Browser close failed", exc_info=True)
