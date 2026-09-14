"""The two renderings of a plan: one for the model, one for the user's widget.

A :class:`~kodo.plan.PlanState` is the stored truth — every task with the full
body the planner wrote. Neither audience is given that wholesale:

* :func:`plan_for_model` — the executing agent's tool result. A titles-and-
  statuses ledger of the whole plan, plus the **full body of the one task in
  progress**. The ledger says where the work stands and what is left; the body
  says what the agent is doing right now, restated at the moment it starts
  rather than left many turns back in the planner's ``return_result`` (or gone
  entirely, after a compaction).
* :func:`plan_for_widget` — the user's plan card. The same ledger, with
  ``current_task`` as a bare id. The bodies are the agent's working material and
  would bury the one thing the card exists to show: how far the work has got.

They were one payload until this split — the model's tool result and the widget
event were the identical ``PlanState``, which made it impossible for the two to
disagree. That property is now weaker and deliberately so: both projections are
built **here**, from the same state, in one file, so the pair can still only
disagree in ways this module says they may.

See doc/PLANNING.md §6.
"""

from __future__ import annotations

from ._records import PlanState, PlanTask

__all__ = ["plan_for_model", "plan_for_widget"]


def _ledger(tasks: list[PlanTask]) -> list[dict[str, object]]:
    """The ordered ``{id, title, status}`` rows — the plan's shape, without its bodies.

    Shared by both projections: whatever else they differ about, the task list
    the user reads and the task list the model reads are built by the same code.
    """
    return [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in tasks]


def _detail(task: PlanTask) -> dict[str, object]:
    """One task as the executing agent needs it — the planner's instruction, in full."""
    return {
        "id": task["id"],
        "title": task["title"],
        "subagent": task["subagent"],
        "instructions": task["instructions"],
        "files": list(task["files"]),
        "acceptance": task["acceptance"],
    }


def plan_for_model(plan: PlanState) -> dict[str, object]:
    """The plan as the executing agent's tool result (``get_plan`` / ``plan_step_forward``).

    ``current_task`` is an **object** here — the whole task now in progress —
    where the widget's is a bare id. It is ``null`` in exactly the two situations
    the state itself has no current task: before the first step, and once the
    plan is complete or abandoned. ``complete`` is what tells those apart, which
    is why it is still worth sending on a plan with nothing in progress.

    Args:
        plan: The session's live plan state.

    Returns:
        dict[str, object]: The ``plan`` value of both plan tools' results.
    """
    current_id = plan["current_task"]
    current = next((t for t in plan["tasks"] if t["id"] == current_id), None)
    return {
        "created_by": plan["created_by"],
        "context": plan["context"],
        "tasks": _ledger(plan["tasks"]),
        "current_task": _detail(current) if current is not None else None,
        "complete": plan["complete"],
        "abandoned": plan["abandoned"],
        "abandon_reason": plan["abandon_reason"],
    }


def plan_for_widget(plan: PlanState) -> dict[str, object]:
    """The plan as the ``plan.state`` event payload (the user's plan card).

    Byte-for-byte the shape the client has always received — the bodies are
    dropped here rather than at the client, so a task's instructions never reach
    the WS wire or the session's marker log at all. The client renders the
    statuses as given and never computes one.

    Args:
        plan: The session's live plan state.

    Returns:
        dict[str, object]: The event payload, merged with ``reason``/``issue``
        by :meth:`~kodo.runtime._engine._events.EngineEmitters.emit_plan_state`.
    """
    return {
        "created_by": plan["created_by"],
        "context": plan["context"],
        "tasks": _ledger(plan["tasks"]),
        "current_task": plan["current_task"],
        "complete": plan["complete"],
        "abandoned": plan["abandoned"],
        "abandon_reason": plan["abandon_reason"],
    }
