"""``web_search`` tool — three-phase themed web research (doc/WEB_SEARCH.md).

Dispatch handler for :data:`kodo.toolspecs.WEB_SEARCH`. Phases 1–2 (discovery
via Google/Bing/DuckDuckGo/Wikipedia, then scraping) run in :mod:`kodo.websearch` inside
one shared headless Chromium; phase 3 hands the scraped text to the silent
``web_summarizer`` sub-agent through the engine's dedicated ungated service
(:meth:`~kodo.tools.EngineServices.run_web_summarizer` — holding this tool *is*
the authorization, mirroring ``toolchain_deps``/``run_dependency_manager``).

Everything is best-effort: no anti-bot evasion is attempted, engines that
serve a captcha are put on a 30-minute cooldown persisted under
``~/.kodo/websearch/``, and every degradation (cooling engines, failed pages,
a failed summarization) is folded into the ``note`` instead of erroring the
call — the calling agent always gets a schema-compliant ``{themes, note}``.
"""

from __future__ import annotations

import json
import logging

from kodo.project import kodo_user_dir
from kodo.websearch import (
    BrowserSession,
    BrowserUnavailableError,
    CooldownStore,
    DiscoveryOutcome,
    PageText,
    ScrapeOutcome,
    discover,
    scrape_pages,
)

from ._tool import Tool

__all__ = ["WebSearchTool"]

_log = logging.getLogger(__name__)

# Bounds on the `max_results` input (the theme cap).
_DEFAULT_MAX_THEMES = 5
_MAX_THEMES = 10


class WebSearchTool(Tool):
    """Run the discovery → scrape → summarize pipeline for one query."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        query = tool_input.get("query")
        if not query or not isinstance(query, str):
            return json.dumps({"error": "web_search requires a non-empty 'query'."})
        max_themes = self.__theme_cap(tool_input.get("max_results"))
        _log.info("web_search from %s: %s", self.context.agent_name, query)

        notes: list[str] = []
        cooldowns = CooldownStore(kodo_user_dir() / "websearch" / "engine_cooldowns.json")
        try:
            async with BrowserSession() as session:
                if session.installed_now:
                    notes.append("Chromium was downloaded on first use (one-time setup).")
                discovery = await discover(session.browser, query, cooldowns)
                notes.append(self.__describe_discovery(discovery))
                if not discovery.hits:
                    notes.append("No web pages could be discovered; no report generated.")
                    return self.__result([], notes)
                scrape = await scrape_pages(session.browser, discovery.hits)
        except BrowserUnavailableError as exc:
            return self.__result([], [f"Web search is unavailable: {exc}"])
        except Exception as exc:  # noqa: BLE001 — best-effort tool, never crash the run
            _log.warning("web_search pipeline failed: %s", exc, exc_info=True)
            return self.__result([], [f"Web search failed: {exc}"])

        notes.append(self.__describe_scrape(discovery, scrape))
        if not scrape.pages:
            notes.append(
                "None of the discovered pages could be scraped; no report generated. "
                "Discovered URLs: " + ", ".join(h.url for h in discovery.hits)
            )
            return self.__result([], notes)

        themes = await self.__summarize(query, max_themes, scrape.pages)
        if themes is None:
            notes.append(
                "Theme summarization failed; returning no themes. Scraped source URLs: "
                + ", ".join(p.url for p in scrape.pages)
            )
            return self.__result([], notes)
        notes.append(f"Generated {len(themes)} theme(s).")
        return self.__result(themes, notes)

    async def __summarize(
        self, query: str, max_themes: int, pages: list[PageText]
    ) -> list[dict[str, object]] | None:
        """Phase 3 — delegate to the silent summarizer; ``None`` on failure."""
        task_input: dict[str, object] = {
            "query": query,
            "max_themes": max_themes,
            "sources": [{"url": p.url, "title": p.title, "text": p.text} for p in pages],
        }
        try:
            result = await self.context.services.run_web_summarizer(task_input)
        except Exception as exc:  # noqa: BLE001 — degrade to a note, keep the run alive
            _log.warning("web_search summarization failed: %s", exc, exc_info=True)
            return None
        themes = result.get("themes")
        return themes if isinstance(themes, list) else None

    @staticmethod
    def __theme_cap(raw: object) -> int:
        """Clamp the ``max_results`` input to a sane theme cap."""
        if isinstance(raw, int) and raw > 0:
            return min(raw, _MAX_THEMES)
        return _DEFAULT_MAX_THEMES

    @staticmethod
    def __describe_discovery(discovery: DiscoveryOutcome) -> str:
        """One sentence of phase-1 bookkeeping for the ``note``."""
        parts: list[str] = []
        if discovery.queried:
            parts.append(f"Queried {', '.join(discovery.queried)}.")
        for engine, reason in discovery.skipped.items():
            parts.append(f"Skipped {engine} ({reason}).")
        for engine in discovery.tripped:
            parts.append(f"{engine} served an anti-bot wall; pausing it for 30 minutes.")
        for engine, reason in discovery.errors.items():
            parts.append(f"{engine} failed ({reason}).")
        parts.append(f"Collected {len(discovery.hits)} link(s).")
        return " ".join(parts)

    @staticmethod
    def __describe_scrape(discovery: DiscoveryOutcome, scrape: ScrapeOutcome) -> str:
        """One sentence of phase-2 bookkeeping for the ``note``."""
        text = f"Scraped {len(scrape.pages)} of {len(discovery.hits)} page(s)."
        if scrape.failed:
            text += f" {len(scrape.failed)} page(s) yielded nothing usable."
        return text

    @staticmethod
    def __result(themes: list[dict[str, object]], notes: list[str]) -> str:
        return json.dumps({"themes": themes, "note": " ".join(n for n in notes if n)})
