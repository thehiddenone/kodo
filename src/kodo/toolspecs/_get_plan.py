"""``get_plan`` tool spec — read the session's work plan (doc/PLANNING.md).

Auto-scoped and argument-free: a session has exactly one plan, so there is
nothing to name. The plan is created from a ``planner: true`` sub-agent's result
and advanced only by ``plan_step_forward`` — this tool never changes anything.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["GET_PLAN"]


GET_PLAN: ToolSpec = ToolSpec(
    name="get_plan",
    external_name="Get Plan",
    user_description="Read the current work plan",
    description=(
        "Report this session's work plan: every task in order as a `title` and a `status` "
        "of `not_started`, `in_progress` or `done`, plus `current_task` — the **whole** "
        "task you are on, with the planner's own `instructions`, `files`, `acceptance` "
        "and chosen `subagent` — and `complete` (whether every task is done), `abandoned` "
        "(whether it was closed unfinished) and `context` (the development context the "
        "planner established while writing the plan).\n\n"
        "The task list is titles only; the full body comes back for the task in progress "
        "and no other.\n\n"
        "A task can never be `failed`. The three statuses are the whole vocabulary: a "
        "plan only moves forward, and work that turns out to be misjudged is handled by "
        "closing the whole plan — finishing it, or abandoning it via plan_step_forward — "
        "and commissioning a new one, never by marking a task failed.\n\n"
        "The plan is created by the engine from a planner sub-agent's result — you cannot "
        "write one, and nothing you pass here changes it. A session has exactly one plan, "
        "so this call takes no arguments. `plan: null` means no planner has run yet in "
        "this session, which is a normal answer and not an error.\n\n"
        "When to use: whenever you need to know where the work stands — before picking up "
        "the next step, after a context compaction, or when the user asks what is left. "
        "The plan is not carried in your conversation; this tool is the only place its "
        "current state exists, and after a compaction it is the only place the current "
        "task's instructions still exist. Calling it also shows the user an up-to-date "
        "plan widget, so it is the right way to keep them oriented."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={
        "type": "object",
        "properties": {
            "plan": {
                "type": ["object", "null"],
                "description": (
                    "The session's plan, or null when no planner has run yet in this session."
                ),
                "properties": {
                    "created_by": {
                        "type": "string",
                        "description": "The planner sub-agent whose result this plan came from.",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "The development context the planner established while writing "
                            "the plan."
                        ),
                    },
                    "tasks": {
                        "type": "array",
                        "description": (
                            "Every task, in execution order — titles and statuses only. "
                            "The task in progress is also returned in full as "
                            "`current_task`."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "integer",
                                    "description": "1-based position, and the task's identity.",
                                },
                                "title": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["not_started", "in_progress", "done"],
                                },
                            },
                            "required": ["id", "title", "status"],
                        },
                    },
                    "current_task": {
                        "type": ["object", "null"],
                        "description": (
                            "The task you are on right now, in full — or null, which is the "
                            "case both before the first step and once the plan is complete. "
                            "Read `complete` to tell those apart. An abandoned plan still "
                            "names the task it stopped on. This is the planner's own "
                            "instruction for the step, restated here so you are working from "
                            "the plan rather than from memory of it."
                        ),
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "1-based position, as in `tasks`.",
                            },
                            "title": {"type": "string"},
                            "subagent": {
                                "type": "string",
                                "description": ("The sub-agent the planner chose, or empty."),
                            },
                            "instructions": {
                                "type": "string",
                                "description": (
                                    "What this step must achieve and how to build the "
                                    "sub-agent's input — the planner's words, not a summary."
                                ),
                            },
                            "files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Paths this step is expected to touch. Advisory, may be empty."
                                ),
                            },
                            "acceptance": {
                                "type": "string",
                                "description": ("What must be true before you take the next step."),
                            },
                        },
                        "required": [
                            "id",
                            "title",
                            "subagent",
                            "instructions",
                            "files",
                            "acceptance",
                        ],
                    },
                    "complete": {
                        "type": "boolean",
                        "description": "True once every task is done.",
                    },
                    "abandoned": {
                        "type": "boolean",
                        "description": (
                            "True when the plan was closed without being finished. Its "
                            "unfinished tasks keep their statuses — an abandoned plan says "
                            "how far the work got, not that it was done."
                        ),
                    },
                    "abandon_reason": {
                        "type": "string",
                        "description": "Why it was abandoned, or empty.",
                    },
                },
                "required": [
                    "created_by",
                    "context",
                    "tasks",
                    "current_task",
                    "complete",
                    "abandoned",
                    "abandon_reason",
                ],
            },
        },
        "required": ["plan"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={},
    # Hidden (the default) on purpose: the user reads this plan as the rendered
    # plan widget the call emits, not as the tool card's JSON. Showing both would
    # print the same plan twice, once in a shape nobody wants to read.
    output_visibility={},
)
