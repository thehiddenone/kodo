"""SubAgentSpec for ``planner`` — a standalone planning sub-agent.

The Planner receives a single ``instructions`` prompt from the Problem Solver
(the task plus any investigation results already gathered) and decides whether
the work is large enough to warrant a plan. A plan is warranted only when there
are **at least two independent steps** to execute; otherwise it returns
``plan_warranted: false`` and the Problem Solver runs the whole thing as one
developer task.

When a plan is warranted the Planner returns an ordered list of ``tasks``. Each
task is an instruction *to the Problem Solver* describing how to carry that step
out: which sub-agent to invoke (``investigator`` or ``developer``) and how to
build that sub-agent's input (including which earlier steps' outputs to feed in).
The Planner does not execute anything itself.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["PLANNER"]


PLANNER: SubAgentSpec = SubAgentSpec(
    name="planner",
    description=(
        "Decides whether a task needs a multi-step plan and, if so, returns an ordered list of "
        "independent Problem-Solver tasks (each naming the sub-agent to run and how to build its "
        "input); returns plan_warranted=false for anything that collapses to a single step."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": (
                    "The full context for planning: the task to accomplish plus any "
                    "investigation results already gathered. The Problem Solver must include "
                    "everything relevant here — the Planner sees only this prompt."
                ),
            },
        },
        "required": ["instructions"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "plan_warranted": {
                "type": "boolean",
                "description": (
                    "True only when there are at least two independent steps to execute; false "
                    "when the work collapses to a single step (nothing to plan)."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why a plan is or isn't warranted — the independent steps you see, or why "
                    "the work is a single step."
                ),
            },
            "tasks": {
                "type": "array",
                "description": (
                    "The ordered plan (empty when plan_warranted is false). Each task is an "
                    "instruction to the Problem Solver for one step."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short label for the step.",
                        },
                        "subagent": {
                            "type": "string",
                            "enum": ["investigator", "developer"],
                            "description": (
                                "Which sub-agent the Problem Solver runs for this step."
                            ),
                        },
                        "instructions": {
                            "type": "string",
                            "description": (
                                "Instructions to the Problem Solver for this step: what the step "
                                "must achieve, how to build the chosen sub-agent's input (for "
                                "investigator, how to derive its questions/roots; for developer, "
                                "what to build), and which earlier steps' outputs to feed in."
                            ),
                        },
                    },
                    "required": ["title", "subagent", "instructions"],
                },
            },
        },
        "required": ["plan_warranted", "reason", "tasks"],
    },
)
