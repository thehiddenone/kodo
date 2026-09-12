"""``plan_step_forward`` tool — advance the session's plan by one task.

The only writer of plan *progress* anywhere in the system; the plan's shape comes
from a planner's result and is never edited. See doc/PLANNING.md.
"""

from __future__ import annotations

import asyncio
import json

from kodo.plan import PLAN_REASON_ABANDONED, PLAN_REASON_STEP, abandon_plan, step_plan

from ._get_plan import emit_plan_widget
from ._tool import Tool

__all__ = ["PlanStepForwardTool"]


class PlanStepForwardTool(Tool):
    """Advance the plan by one task, or — with ``abandon_plan`` — close it unfinished.

    Two operations on one tool, because they are the same decision from opposite
    sides: this plan moves on, or this plan stops. An agent that can advance a
    plan can therefore also end one honestly, rather than reaching for the only
    other exit it can see — stepping to the end, which would record work nobody
    did as ``done``.

    Every way this can fail is **soft**: no plan, a closed plan, an abandon with
    nothing left to close. Each returns an ``{"error": …}`` envelope naming what
    to do instead, because none of them costs anything and the agent can act on
    the answer. That is the deliberate counterpart to the one hard failure in this
    feature — a planner superseding a plan that is still *open* — which ends the
    turn (doc/PLANNING.md §4). The difference is acknowledgement: abandoning is
    the legal route, and reaching the hard failure means the agent replanned over
    live work without ever saying so.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        plan_dir = self.context.plan_dir
        abandoning = bool(tool_input.get("abandon_plan"))
        if plan_dir is None:
            return json.dumps(
                {
                    "error": (
                        "There is no plan in this session, so there is nothing to abandon."
                        if abandoning
                        else "There is no plan in this session, so there is no step to take. "
                        "A plan is created by a planner sub-agent's result, not by this tool."
                    )
                }
            )
        try:
            if abandoning:
                reason = str(tool_input.get("reason", "")).strip()
                plan = await asyncio.to_thread(abandon_plan, plan_dir, reason)
            else:
                plan = await asyncio.to_thread(step_plan, plan_dir)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        await emit_plan_widget(
            self, plan, PLAN_REASON_ABANDONED if abandoning else PLAN_REASON_STEP
        )
        return json.dumps({"plan": plan})
