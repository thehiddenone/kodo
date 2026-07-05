"""``remaining_time`` tool spec — the web_search agent's timeout countdown.

Exclusive to the ``web_search`` agent (doc/WEB_SEARCH.md) by convention. The
``web_search`` tool's caller-supplied ``timeout`` (capped at 600s) bounds the
whole run; this tool is how the agent checks its own budget so it can wrap up
with a usable report instead of running out the clock mid-search.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["REMAINING_TIME"]

REMAINING_TIME: ToolSpec = ToolSpec(
    name="remaining_time",
    external_name="Remaining Time",
    user_description="Check time left",
    description=(
        "Return how many seconds remain before this web_search call's timeout. "
        "HARD RULE: wrap up and call return_result while you still have a "
        "comfortable margin — do not wait until this reaches zero. If you let time "
        "run out, the run is cut off and returns whatever partial note it can "
        "salvage, which is always worse than a report you finished deliberately. As "
        "a guideline, once remaining_time drops below roughly a fifth of what you "
        "started with (or below ~20-30 seconds, whichever is larger), stop "
        "gathering new sources and synthesize the report from what you already have."
    ),
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "remaining_seconds": {
                "type": "number",
                "description": "Seconds left before this call's timeout (never negative).",
            },
        },
        "required": ["remaining_seconds"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    output_visibility={"remaining_seconds": "always"},
    when_to_use=(
        "Periodically during a research loop, to decide whether there's time for "
        "another engine query / page read or whether it's time to wrap up.",
        "Before starting a `wait` call, to make sure the pause itself won't eat into "
        "time needed to synthesize the final report.",
    ),
)
