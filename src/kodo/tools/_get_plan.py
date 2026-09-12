"""``get_plan`` tool — read this session's work plan (doc/PLANNING.md)."""

from __future__ import annotations

import asyncio
import json

from kodo.plan import PLAN_REASON_READ, PlanState, read_plan

from ._tool import Tool

__all__ = ["GetPlanTool", "emit_plan_widget"]


async def emit_plan_widget(tool: Tool, plan: PlanState, reason: str) -> None:
    """Push *plan* to the user's plan widget, ignoring a host that has no services.

    Shared by both plan tools so the "show the user exactly what the model was
    told" rule has one implementation rather than two. ``services`` is ``None``
    only in tests that do not wire it (never in production), so a missing one is
    silently skipped rather than treated as a failure — the tool's own result is
    what the agent depends on, and a run must not fail because the UI side of a
    read could not be delivered.
    """
    services = tool.context.services
    if services is None:
        return
    await services.emit_plan_state(dict(plan), reason)


class GetPlanTool(Tool):
    """Report the session's plan, and refresh the user's plan widget with it.

    Auto-scoped and argument-free: a session has exactly one plan
    (``ToolContext.plan_dir``), so there is nothing for the agent to name. With no
    plan — every session where no planner has run — it answers ``{"plan": null}``
    rather than an error, which is what keeps one prompt correct both before and
    after a plan exists.

    Read-only with respect to the plan; the user-facing widget it emits is a
    view, not a change.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        plan_dir = self.context.plan_dir
        if plan_dir is None:
            return json.dumps({"plan": None})
        plan = await asyncio.to_thread(read_plan, plan_dir)
        if plan is None:
            return json.dumps({"plan": None})
        await emit_plan_widget(self, plan, PLAN_REASON_READ)
        return json.dumps({"plan": plan})
