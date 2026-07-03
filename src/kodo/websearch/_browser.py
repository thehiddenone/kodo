"""Headless-Chromium lifecycle for the web-search pipeline.

:class:`BrowserSession` is an async context manager that starts Playwright and
launches one headless Chromium shared by the discovery and scraping phases of a
single ``web_search`` call. On the very first use of the machine the browser
binary may not exist yet; per the project decision the session then runs
``python -m playwright install chromium`` transparently (a one-time ~150 MB
download) and retries the launch once — :attr:`BrowserSession.installed_now`
tells the caller it happened so the tool can mention it in its ``note``.

No anti-bot evasion is attempted anywhere: this is a best-effort pipeline, and
an engine that walls us off is simply put on cooldown by the discovery phase.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import TracebackType

from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

__all__ = ["BrowserSession", "BrowserUnavailableError"]

_log = logging.getLogger(__name__)

# Upper bound on the one-time `playwright install chromium` download.
_INSTALL_TIMEOUT_S = 600.0


class BrowserUnavailableError(Exception):
    """Raised when no Chromium could be launched (and auto-install failed)."""


class BrowserSession:
    """Owns one Playwright + headless Chromium pair for one ``web_search`` call.

    Usage::

        async with BrowserSession() as session:
            page = await session.browser.new_page()

    Attributes:
        installed_now: ``True`` when this session had to download Chromium
            before it could launch (first use on this machine).
    """

    installed_now: bool

    __playwright: Playwright | None
    __browser: Browser | None

    def __init__(self) -> None:
        self.installed_now = False
        self.__playwright = None
        self.__browser = None

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
            try:
                await self.__browser.close()
            except PlaywrightError:
                _log.debug("Browser close failed", exc_info=True)
            self.__browser = None
        if self.__playwright is not None:
            await self.__playwright.stop()
            self.__playwright = None

    async def __launch(self) -> Browser:
        """Launch headless Chromium, auto-installing the binary on first use.

        Raises:
            BrowserUnavailableError: The launch failed and the automatic
                ``playwright install chromium`` could not fix it.
        """
        assert self.__playwright is not None
        try:
            return await self.__playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            # Playwright's missing-binary error says "Executable doesn't exist"
            # and suggests running install; anything else is not fixable here.
            if "install" not in str(exc).lower():
                raise BrowserUnavailableError(f"Chromium failed to launch: {exc}") from exc
        _log.info("Chromium not installed; running one-time `playwright install chromium`")
        await self.__install_chromium()
        self.installed_now = True
        try:
            return await self.__playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            raise BrowserUnavailableError(
                f"Chromium still failed to launch after install: {exc}"
            ) from exc

    @staticmethod
    async def __install_chromium() -> None:
        """Run ``python -m playwright install chromium`` (one-time download).

        Raises:
            BrowserUnavailableError: The installer exited non-zero or timed out.
        """
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT_S)
        except TimeoutError as exc:
            proc.kill()
            raise BrowserUnavailableError(
                "Automatic Chromium install timed out; run `playwright install chromium` manually."
            ) from exc
        if proc.returncode != 0:
            tail = output.decode(errors="replace")[-500:]
            raise BrowserUnavailableError(
                "Automatic Chromium install failed; run `playwright install chromium` "
                f"manually. Installer output tail: {tail}"
            )
        _log.info("Chromium installed for web_search")
