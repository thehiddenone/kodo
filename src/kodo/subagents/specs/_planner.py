"""SubAgentSpec for ``planner`` — a standalone investigate-then-plan sub-agent.

The Planner is a **researcher that ends with a plan instead of a report**. It
holds read-only tools (``read_file``/``find_files``/``find_text_in_files``/
``get_root_paths``) and investigates the codebase itself before planning: the
Problem Solver no longer scopes the work first, it simply hands over any code
change whose shape it doesn't already know. The whole code study stays in the
Planner's own
sub-session, so the caller pays one round-trip and receives the distilled
result rather than the exploration.

Two things come back. ``codebase_context`` is a thorough, *anchored* briefing on
the code the work touches — real paths and real symbols, never guesses — which
the Problem Solver carries into every step. ``tasks`` is the ordered plan; each
task is an instruction *to the Problem Solver* naming which sub-agent to invoke
(``toolchain_builder``, ``investigator`` or ``developer``), how to build that
sub-agent's input, the concrete ``files`` involved, and the ``acceptance``
criteria that close the step. A ``toolchain_builder`` step, when the project has
no working build model, is always the first task.

``plan_warranted`` is false only when the work is genuinely indivisible — one
piece of building work with nothing to sequence. Only *coding* steps count
toward divisibility: toolchain setup, test writing and investigation are
supporting work. **``codebase_context`` is returned either way** — an
unplannable task must still repay the investigation, since the Problem Solver
is about to build it in one step on the strength of that briefing.

The Planner never executes anything: it has no write tools and no sub-agents.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["PLANNER"]


PLANNER: SubAgentSpec = SubAgentSpec(
    name="planner",
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": (
                    "The task to plan, in full: what the user asked for, the constraints, "
                    "and any answers the Problem Solver already gathered from the user. The "
                    "Planner investigates the code itself, so this need not describe the "
                    "codebase — but it must state the goal completely, since the Planner "
                    "sees only this prompt."
                ),
            },
            "roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Code roots to start from, when the user or the Problem Solver named "
                    "them. Omit to let the Planner discover the roots itself via "
                    "get_root_paths."
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
                    "True when the work divides into at least two independent coding steps; "
                    "false only when the work is indivisible — a single piece of building "
                    "work with nothing to sequence. Toolchain setup, test writing and "
                    "investigation are supporting work and never count toward divisibility."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why a plan is or isn't warranted — the independent coding steps you "
                    "found, or why the work is a single indivisible unit."
                ),
            },
            "codebase_context": {
                "type": "string",
                "description": (
                    "The briefing the Problem Solver carries into every step: what you "
                    "established by reading the code. Cover the layout and where the work "
                    "lands, the structures it touches and how they are wired, the "
                    "conventions new code must match, the blast radius, the hazards, and "
                    "the build/test story. Anchor every claim to real paths and real "
                    "symbols you actually read; state what you could not establish instead "
                    "of guessing. Required even when plan_warranted is false — the Problem "
                    "Solver then builds the whole thing in one step from this briefing. "
                    "Length follows the work; too thin is the expensive failure."
                ),
            },
            "tasks": {
                "type": "array",
                "description": (
                    "The ordered plan (empty when plan_warranted is false). Each task is an "
                    "instruction to the Problem Solver for one step. A toolchain_builder "
                    "task, when the project has no working build model, is always first."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                "Short, specific label for the step. Shown to the user as a "
                                "progress line after every completed step."
                            ),
                        },
                        "subagent": {
                            "type": "string",
                            "enum": ["toolchain_builder", "investigator", "developer"],
                            "description": (
                                "Which sub-agent the Problem Solver runs for this step. At "
                                "most one toolchain_builder step per plan, and it is always "
                                "first. An investigator step is only for what the Planner "
                                "could not establish itself — a fact that won't exist until "
                                "an earlier step lands, or web research."
                            ),
                        },
                        "instructions": {
                            "type": "string",
                            "description": (
                                "Instructions to the Problem Solver for this step: what it "
                                "must achieve, how to build the chosen sub-agent's input "
                                "(for toolchain_builder, the project_path/language/mode; "
                                "for investigator, how to derive its questions/roots; for "
                                "developer, what to build), which codebase_context facts "
                                "bear on it, and which earlier steps' outputs to feed in."
                            ),
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The concrete paths this step creates or changes, as far as "
                                "they can be determined. Expected on a developer step; "
                                "leave empty only when a path genuinely cannot be known "
                                "yet, and say why in instructions."
                            ),
                        },
                        "acceptance": {
                            "type": "string",
                            "description": (
                                "How the Problem Solver knows the step is done: the "
                                "behavior that must hold, the check that must pass, or the "
                                "artifact that must exist. Something observable — not "
                                "'the code is written'."
                            ),
                        },
                    },
                    "required": ["title", "subagent", "instructions", "acceptance"],
                },
            },
        },
        "required": ["plan_warranted", "reason", "codebase_context", "tasks"],
    },
)
