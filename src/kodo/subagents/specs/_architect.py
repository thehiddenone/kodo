"""SubAgentSpec for ``architect`` (stage 2 author, paired with architect_critic)."""

from __future__ import annotations

from .._artifacts import (
    PRODUCES_REMAINDER,
    ROLE_ARCHITECTURE,
    ROLE_NARRATIVE,
    ROLE_TECH_STACK,
    Need,
)
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, pipeline_input

__all__ = ["ARCHITECT"]


ARCHITECT: SubAgentSpec = SubAgentSpec(
    name="architect",
    input_schema=pipeline_input(
        input_paths="Must include the accepted Narrative and Tech Stack documents.",
    ),
    output_schema=author_output(
        extra_properties={
            "components": {
                "type": "array",
                "description": (
                    "The Responsibility Map as data: one entry per component you named, "
                    "with the codenames it depends on. You already decide this to write "
                    "Part 1 — declaring it here is what lets later stages be handed the "
                    "designs of a component's neighbours automatically, instead of "
                    "re-deriving your decomposition from prose. Required unless you are "
                    "escalating (see `reason`)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The component's stable codename, e.g. LEADERBOARD.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Codenames this component consumes. [] for one that "
                                "consumes nothing."
                            ),
                        },
                    },
                    "required": ["code", "depends_on"],
                },
            },
            "end_to_end_testable": {
                "type": "string",
                "enum": ["applicable", "excluded"],
                "description": (
                    "The Architect's end-to-end testability determination (Part 3 of the "
                    "architecture document). The Guide reads the stage-8 gate from this value. "
                    "Required unless you are escalating (see `reason`)."
                ),
            },
        },
    ),
    produces={ROLE_ARCHITECTURE: PRODUCES_REMAINDER},
    consumes=(Need(ROLE_NARRATIVE), Need(ROLE_TECH_STACK)),
)
