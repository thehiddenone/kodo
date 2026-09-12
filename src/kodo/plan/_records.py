"""The append-only entry types in a session's plan log, and the status rule.

A **plan** is one ordered list of tasks a ``planner: true`` sub-agent handed
back, plus the development context it established while writing that list. A
plan log holds three kinds of line:

* ``plan_created`` — a whole plan arriving at once. It carries the ordered
  ``tasks`` and the ``context``, and *supersedes* whatever plan came before it.
  The engine only ever appends one when the previous plan is **closed** (see
  :func:`kodo.plan.create_plan`), so a log is a sequence of closed plans
  followed by at most one live plan.
* ``plan_step`` — one ``plan_step_forward`` call. It carries no payload at all:
  a step is a *pointer move*, and the statuses are derived from how many steps
  follow the live ``plan_created`` (:func:`derive_state`).
* ``plan_abandoned`` — the live plan is closed without being finished, carrying
  the caller's ``reason``. It records no status either: the unfinished tasks stay
  unfinished, so the log keeps saying how far the work actually got.

Storing steps rather than statuses is what makes the status rule impossible to
violate by writing a bad line: there is no way to record "task 3 done while
task 2 is not started", because no line ever names a status.

A task cannot **fail**. There are exactly three statuses — ``not_started``,
``in_progress``, ``done`` — and a plan only ever moves forward. Work that turns
out to be wrong is handled by *abandoning* the plan and then superseding it, not
by marking a task failed and not by stepping to the end (which would record
unfinished work as done).

See doc/PLANNING.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypedDict

__all__ = [
    "ENTRY_PLAN_ABANDONED",
    "ENTRY_PLAN_CREATED",
    "ENTRY_PLAN_STEP",
    "PLAN_CONTEXT_FIELD",
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
    "closed",
    "derive_state",
    "normalize_tasks",
    "plan_abandoned_entry",
    "plan_created_entry",
    "plan_step_entry",
]

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"

#: The complete status vocabulary. There is deliberately no ``failed``: a task
#: is a unit of work to be carried out, not an attempt that can be scored, and a
#: plan that turns out to be wrong is replaced wholesale rather than annotated.
TaskStatus = Literal["not_started", "in_progress", "done"]

ENTRY_PLAN_CREATED = "plan_created"
ENTRY_PLAN_STEP = "plan_step"
ENTRY_PLAN_ABANDONED = "plan_abandoned"

# The property every task object in a planner's ``tasks`` schema must declare.
# The engine reads only this one off each task (see :class:`PlanTask`), so it is
# also the whole of what :class:`~kodo.subagents.AgentRegistry` type-checks a
# planner's ``tasks`` items against at load time.
PLAN_TASK_TITLE_FIELD = "title"

#: The two output fields a ``planner: true`` sub-agent's result must carry — the
#: whole contract between a planner and the engine (doc/PLANNING.md §2).
#:
#: Fixed names rather than per-agent frontmatter on purpose: these *are* the
#: planner role, the way ``paths`` is the shape of a reported work product, so a
#: second planner conforms to the same two names instead of teaching the engine a
#: third vocabulary. :class:`~kodo.subagents.AgentRegistry` enforces them at load
#: time against the agent's ``output_schema``, so the coupling is checked rather
#: than assumed.
PLAN_TASKS_FIELD = "tasks"
PLAN_CONTEXT_FIELD = "codebase_context"
PLAN_OUTPUT_FIELDS: frozenset[str] = frozenset({PLAN_TASKS_FIELD, PLAN_CONTEXT_FIELD})

# ``reason`` on the plan-state event: what produced the view the user is looking
# at. Never shown to a model — the model reads the plan out of its own tool
# result, which carries no reason at all. These live here, with the records,
# because both the engine (on creation) and the two plan tools emit the event,
# and a vocabulary two layers share should not be owned by either of them.
PLAN_REASON_CREATED = "created"
PLAN_REASON_READ = "read"
PLAN_REASON_STEP = "step"
PLAN_REASON_ABANDONED = "abandoned"

#: How long a task title may be before it is truncated. Titles are model-authored
#: and go straight into a UI widget; a runaway one would break the layout and
#: tell the reader nothing the first clause did not.
_MAX_TITLE_CHARS = 200


class PlanConflictError(Exception):
    """A new plan arrived while the live one was still open.

    The one-plan-per-session rule (doc/PLANNING.md §4): a plan may only be
    superseded once it is **closed** — finished, or deliberately abandoned.
    Raised by :func:`kodo.plan.create_plan` and deliberately **not** converted
    into a tool-result error: the engine re-raises it past every per-tool-call
    failure boundary (see ``runtime/_engine/_turns.py``) and the worker turns it
    into a ``kodo_crit`` notice and a stopped turn.

    This is the failure of *not acknowledging* the abandonment, not of wanting to
    re-plan. An agent that genuinely needs a different plan has a legal route —
    ``plan_step_forward`` with ``abandon_plan: true``, which closes the live plan
    on the record and leaves its unfinished tasks unfinished. Reaching this error
    means the agent replanned over open work without ever saying so, and nothing
    it does next can be trusted to follow a plan.
    """


class PlanTask(TypedDict):
    """One task in a plan: an identity, a label, and a derived status.

    Deliberately slim. The plan is a **progress ledger**, not a second copy of
    the planner's output — the agent executing the plan already holds the full
    task bodies (instructions, files, acceptance criteria) from the planner's own
    ``return_result``, and re-sending them on every ``get_plan`` call would cost
    tokens on every step to tell the caller what it already knows.

    ``id`` is the task's 1-based position, which is also its only identity: a
    plan is a plain ordered list and tasks are never inserted, removed or
    reordered once it is created.
    """

    id: int
    title: str
    status: str


class PlanState(TypedDict):
    """The whole current state of a session's plan — what ``get_plan`` returns.

    A plan is **closed** when it is either ``complete`` or ``abandoned``, and
    only a closed plan may be superseded by a new one (:func:`closed`). The two
    are kept as separate booleans rather than one overloaded flag because they
    mean opposite things to a reader: one says the work was done, the other says
    it deliberately was not.

    ``current_task`` is the ``id`` of the single ``in_progress`` task, or
    ``None`` — which is the case both before the first step and after the last
    one. ``complete`` is what distinguishes those two, so a reader must never
    infer "finished" from a missing current task.

    An abandoned plan keeps every task's status exactly as it was: unfinished
    work stays ``not_started``/``in_progress``. Abandoning closes the plan, it
    does not pretend the work happened.
    """

    created_by: str
    context: str
    tasks: list[PlanTask]
    current_task: int | None
    complete: bool
    abandoned: bool
    abandon_reason: str


def closed(plan: PlanState) -> bool:
    """Whether *plan* may be superseded by a new one.

    The single definition of the supersede precondition, so the store and any
    caller that wants to check first cannot disagree about it. A plan is closed
    once it is finished (``complete``) or deliberately dropped (``abandoned``);
    anything else is live work a new plan would silently discard.
    """
    return plan["complete"] or plan["abandoned"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_tasks(value: object) -> list[PlanTask]:
    """Coerce a planner's raw ``tasks`` array into well-formed :class:`PlanTask`s.

    Model-authored, so nothing is trusted: a non-list yields ``[]``, a non-dict
    element is skipped, and an element with no usable ``title`` is skipped too —
    a task the user cannot read is not a task, and keeping it would put an
    unnameable row in the widget and an unexplainable step in the ledger.

    Every other field the planner put on the task (``instructions``, ``files``,
    ``acceptance``, ``subagent``, …) is dropped here by design: see
    :class:`PlanTask`.

    Ids are assigned **here**, from position, and never read from the input — so
    a planner cannot hand back colliding or out-of-order ids, and the ids in the
    ledger always match the order the tasks will be executed in.
    """
    if not isinstance(value, list):
        return []
    tasks: list[PlanTask] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        tasks.append(
            PlanTask(
                id=len(tasks) + 1,
                title=title[:_MAX_TITLE_CHARS],
                status=STATUS_NOT_STARTED,
            )
        )
    return tasks


def derive_state(
    *,
    created_by: str,
    context: str,
    tasks: list[PlanTask],
    steps: int,
    abandoned: bool = False,
    abandon_reason: str = "",
) -> PlanState:
    """Project *steps* completed ``plan_step_forward`` calls onto *tasks*.

    The single definition of what a step means, used both when replaying a log
    and when reporting the result of the step that just happened.

    A plan starts with every task ``not_started`` — nothing is in progress until
    the executing agent says it is starting. From there each step advances the
    pointer by one:

    ====== ==========================================================
    steps  statuses (for three tasks)
    ====== ==========================================================
    0      ``not_started``, ``not_started``, ``not_started``
    1      ``in_progress``, ``not_started``, ``not_started``
    2      ``done``, ``in_progress``, ``not_started``
    3      ``done``, ``done``, ``in_progress``
    4      ``done``, ``done``, ``done`` — complete
    ====== ==========================================================

    So *n* tasks take *n+1* steps: the first starts task 1 without completing
    anything, and the last completes task *n* without starting anything. That
    asymmetry is deliberate — "the work has not begun" and "task 1 is underway"
    are different facts about a plan, and collapsing them would make a plan's
    first state a lie.

    *steps* beyond ``len(tasks) + 1`` are clamped, so a log that somehow grew an
    extra step still replays to a readable complete plan rather than an index
    error. Rejecting the extra step is :func:`kodo.plan.step_plan`'s job, and it
    does so before ever writing one.

    *abandoned* is passed through untouched: abandoning closes a plan without
    rewriting a single status, so an abandoned plan still shows exactly how far
    the work actually got. It therefore composes with any step count, including
    zero.
    """
    bounded = max(0, min(steps, len(tasks) + 1))
    done_count = max(0, bounded - 1)
    in_progress_index = bounded - 1 if 1 <= bounded <= len(tasks) else None
    projected: list[PlanTask] = []
    for index, task in enumerate(tasks):
        if index < done_count:
            status = STATUS_DONE
        elif index == in_progress_index:
            status = STATUS_IN_PROGRESS
        else:
            status = STATUS_NOT_STARTED
        projected.append(PlanTask(id=task["id"], title=task["title"], status=status))
    return PlanState(
        created_by=created_by,
        context=context,
        tasks=projected,
        current_task=(
            projected[in_progress_index]["id"] if in_progress_index is not None else None
        ),
        complete=bool(tasks) and bounded >= len(tasks) + 1,
        abandoned=abandoned,
        abandon_reason=abandon_reason,
    )


def plan_created_entry(
    *, created_by: str, context: str, tasks: list[PlanTask]
) -> dict[str, object]:
    """One ``plan_created`` log line — a whole plan, superseding any before it."""
    return {
        "type": ENTRY_PLAN_CREATED,
        "timestamp": _now(),
        "created_by": created_by,
        "context": context,
        "tasks": [dict(task) for task in tasks],
    }


def plan_step_entry() -> dict[str, object]:
    """One ``plan_step`` log line — a pointer move, carrying no status of its own."""
    return {"type": ENTRY_PLAN_STEP, "timestamp": _now()}


def plan_abandoned_entry(reason: str) -> dict[str, object]:
    """One ``plan_abandoned`` log line — closes the live plan without finishing it.

    The deliberate escape from the one-plan-per-session rule: a plan the work has
    moved on from is *closed*, not stepped to the end. It records no status, for
    the same reason :func:`plan_step_entry` does not — the unfinished tasks stay
    unfinished, and the log keeps saying how far the work really got.
    """
    return {"type": ENTRY_PLAN_ABANDONED, "timestamp": _now(), "reason": reason}
