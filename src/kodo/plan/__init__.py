"""Per-session work plan — one ordered task list and where the work has got to.

A **plan** is what a ``planner: true`` sub-agent hands back: an ordered list of
tasks plus the development context it established while writing them. The engine
parses that result and initializes the plan (``runtime/_engine/_subagents.py``);
no model ever writes one directly. The agent executing it reads the plan through
``get_plan`` and advances it one task at a time through ``plan_step_forward``.

Three rules shape the whole package:

* **One plan per session.** A plan may be superseded only once it is complete;
  a planner result arriving against an unfinished plan raises
  :class:`PlanConflictError`, which stops the session.
* **Forward only, and no failures.** The statuses are ``not_started`` /
  ``in_progress`` / ``done`` — there is no ``failed``. A plan that turns out to
  be wrong is replaced wholesale, not annotated.
* **Statuses are derived, never stored.** A ``plan_step`` line carries no
  payload; the statuses come from counting the steps recorded after the live
  plan (:func:`~._records.derive_state`), so no write can produce a ledger that
  contradicts itself.

Storage is **session**-scoped (``<session-dir>/plan/plan.jsonl``): a plan is a
fact about one session's attempt at some work, not about the project tree, and
two sessions working the same repo hold entirely separate plans. Like
:mod:`kodo.findings` this is a leaf package of plain functions with no in-memory
index — current state is always a replay of the log.

Spec: doc/PLANNING.md.
"""

from ._records import (
    ENTRY_PLAN_ABANDONED,
    ENTRY_PLAN_CREATED,
    ENTRY_PLAN_STEP,
    PLAN_CONTEXT_FIELD,
    PLAN_OUTPUT_FIELDS,
    PLAN_REASON_ABANDONED,
    PLAN_REASON_CREATED,
    PLAN_REASON_READ,
    PLAN_REASON_STEP,
    PLAN_TASK_TITLE_FIELD,
    PLAN_TASKS_FIELD,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    PlanConflictError,
    PlanState,
    PlanTask,
    TaskStatus,
    closed,
    derive_state,
    normalize_tasks,
)
from ._store import (
    PLAN_LOG_NAME,
    abandon_plan,
    create_plan,
    plan_log_path,
    read_plan,
    step_plan,
)

__all__ = [
    "ENTRY_PLAN_ABANDONED",
    "ENTRY_PLAN_CREATED",
    "ENTRY_PLAN_STEP",
    "PLAN_CONTEXT_FIELD",
    "PLAN_LOG_NAME",
    "PLAN_OUTPUT_FIELDS",
    "PLAN_REASON_ABANDONED",
    "PLAN_REASON_CREATED",
    "PLAN_REASON_READ",
    "PLAN_REASON_STEP",
    "PLAN_TASKS_FIELD",
    "PLAN_TASK_TITLE_FIELD",
    "PlanConflictError",
    "PlanState",
    "PlanTask",
    "STATUS_DONE",
    "STATUS_IN_PROGRESS",
    "STATUS_NOT_STARTED",
    "TaskStatus",
    "abandon_plan",
    "closed",
    "create_plan",
    "derive_state",
    "normalize_tasks",
    "plan_log_path",
    "read_plan",
    "step_plan",
]
