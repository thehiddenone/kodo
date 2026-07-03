"""``read_webpage`` tool — fetch one URL and return its main content as Markdown.

Dispatch handler for :data:`kodo.toolspecs.READ_WEBPAGE`. A thin wrapper over
:func:`kodo.websearch.read_page` (doc/READ_WEBPAGE.md): open one short-lived
headless Chromium session, fetch the URL, and convert its main content to
Markdown.

Best-effort like ``web_search``, but with a simpler failure contract: there is
no per-host cooldown state, so an anti-bot wall or SSRF-guarded URL just comes
back as an ``error`` telling the caller not to retry the same URL.
"""

from __future__ import annotations

import json
import logging

from kodo.websearch import (
    AntiBotWallError,
    BrowserSession,
    BrowserUnavailableError,
    InvalidUrlError,
    read_page,
    validate_public_url,
)

from ._tool import Tool

__all__ = ["ReadWebpageTool"]

_log = logging.getLogger(__name__)

_RETRY_ADVICE = (
    " Do not retry this exact URL — unlike web_search there is no cooldown here, so an "
    "immediate retry will fail the same way; try a different source or ask the user."
)


class ReadWebpageTool(Tool):
    """Fetch one URL and return its main content as Markdown."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        url = tool_input.get("url")
        if not url or not isinstance(url, str):
            return json.dumps({"error": "read_webpage requires a non-empty 'url'."})
        _log.info("read_webpage from %s: %s", self.context.agent_name, url)

        try:
            # Validated before touching Chromium: a bad/private-network URL
            # should fail without paying for a browser launch.
            await validate_public_url(url)
            async with BrowserSession() as session:
                page = await read_page(session.browser, url)
        except InvalidUrlError as exc:
            return json.dumps({"error": str(exc)})
        except AntiBotWallError as exc:
            return json.dumps({"error": str(exc) + _RETRY_ADVICE})
        except BrowserUnavailableError as exc:
            return json.dumps({"error": f"read_webpage is unavailable: {exc}"})
        except Exception as exc:  # noqa: BLE001 — best-effort tool, never crash the run
            _log.warning("read_webpage failed for %s: %s", url, exc, exc_info=True)
            return json.dumps({"error": f"Could not read {url}: {exc}"})

        markdown = f"# {page.title}\n\n{page.markdown}" if page.title else page.markdown
        return json.dumps({"markdown": markdown})
