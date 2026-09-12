"""Append/replay a session's single plan log.

All functions are synchronous file I/O; callers on a hot async path wrap them in
``asyncio.to_thread`` (the same convention :mod:`kodo.findings` and
:mod:`kodo.guided_state` use).

There is no index and no in-memory state: :func:`read_plan` replays the log every
time. That is what makes the status rule (doc/PLANNING.md §3) true by
construction — statuses are *derived* from the number of steps recorded after the
live plan, so no write can put the ledger into a state nothing can read.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._records import (
    ENTRY_PLAN_ABANDONED,
    ENTRY_PLAN_CREATED,
    ENTRY_PLAN_STEP,
    PlanConflictError,
    PlanState,
    PlanTask,
    closed,
    derive_state,
    normalize_tasks,
    plan_abandoned_entry,
    plan_created_entry,
    plan_step_entry,
)

__all__ = [
    "PLAN_LOG_NAME",
    "abandon_plan",
    "create_plan",
    "plan_log_path",
    "read_plan",
    "step_plan",
]

#: The log's filename inside the session directory. One plan log per session —
#: the file's very existence in a per-session directory is what enforces
#: "one plan per session"; there is no key and nothing to scope.
PLAN_LOG_NAME = "plan.jsonl"


def plan_log_path(plan_dir: Path) -> Path:
    """The plan log inside *plan_dir* (this session's ``plan/`` directory)."""
    return plan_dir / PLAN_LOG_NAME


def _read_jsonl(jsonl_path: Path) -> list[dict[str, object]]:
    if not jsonl_path.exists():
        return []
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _append(jsonl_path: Path, entry: dict[str, object]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _replay(history: list[dict[str, object]]) -> PlanState | None:
    """Fold a log into the **live** plan's current state, or ``None``.

    Only the last ``plan_created`` line matters: every earlier one was
    superseded, and a superseded plan was closed when it was replaced, so
    nothing is lost by not projecting it. Steps and the abandonment are counted
    from that line onward — anything recorded before it belonged to the plan it
    superseded, which is what keeps a replacement plan's state clean.
    """
    created_by = ""
    context = ""
    tasks: list[PlanTask] = []
    steps = 0
    abandoned = False
    abandon_reason = ""
    seen_plan = False
    for entry in history:
        kind = entry.get("type")
        if kind == ENTRY_PLAN_CREATED:
            seen_plan = True
            created_by = str(entry.get("created_by", ""))
            context = str(entry.get("context", ""))
            # Re-normalized on read rather than trusted: the ids are then
            # guaranteed positional even for a log written by an older build.
            tasks = normalize_tasks(entry.get("tasks"))
            steps = 0
            abandoned = False
            abandon_reason = ""
        elif kind == ENTRY_PLAN_STEP and seen_plan:
            steps += 1
        elif kind == ENTRY_PLAN_ABANDONED and seen_plan:
            abandoned = True
            abandon_reason = str(entry.get("reason", ""))
    if not seen_plan:
        return None
    return derive_state(
        created_by=created_by,
        context=context,
        tasks=tasks,
        steps=steps,
        abandoned=abandoned,
        abandon_reason=abandon_reason,
    )


def read_plan(plan_dir: Path) -> PlanState | None:
    """This session's live plan, or ``None`` when no plan was ever created.

    ``None`` is the ordinary answer for most of a session's life (every session
    that never ran a planner), not an error — callers render it as "there is no
    plan" rather than failing.

    Args:
        plan_dir: This session's ``plan/`` directory.

    Returns:
        PlanState | None: The live plan with current statuses, or ``None``.
    """
    return _replay(_read_jsonl(plan_log_path(plan_dir)))


def create_plan(
    plan_dir: Path, *, created_by: str, context: str, tasks: object
) -> PlanState | None:
    """Record a planner's result as this session's plan.

    The enforcement point for the one-plan-per-session rule (doc/PLANNING.md
    §4): a new plan may only be recorded when the session has no plan at all, or
    when the live one is **closed** — complete, or abandoned via
    :func:`abandon_plan`. Superseding an open plan raises
    :class:`PlanConflictError`, which the engine turns into a stopped turn rather
    than a tool error.

    Args:
        plan_dir: This session's ``plan/`` directory.
        created_by: Name of the ``planner: true`` agent whose result this is.
        context: The development context the planner established
            (its ``codebase_context``).
        tasks: The planner's raw ``tasks`` array, normalized here.

    Returns:
        PlanState | None: The newly created plan (every task ``not_started``), or
            ``None`` when *tasks* held nothing usable — a planner that found
            nothing to sequence leaves the session with no plan rather than an
            empty one, so a later planner call is free to create the first real
            plan without tripping the conflict rule.

    Raises:
        PlanConflictError: A live plan exists and is still open.
    """
    live = read_plan(plan_dir)
    if live is not None and not closed(live):
        outstanding = [t["title"] for t in live["tasks"] if t["status"] != "done"]
        raise PlanConflictError(
            f"{created_by} returned a new plan while the current plan still has "
            f"{len(outstanding)} unfinished task(s): {', '.join(outstanding)}. "
            f"A plan may only be superseded once it is closed — finish it, or abandon "
            f"it first with plan_step_forward(abandon_plan=true) stating why."
        )
    normalized = normalize_tasks(tasks)
    if not normalized:
        return None
    _append(
        plan_log_path(plan_dir),
        plan_created_entry(created_by=created_by, context=context, tasks=normalized),
    )
    return derive_state(created_by=created_by, context=context, tasks=normalized, steps=0)


def step_plan(plan_dir: Path) -> PlanState:
    """Advance the live plan by one step and return its new state.

    One step completes whatever task was in progress and starts the next; the
    first step starts task 1 without completing anything, and the last completes
    the final task without starting anything (:func:`~._records.derive_state`).

    Args:
        plan_dir: This session's ``plan/`` directory.

    Returns:
        PlanState: The plan after the step.

    Raises:
        ValueError: There is no plan to advance, or the live plan is already
            closed (complete or abandoned). All are *soft* failures the caller
            reports back to the model as a tool error — unlike a plan conflict,
            stepping past the end costs nothing and tells the agent exactly what
            it got wrong.
    """
    live = read_plan(plan_dir)
    if live is None:
        raise ValueError(
            "There is no plan in this session, so there is no step to take. A plan is "
            "created by a planner sub-agent's result, not by this tool."
        )
    if live["abandoned"]:
        raise ValueError(
            "This plan was abandoned, so its steps no longer mean anything. Run a "
            "planner sub-agent to create the plan that replaces it."
        )
    if live["complete"]:
        raise ValueError(
            "The plan is already complete — every task is done, so there is no next "
            "step. Report the finished work; a new plan can only come from another "
            "planner sub-agent run."
        )
    _append(plan_log_path(plan_dir), plan_step_entry())
    # Re-read rather than increment a local count: the log is the state, and
    # deriving from it keeps one code path between "the step I just took" and
    # "the plan I replay after a reload".
    stepped = read_plan(plan_dir)
    assert stepped is not None  # noqa: S101 - just appended to an existing plan
    return stepped


def abandon_plan(plan_dir: Path, reason: str) -> PlanState:
    """Close the live plan without finishing it, and return its final state.

    The deliberate escape from the one-plan-per-session rule (doc/PLANNING.md
    §4). Without it an agent whose work legitimately moved on had **no legal
    move**: re-planning stopped the turn, and stepping to the end would have
    recorded work nobody did as ``done`` — the one thing the derived-status design
    exists to make unwriteable.

    Not a status change. Every task keeps the status it had, so the record still
    shows how far the work actually got; only the plan is closed. A later
    :func:`create_plan` is then allowed.

    Args:
        plan_dir: This session's ``plan/`` directory.
        reason: Why the plan is being dropped. Not enforced to be non-empty here
            — the tool layer decides how hard to push for one — but it is what the
            user's widget shows and the only explanation the log will ever carry.

    Returns:
        PlanState: The plan, now ``abandoned``, with its statuses untouched.

    Raises:
        ValueError: There is no plan to abandon, or it is already closed. Soft
            failures, like :func:`step_plan`'s: abandoning a finished plan is a
            no-op the caller has misunderstood, not a breach of the rule.
    """
    live = read_plan(plan_dir)
    if live is None:
        raise ValueError("There is no plan in this session, so there is nothing to abandon.")
    if live["abandoned"]:
        raise ValueError("This plan was already abandoned; there is nothing left to close.")
    if live["complete"]:
        raise ValueError(
            "The plan is complete, so there is nothing to abandon — a finished plan can "
            "already be superseded by a new planner run."
        )
    _append(plan_log_path(plan_dir), plan_abandoned_entry(reason))
    abandoned = read_plan(plan_dir)
    assert abandoned is not None  # noqa: S101 - just appended to an existing plan
    return abandoned
