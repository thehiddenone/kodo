"""``web_search`` tool — themed web research, agent-driven (doc/WEB_SEARCH.md).

Dispatch handler for :data:`kodo.toolspecs.WEB_SEARCH`. A thin wrapper: the
actual research (deciding which engines to query, reading pages, pacing
itself, synthesizing themes) is driven entirely by the ``web_search``
sub-agent through the engine's dedicated ungated service
(:meth:`~kodo.tools.EngineServices.run_web_search_agent` — holding this tool
*is* the authorization, mirroring ``toolchain_deps``/``run_dependency_manager``).
This handler only validates/clamps the input and returns the agent's result
verbatim — it already comes back schema-compliant via the agent's own
``return_result`` call.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["WebSearchTool"]

_log = logging.getLogger(__name__)

# Bounds on the `max_results` input (the theme cap).
_DEFAULT_MAX_THEMES = 5
_MAX_THEMES = 10

# Bounds on the `timeout` input, seconds.
_DEFAULT_TIMEOUT_S = 180.0
_MAX_TIMEOUT_S = 600.0


class WebSearchTool(Tool):
    """Delegate one query to the ``web_search`` agent and return its report."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        query = tool_input.get("query")
        if not query or not isinstance(query, str):
            return json.dumps({"error": "web_search requires a non-empty 'query'."})
        max_themes = self.__theme_cap(tool_input.get("max_results"))
        timeout = self.__timeout_cap(tool_input.get("timeout"))
        _log.info("web_search from %s: %s (timeout=%.0fs)", self.context.agent_name, query, timeout)

        try:
            result = await self.context.services.run_web_search_agent(
                {"query": query, "max_themes": max_themes, "timeout": timeout}
            )
        except Exception as exc:  # noqa: BLE001 — best-effort tool, never crash the run
            _log.warning("web_search agent failed: %s", exc, exc_info=True)
            return json.dumps({"themes": [], "note": f"Web search failed: {exc}"})

        themes = result.get("themes")
        note = result.get("note")
        return json.dumps(
            {
                "themes": themes if isinstance(themes, list) else [],
                "note": note if isinstance(note, str) else "",
            }
        )

    @staticmethod
    def __theme_cap(raw: object) -> int:
        """Clamp the ``max_results`` input to a sane theme cap."""
        if isinstance(raw, int) and raw > 0:
            return min(raw, _MAX_THEMES)
        return _DEFAULT_MAX_THEMES

    @staticmethod
    def __timeout_cap(raw: object) -> float:
        """Clamp the ``timeout`` input, seconds."""
        if isinstance(raw, (int, float)) and raw > 0:
            return min(float(raw), _MAX_TIMEOUT_S)
        return _DEFAULT_TIMEOUT_S
