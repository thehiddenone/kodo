"""``plan_step_forward`` tool spec — advance the session's plan, or abandon it.

The only way a plan's progress ever changes. A step takes no arguments for the
same reason ``get_plan`` does: a session has one plan, and a step is always "the
next one" — there is no task to name and no way to skip, reorder or go back.

``abandon_plan: true`` is the one other thing this tool does, and it lives here
rather than in a tool of its own because it is the same decision seen from the
other side: "this plan moves on" vs. "this plan stops". Keeping both on one tool
means an agent that has the means to advance a plan already has the means to end
it honestly, instead of reaching for the only other exit it can see — stepping to
the end, which would record work nobody did as done.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["PLAN_STEP_FORWARD"]


PLAN_STEP_FORWARD: ToolSpec = ToolSpec(
    name="plan_step_forward",
    external_name="Advance Plan",
    user_description="Move the plan to the next task",
    description=(
        "Move this session's work plan one step forward, and return the plan's new state "
        "in the same shape as `get_plan`.\n\n"
        "One step completes whatever task was in progress and starts the next. The very "
        "first step starts task 1 without completing anything — a plan begins with every "
        "task `not_started`, so call this once when you begin the first task. The last "
        "step completes the final task and leaves the plan `complete`.\n\n"
        "A step is irreversible and cannot be aimed: you cannot skip a task, reorder the "
        "plan, go back, or mark a task failed. Take a step only when the current task is "
        "genuinely finished — or, for the first call, when you are genuinely starting. "
        "Stepping because you are impatient with a task silently recasts unfinished work "
        "as done, and nothing downstream can tell.\n\n"
        "Calling this on an already-complete plan is an error, not a step: report the "
        "finished work instead. A new plan can only come from another planner sub-agent "
        "run.\n\n"
        "`abandon_plan: true` does the opposite: it CLOSES the plan without finishing it, "
        "leaving every unfinished task exactly as unfinished as it is. Use it when the "
        "work has genuinely moved on — the user redirected you, the plan was built on "
        "something that turned out to be wrong, or the remaining tasks no longer apply — "
        "and say why in `reason`. This is the ONLY way to replace a plan that is not "
        "finished: a planner returning a new plan while this one is still open ENDS THE "
        "TURN, so abandon first and then re-plan. Do not abandon a plan you simply want "
        "to finish faster, and never abandon as a substitute for telling the user that "
        "something is blocked.\n\n"
        "When to use: a step exactly twice per task boundary and nowhere else — once when "
        "you begin the first task, and once each time a task is genuinely complete (its "
        "acceptance criteria met, its check passing). Not to reorder, not to skip a task "
        "you have decided against, and not to record that something went wrong — a plan "
        "has no failure state, so a task you cannot finish is something to raise with the "
        "user, not to step over. Abandon only to close a plan the work has moved past, "
        "before commissioning its replacement. Either call refreshes the user's plan "
        "widget, so they see progress as it happens without you having to narrate it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "abandon_plan": {
                "type": "boolean",
                "description": (
                    "False (default) takes one step forward. True closes the plan without "
                    "finishing it — unfinished tasks stay unfinished — so a planner can "
                    "create its replacement. Give a `reason` when you pass this."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why the plan is being abandoned. Shown to the user and recorded as "
                    "the only explanation the plan's history will ever carry, so state "
                    "what changed, not that you are moving on. Ignored for an ordinary "
                    "step."
                ),
            },
        },
        "required": [],
    },
    output_schema={
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "description": (
                    "The plan after the step — same shape as get_plan's `plan`, with the "
                    "advanced statuses. For an abandon it is the closed plan, statuses "
                    "untouched."
                ),
                "properties": {
                    "created_by": {"type": "string"},
                    "context": {"type": "string"},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
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
                        "type": ["integer", "null"],
                        "description": (
                            "Id of the task now in progress, or null once the plan is complete."
                        ),
                    },
                    "complete": {
                        "type": "boolean",
                        "description": "True when that step finished the last task.",
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
    # Both inputs are shown: abandoning a plan is a notable act and the user
    # should see it on the card without having to infer it from the widget, and
    # the reason is the whole explanation.
    input_visibility={"abandon_plan": "visible", "reason": "always"},
    # Hidden (the default) on purpose — see ``GET_PLAN``: the widget this call
    # emits is the user's rendering of exactly this payload.
    output_visibility={},
)
