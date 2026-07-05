"""``wait`` tool spec — pause to avoid bursting search-engine requests.

Exclusive to the ``web_search`` agent (doc/WEB_SEARCH.md) by convention. A
plain sleep with no other effect — the agent's one lever for spacing out
requests instead of hammering an engine (or several engines at once) in a
tight burst, which is exactly what trips volume-based anti-bot walls
(doc/hidden/WEB_SEARCH_TOOL_REPORT.md).
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["WAIT"]

# Bounds on the `seconds` input.
_DEFAULT_SECONDS = 5
_MAX_SECONDS = 30

WAIT: ToolSpec = ToolSpec(
    name="wait",
    external_name="Wait",
    user_description="Pause briefly",
    description=(
        f"Pause for `seconds` (default {_DEFAULT_SECONDS}, max {_MAX_SECONDS} per call) "
        "and return nothing else. Use this to space out requests instead of bursting "
        "them: querying the same engine repeatedly in quick succession, or hitting "
        "several engines back-to-back with no pacing, is the single most common way "
        "to trip a volume-based anti-bot wall — it can burn out an engine (or the "
        "whole session's IP reputation) for everyone, not just this query. Guidelines: "
        "wait a few seconds between independent engine queries even when nothing "
        "looks wrong yet; wait longer (call `wait` more than once, or with a larger "
        "`seconds`) after any engine shows signs of suspicion; never rely on wait "
        "alone to fix a wall an engine already served — check "
        "get_web_search_state and back off that engine entirely instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": (
                    f"How long to pause, in seconds (default {_DEFAULT_SECONDS}, "
                    f"clamped to {_MAX_SECONDS})."
                ),
                "exclusiveMinimum": 0,
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {},
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"seconds": "visible"},
    output_visibility={},
    when_to_use=(
        "Between independent search-engine queries, to avoid a burst of requests "
        "that could trip a volume-based anti-bot wall.",
        "After any sign of anti-bot suspicion (a slow response, a partial wall), "
        "before trying anything else against that engine.",
    ),
)
