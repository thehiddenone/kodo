"""Unit tests for :mod:`kodo.plan` — the per-session work plan.

The rules these cover are the ones the whole design rests on (doc/PLANNING.md):
a plan starts with nothing in progress, a step is a pointer move rather than a
recorded status, only a **complete** plan may be superseded, and a task can never
fail. Statuses are always derived from the log, so nothing here asserts a stored
status — every expectation is read back through :func:`read_plan`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.plan import (
    PLAN_CONTEXT_FIELD,
    PLAN_OUTPUT_FIELDS,
    PLAN_TASKS_FIELD,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NOT_STARTED,
    PlanConflictError,
    abandon_plan,
    closed,
    create_plan,
    derive_state,
    normalize_tasks,
    plan_log_path,
    read_plan,
    step_plan,
)

_THREE = [{"title": "Toolchain setup"}, {"title": "Extract parser"}, {"title": "Rewire CLI"}]


def _statuses(plan_dir: Path) -> list[str]:
    plan = read_plan(plan_dir)
    assert plan is not None
    return [task["status"] for task in plan["tasks"]]


def _make(plan_dir: Path, tasks: object = None, *, by: str = "planner", context: str = "ctx"):
    return create_plan(plan_dir, created_by=by, context=context, tasks=tasks or _THREE)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def test_a_session_starts_with_no_plan(tmp_path: Path) -> None:
    """No plan is the ordinary answer, not an error — most sessions never run a planner."""
    assert read_plan(tmp_path) is None


def test_creation_leaves_every_task_not_started(tmp_path: Path) -> None:
    """A plan begins with nothing in progress: "not begun" and "task 1 underway"
    are different facts, and collapsing them would make a plan's first state a lie."""
    plan = _make(tmp_path)
    assert plan is not None
    assert [t["status"] for t in plan["tasks"]] == [STATUS_NOT_STARTED] * 3
    assert plan["current_task"] is None
    assert plan["complete"] is False
    assert plan["created_by"] == "planner"
    assert plan["context"] == "ctx"


def test_ids_are_positional_and_assigned_by_the_store(tmp_path: Path) -> None:
    """A planner cannot choose its tasks' ids — colliding or out-of-order ones
    would desynchronize the ledger from the execution order."""
    plan = _make(tmp_path, [{"title": "a", "id": 99}, {"title": "b", "id": 99}])
    assert plan is not None
    assert [t["id"] for t in plan["tasks"]] == [1, 2]


def test_only_the_title_survives_into_the_ledger(tmp_path: Path) -> None:
    """The plan is a progress ledger, not a second copy of the planner's output:
    the caller already holds the task bodies from the planner's own result."""
    plan = _make(
        tmp_path,
        [{"title": "a", "subagent": "developer", "instructions": "long", "acceptance": "x"}],
    )
    assert plan is not None
    assert set(plan["tasks"][0]) == {"id", "title", "status"}


def test_empty_and_unusable_task_lists_create_no_plan(tmp_path: Path) -> None:
    """A planner that found nothing to sequence leaves the session with no plan —
    so a later planner call can still create the first one without a conflict."""
    assert create_plan(tmp_path, created_by="planner", context="c", tasks=[]) is None
    assert create_plan(tmp_path, created_by="planner", context="c", tasks=None) is None
    assert create_plan(tmp_path, created_by="planner", context="c", tasks="nope") is None
    # Elements with no usable title are dropped, not kept as blank rows.
    assert create_plan(tmp_path, created_by="planner", context="c", tasks=[{}, "x", 5]) is None
    assert read_plan(tmp_path) is None


def test_titles_are_truncated_rather_than_rejected(tmp_path: Path) -> None:
    """A runaway model-authored title would break the widget; the task still counts."""
    plan = _make(tmp_path, [{"title": "x" * 500}])
    assert plan is not None
    assert len(plan["tasks"][0]["title"]) == 200


# ---------------------------------------------------------------------------
# Stepping
# ---------------------------------------------------------------------------


def test_n_tasks_take_n_plus_one_steps(tmp_path: Path) -> None:
    """The first step starts task 1 without completing anything; the last completes
    task n without starting anything (doc/PLANNING.md §3)."""
    _make(tmp_path)
    assert _statuses(tmp_path) == [STATUS_NOT_STARTED] * 3
    step_plan(tmp_path)
    assert _statuses(tmp_path) == [STATUS_IN_PROGRESS, STATUS_NOT_STARTED, STATUS_NOT_STARTED]
    step_plan(tmp_path)
    assert _statuses(tmp_path) == [STATUS_DONE, STATUS_IN_PROGRESS, STATUS_NOT_STARTED]
    step_plan(tmp_path)
    assert _statuses(tmp_path) == [STATUS_DONE, STATUS_DONE, STATUS_IN_PROGRESS]
    final = step_plan(tmp_path)
    assert _statuses(tmp_path) == [STATUS_DONE] * 3
    assert final["complete"] is True
    assert final["current_task"] is None


