"""``disable_autonomous_mode`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["DISABLE_AUTONOMOUS_MODE"]


DISABLE_AUTONOMOUS_MODE: ToolSpec = ToolSpec(
    name="disable_autonomous_mode",
    external_name="Disable Autonomous Mode",
    user_description="Disable autonomous mode",
    description=(
        "Break-glass tool. Forces the pipeline into interactive mode. Once pulled, "
        "autonomous mode stays off until the user explicitly re-enables it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why autonomous mode is being disabled.",
            },
        },
        "required": ["reason"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'disabled'."},
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.HIGH,
    input_visibility={"reason": "always"},
    output_visibility={"status": "always"},
    when_to_use=(
        "Only for diagnosed pipeline-level non-convergence — the same "
        "artifact (or pair of artifacts) reworked repeatedly (as a "
        "guideline, 3+ rework cycles without net progress) with a root "
        "cause that requires the user's intent to resolve.",
        "Never for ordinary, single-loop escalations — those are triaged "
        "normally (procedurally or substantively) without pulling the "
        "break-glass.",
    ),
)