def test_current_task_tracks_the_single_in_progress_task(tmp_path: Path) -> None:
    _make(tmp_path)
    assert step_plan(tmp_path)["current_task"] == 1
    assert step_plan(tmp_path)["current_task"] == 2
    assert step_plan(tmp_path)["current_task"] == 3


def test_stepping_with_no_plan_is_a_soft_failure(tmp_path: Path) -> None:
    """A ValueError the caller reports to the model — nothing was written."""
    with pytest.raises(ValueError, match="no plan"):
        step_plan(tmp_path)
    assert not plan_log_path(tmp_path).exists()


def test_stepping_past_the_end_is_a_soft_failure_and_writes_nothing(tmp_path: Path) -> None:
    """Unlike a plan conflict this costs nothing — but it must not grow the log,
    or the clamp in derive_state would be the only thing keeping the plan readable."""
    _make(tmp_path, [{"title": "only"}])
    step_plan(tmp_path)
    step_plan(tmp_path)
    lines_before = plan_log_path(tmp_path).read_text(encoding="utf-8").count("\n")
    with pytest.raises(ValueError, match="already complete"):
        step_plan(tmp_path)
    assert plan_log_path(tmp_path).read_text(encoding="utf-8").count("\n") == lines_before


def test_no_status_is_ever_written_to_the_log(tmp_path: Path) -> None:
    """A step is a pointer move. Because no line names a status, there is no way to
    write a ledger that contradicts itself (e.g. task 3 done while task 2 is not)."""
    _make(tmp_path)
    step_plan(tmp_path)
    step_plan(tmp_path)
    entries = [
        json.loads(line)
        for line in plan_log_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    steps = [e for e in entries if e["type"] == "plan_step"]
    assert len(steps) == 2
    assert all(set(e) == {"type", "timestamp"} for e in steps)


# ---------------------------------------------------------------------------
# One plan per session
# ---------------------------------------------------------------------------


def test_superseding_an_unfinished_plan_raises(tmp_path: Path) -> None:
    """The one hard failure in the feature — the engine turns it into a stopped session."""
    _make(tmp_path)
    step_plan(tmp_path)
    with pytest.raises(PlanConflictError) as excinfo:
        _make(tmp_path, [{"title": "replacement"}])
    # The message names what was still outstanding, so the user can see what
    # the agent walked away from.
    assert "Extract parser" in str(excinfo.value)
    # And nothing changed: the live plan is still the original.
    plan = read_plan(tmp_path)
    assert plan is not None
    assert [t["title"] for t in plan["tasks"]] == [t["title"] for t in _THREE]


def test_a_complete_plan_can_be_superseded(tmp_path: Path) -> None:
    _make(tmp_path, [{"title": "only"}])
    step_plan(tmp_path)
    step_plan(tmp_path)
    replacement = _make(tmp_path, [{"title": "new a"}, {"title": "new b"}], by="planner2")
    assert replacement is not None
    assert replacement["created_by"] == "planner2"
    # The new plan starts fresh — the superseded plan's steps do not carry over,
    # which is what makes replay count steps from the live plan_created line only.
    assert [t["status"] for t in replacement["tasks"]] == [STATUS_NOT_STARTED] * 2
    assert _statuses(tmp_path) == [STATUS_NOT_STARTED] * 2


def test_an_unusable_replacement_does_not_disturb_a_complete_plan(tmp_path: Path) -> None:
    """No tasks means no plan was created — the finished one stays readable."""
    _make(tmp_path, [{"title": "only"}])
    step_plan(tmp_path)
    step_plan(tmp_path)
    assert create_plan(tmp_path, created_by="planner", context="c", tasks=[]) is None
    plan = read_plan(tmp_path)
    assert plan is not None
    assert plan["complete"] is True


# ---------------------------------------------------------------------------
# Replay and derivation
# ---------------------------------------------------------------------------


def test_state_is_always_a_replay_of_the_log(tmp_path: Path) -> None:
    """There is no index and no in-memory state, so a fresh read of the same
    directory must agree exactly with what the last write returned."""
    _make(tmp_path)
    stepped = step_plan(tmp_path)
    assert read_plan(tmp_path) == stepped


def test_derive_state_clamps_an_overlong_step_count() -> None:
    """A log that somehow grew an extra step still replays to a readable complete
    plan rather than an index error — rejecting the extra step is step_plan's job."""
    tasks = normalize_tasks([{"title": "a"}, {"title": "b"}])
    state = derive_state(created_by="p", context="c", tasks=tasks, steps=99)
    assert [t["status"] for t in state["tasks"]] == [STATUS_DONE, STATUS_DONE]
    assert state["complete"] is True
    assert state["current_task"] is None


def test_derive_state_on_an_empty_task_list_is_never_complete() -> None:
    """Nothing is a finished plan — guarding the supersede rule against a plan
    that would report itself done without ever having held any work."""
    state = derive_state(created_by="p", context="c", tasks=[], steps=5)
    assert state["complete"] is False


def test_the_planner_output_contract_is_the_two_field_names() -> None:
    """The engine reads exactly these two fields off a `planner: true` result, and
    AgentRegistry validates a planner's output_schema against this same set."""
    assert {PLAN_TASKS_FIELD, PLAN_CONTEXT_FIELD} == PLAN_OUTPUT_FIELDS
    assert (PLAN_TASKS_FIELD, PLAN_CONTEXT_FIELD) == ("tasks", "codebase_context")


# ---------------------------------------------------------------------------
# Abandoning — the legal way out of an unfinished plan
# ---------------------------------------------------------------------------


def test_abandoning_closes_a_plan_without_rewriting_a_status(tmp_path: Path) -> None:
    """The whole point: abandoning records that the work stopped, never that it
    happened. Stepping to the end would have been the lie this avoids."""
    _make(tmp_path)
    step_plan(tmp_path)
    step_plan(tmp_path)
    before = _statuses(tmp_path)
    plan = abandon_plan(tmp_path, "user redirected to the import bug")
    assert [t["status"] for t in plan["tasks"]] == before
    assert plan["abandoned"] is True
    assert plan["complete"] is False
    assert plan["abandon_reason"] == "user redirected to the import bug"
    assert closed(plan) is True


def test_an_abandoned_plan_can_be_superseded(tmp_path: Path) -> None:
    """Closing the trap: before this existed an agent whose work legitimately
    moved on had no legal move at all."""
    _make(tmp_path)
    step_plan(tmp_path)
    with pytest.raises(PlanConflictError):
        _make(tmp_path, [{"title": "replacement"}])
    abandon_plan(tmp_path, "the design changed")
    replacement = _make(tmp_path, [{"title": "new a"}, {"title": "new b"}])
    assert replacement is not None
    assert [t["title"] for t in replacement["tasks"]] == ["new a", "new b"]
    # The replacement is live and clean — the abandonment belonged to the plan it
    # replaced, which is what makes replay count from the live plan_created line.
    assert replacement["abandoned"] is False
    assert [t["status"] for t in replacement["tasks"]] == [STATUS_NOT_STARTED] * 2


def test_the_conflict_message_names_the_way_out(tmp_path: Path) -> None:
    """A guard rail the agent cannot see past is one it will keep walking into."""
    _make(tmp_path)
    step_plan(tmp_path)
    with pytest.raises(PlanConflictError, match="abandon"):
        _make(tmp_path, [{"title": "replacement"}])


def test_an_abandoned_plan_cannot_be_stepped(tmp_path: Path) -> None:
    _make(tmp_path)
    abandon_plan(tmp_path, "dropped")
    with pytest.raises(ValueError, match="abandoned"):
        step_plan(tmp_path)


def test_abandoning_twice_and_abandoning_nothing_are_soft_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nothing to abandon"):
        abandon_plan(tmp_path, "no plan here")
    _make(tmp_path)
    abandon_plan(tmp_path, "once")
    with pytest.raises(ValueError, match="already abandoned"):
        abandon_plan(tmp_path, "twice")


def test_a_complete_plan_cannot_be_abandoned(tmp_path: Path) -> None:
    """Nothing to close — it can already be superseded, so the call is a
    misunderstanding rather than a breach of the rule."""
    _make(tmp_path, [{"title": "only"}])
    step_plan(tmp_path)
    step_plan(tmp_path)
    with pytest.raises(ValueError, match="complete"):
        abandon_plan(tmp_path, "pointless")


def test_abandonment_survives_a_replay(tmp_path: Path) -> None:
    _make(tmp_path)
    step_plan(tmp_path)
    abandoned = abandon_plan(tmp_path, "reason that must persist")
    assert read_plan(tmp_path) == abandoned


def test_closed_is_the_supersede_precondition() -> None:
    """One definition, so the store and any caller cannot disagree about it."""
    tasks = normalize_tasks([{"title": "a"}])
    live = derive_state(created_by="p", context="c", tasks=tasks, steps=0)
    assert closed(live) is False
    assert closed(derive_state(created_by="p", context="c", tasks=tasks, steps=2)) is True
    assert closed({**live, "abandoned": True}) is True  # type: ignore[typeddict-item]
