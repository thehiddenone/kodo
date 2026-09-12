# Planning — the session's work plan, and how to write a planner sub-agent

> Reference: [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) — the frontmatter and
> spec mechanics this builds on. [TOOLS.md](TOOLS.md) — the tool catalogue.

**Status:** implemented.

A **plan** is an ordered list of tasks plus the development context that produced
it. One sub-agent declares `planner: true`; the engine reads that agent's result
and records the plan. From then on the agent that commissioned the plan tracks it
through two tools — `get_plan` reads it, `plan_step_forward` advances it — and the
user watches a rendered widget of the same state in the chat feed.

Before this existed, the Problem Solver held its plan in its own prose and
re-posted a hand-written checklist in a `<kodo_info>` callout after every step.
That had no state anywhere: callout text is stripped from the model's own history
(`kodo/llms/_sanitize.py`), so the agent had to keep a private copy, a compaction
could lose it, and nothing but the model's diligence connected the list the user
read to the work actually being done.

---

## 1. The shape of it

```
      planner sub-agent                  engine                         session
      (planner: true)
            │
            │  return_result
            │  { tasks, codebase_context, … }
            ├──────────────────────────────►  _initialize_plan
            │                                 kodo.plan.create_plan
            │                                      │
            │                                      ├──► <session-dir>/plan/plan.jsonl
            │                                      └──► plan.state event ──► widget
            │
   commissioning agent
            │  get_plan               ──►  read_plan        ──► JSON result (to the LLM)
            │                                               └─► plan.state event (to the user)
            │  plan_step_forward      ──►  step_plan         ──► JSON result (to the LLM)
            │                                               └─► plan.state event (to the user)
            │  plan_step_forward      ──►  abandon_plan     ──► JSON result (to the LLM)
            │    {abandon_plan: true}                       └─► plan.state event (to the user)
```

Two properties are worth stating outright, because both are easy to get wrong:

**No model ever writes a plan.** A planner returns a result like any other
sub-agent; the engine is what turns it into a plan. There is no "create plan"
tool, and `plan_step_forward` cannot change a task's title or insert one.

**The user and the model are shown the same payload, by different routes.** The
model's copy is the tool's JSON result. The user's copy is the `plan.state` event,
rendered as a widget. They are the same `PlanState`, emitted twice — so they
cannot disagree. This is *unlike* `review.findings`, where the information itself
is user-only; here only the rendering is.

---

## 2. Writing a planner sub-agent

Two things, and the registry checks that they line up at load time.

**1. Declare the flag in frontmatter.**

```yaml
---
name: planner
display_name: Planner
standalone: true
planner: true          # ← this
capability: high
tools:
  - read_file
  - find_files
---
```

**2. Declare the two output fields in the agent's `SubAgentSpec`.**

```python
PLANNER: SubAgentSpec = SubAgentSpec(
    name="planner",
    input_schema={...},
    output_schema={
        "type": "object",
        "properties": {
            "tasks": {                      # ← the ordered plan
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, ...},
                    "required": ["title", ...],
                },
            },
            "codebase_context": {"type": "string"},   # ← the development context
            ...
        },
        "required": [..., "codebase_context", "tasks"],
    },
)
```

`tasks` and `codebase_context` are **fixed names** — the contract, held in
`kodo.plan.PLAN_OUTPUT_FIELDS`. They are not configurable per agent, for the same
reason `paths` is not: they *are* the planner role, and a second planner should
conform to the same two names rather than teach the engine a third vocabulary.

`AgentRegistry.__validate_planner` refuses to load a `planner: true` agent that
gets either half wrong, so the two sides — frontmatter and spec, in different
files — cannot silently drift apart:

| the spec does this | and the registry |
| --- | --- |
| omits `tasks` or `codebase_context` | refuses |
| declares `tasks` as anything but an array | refuses |
| declares `tasks` items as anything but objects | refuses |
| declares task items with no `title` property | refuses |
| declares `codebase_context` as anything but a string | refuses |
| has no `SubAgentSpec` at all | refuses |

**The type checks are not pedantry.** `normalize_output` validates that a
result's required fields are *present*, never that they hold the declared type
(§8). A planner whose schema says `tasks` is a string therefore passes compliance
at run time, reaches `normalize_tasks`, has every element dropped, and creates
**no plan** — while the agent holds a result that looks like it worked. Load time
is the only place that failure is cheap.

**The `required` lists are checked too — by CI, not by the registry.**
`test/test_agents.py::test_every_planner_frontmatter_is_backed_by_the_plan_output_contract`
reads the raw frontmatter of every shipped agent file and holds each one that
declares `planner: true` to the whole table above, *plus* two things
`__validate_planner` does not look at: `tasks` and `codebase_context` must appear
in the output schema's `required` list, and `title` in each task item's. An
optional field is the one remaining way to get a silently plan-less planner —
`normalize_output` backfills and flags only fields that are *required*, so a
result that simply omits `tasks` is fully compliant, creates no plan, and raises
no `plan_issue` either (§8): zero reported tasks is a legitimate
`plan_warranted: false` answer, and nothing downstream can tell the two apart.

The scan reads the `.md` rather than the loaded `SubAgent` on purpose — it is
checking what an author wrote — so a sibling test asserts, for every shipped
file, that the raw `planner:` line and `SubAgent.planner` agree. Without it a
value the loader does not read as true (`planner: True.`) would exempt an agent
from the contract instead of failing it.

### What the engine reads off each task

Only `title`. The plan is a **progress ledger**, not a second copy of the
planner's output: the agent executing the plan already holds the full task bodies
(`instructions`, `files`, `acceptance`, whichever sub-agent to run) from the
planner's own `return_result`, and re-sending them on every `get_plan` would cost
tokens on every step to say what the caller already knows. Everything except
`title` is dropped by `normalize_tasks`.

Task **ids are assigned by the store**, from position, and never read from the
planner's output — so a planner cannot hand back colliding or out-of-order ids,
and the ids always match the execution order.

### What else a planner may return

Anything. The extra fields pass through to the caller untouched; the engine looks
at exactly two. In particular the shipped `planner` also returns
`plan_warranted` / `reason`, which the engine **deliberately does not read** —
"it gave me no tasks" is the same fact as `plan_warranted: false`, and reading
the flag would put one agent's private vocabulary into the engine.

### What is *not* required

- `planner:` is orthogonal to `critic:`, `user_review:` and `standalone:`. The
  plan is initialized from whatever result the declared flow finally produces.
- A planner needs no `produces`/`consumes` roles. A plan is not a work product;
  it is not reviewed, not written to disk in the project, and not resolved as
  anyone's input.
- A critic **may not** declare `planner:` — it returns a review verdict, not a
  plan, so there would be no tasks to read. `load_agent` rejects it.

---

## 3. The task model

Three statuses, and **no failure state**:

| status | meaning |
| --- | --- |
| `not_started` | not begun |
| `in_progress` | the task currently being worked |
| `done` | finished |

A task cannot fail. A plan only moves forward: work that turns out to be
misjudged is handled by **closing the whole plan** — finishing it, or abandoning it
(§4) — and commissioning a new one, never by annotating a task. An agent that
cannot complete a step is expected to raise it with the user, not to step past it.

### Statuses are derived, never stored

A `plan_step` log line carries no payload at all — a step is a *pointer move*.
Current statuses come from counting the steps recorded after the live plan
(`derive_state`). Nothing can write a ledger that contradicts itself, because no
line ever names a status.

### *n* tasks take *n+1* steps

| steps | statuses (three tasks) |
| --- | --- |
| 0 | `not_started`, `not_started`, `not_started` |
| 1 | `in_progress`, `not_started`, `not_started` |
| 2 | `done`, `in_progress`, `not_started` |
| 3 | `done`, `done`, `in_progress` |
| 4 | `done`, `done`, `done` — **complete** |

The first step starts task 1 without completing anything; the last completes
task *n* without starting anything. That asymmetry is deliberate: "the work has
not begun" and "task 1 is underway" are different facts about a plan, and
collapsing them would make a plan's first state a lie.

Consequently `current_task` is `null` in **two** situations — before the first
step and after the last. `complete` is what distinguishes them, so never infer
"finished" from a missing current task.

---

## 4. One plan per session, the escape, and the one hard failure

A session holds **at most one plan**, and a new plan may supersede it only once it
is **closed**. Closed means one of two things, and `kodo.plan.closed()` is the one
definition both the store and any caller read:

| | how | task statuses |
| --- | --- | --- |
| **complete** | stepped to the end | every one `done` |
| **abandoned** | `plan_step_forward {abandon_plan: true, reason: …}` | **untouched** |

### Abandoning — the legal way out

An agent whose work legitimately moves on — the user redirected it, or the plan
rested on something that turned out to be wrong — closes the plan with
`abandon_plan: true` and a `reason`, then runs a planner for the replacement.

The crucial property: **abandoning changes no status.** Unfinished tasks stay
`not_started`/`in_progress`, so the record keeps saying how far the work actually
got. That is the whole reason this is a distinct operation rather than "step to
the end": stepping would record work nobody did as `done`, which is precisely what
the derived-status design (§3) exists to make unwriteable. A `plan_abandoned` log
line carries the reason and no status, exactly as `plan_step` carries none.

Without this route an agent in that position had **no legal move at all** — and
would hit the hard failure below on every attempt, turn after turn.

### The hard failure

A planner returning a plan while the live one is **still open** ends the turn:

- `kodo.plan.create_plan` raises `PlanConflictError`.
- It propagates untouched through *both* per-tool-call failure boundaries in
  `runtime/_engine/_turns.py` — it is the third member of that re-raise tuple,
  alongside `asyncio.CancelledError` and `UnrecoverableError`. Turning it into an
  `{"error": …}` tool result the agent could "deal with" would defeat the rule.
- `_run_worker` catches it, emits `plan.conflict_critical` (a red `<kodo_crit>`
  callout in the webview) and sets the session phase to `stopped`.

What this means precisely: **the turn ends and the phase goes to `stopped`**. The
worker is not dead — the `except` sits inside its `while` loop with no `break`, so
the user can send another prompt and it will be served. But the plan is still
open, so an agent that replans again hits the same wall; the escape is to abandon
first. The conflict message says so explicitly, because a guard rail an agent
cannot see past is one it will keep walking into.

The severity is about *acknowledgement*, not about wanting to re-plan. An agent
that abandons first has said, on the record and with a reason, that it is dropping
committed work. One that simply replans has not, and every step it takes
afterwards runs against a plan nobody is tracking.

### The soft failures, by contrast

These return an ordinary `{"error": …}` tool result and change nothing:

- `plan_step_forward` with **no plan** in the session.
- `plan_step_forward` on an **already-closed** plan (complete, or abandoned).
- `abandon_plan: true` with no plan, on an already-abandoned plan, or on a
  complete one (nothing left to close — it can already be superseded).

All cost nothing and name exactly what the agent got wrong, so it can act on the
answer.

### An unusable planner result creates nothing

A planner whose `tasks` hold nothing usable — an empty array, or only elements
with no `title`, or an escalation instead of a plan — leaves the session with
**no plan at all** rather than an empty one. That matters for the conflict rule:
a later planner call is then free to create the first real plan without tripping
it. When the planner *reported* tasks that turned out unusable, that is a reported
failure rather than a quiet one — see §8.

## 5. Storage

`<session-dir>/plan/plan.jsonl` — **session**-scoped, like
[findings](FINDINGS.md) and unlike the project-scoped work-product ledger. A plan
is a fact about one session's attempt at some work; two sessions over the same
repo hold entirely separate plans.

Three append-only entry types:

```jsonl
{"type":"plan_created","timestamp":"…","created_by":"planner","context":"…","tasks":[{"id":1,"title":"…","status":"not_started"}]}
{"type":"plan_step","timestamp":"…"}
{"type":"plan_step","timestamp":"…"}
{"type":"plan_abandoned","timestamp":"…","reason":"user redirected to the import bug"}
```

Replay takes the **last** `plan_created`, counts the `plan_step` lines after it,
and notes whether a `plan_abandoned` followed. Every earlier plan was superseded,
and a superseded plan was closed when it was replaced, so nothing is lost by not
projecting it — and a superseded plan's steps and abandonment correctly do not
carry into its replacement.

Neither `plan_step` nor `plan_abandoned` records a status. That is the property
§3 rests on, and it is why abandoning cannot corrupt a ledger: there is nothing
for it to write.

`kodo.plan` is a leaf package of plain functions with no in-memory index: current
state is always a replay of the log. The engine derives the directory in one
place, `_plan_dir()` — the exact sibling of `_findings_dir()` — and injects it
into each run's `ToolContext`, because inside a sub-agent run `session_id` holds
the *subsession* id and no tool could derive the session's own store path.

---

## 6. The two tools

Both are **argument-free**: a session has exactly one plan, so there is nothing
to name, and a step is always "the next one".

| | `get_plan` | `plan_step_forward` | `plan_step_forward {abandon_plan: true}` |
| --- | --- | --- | --- |
| changes the plan | no | advances it one step | closes it unfinished |
| no plan exists | `{"plan": null}` — a normal answer | `{"error": …}` | `{"error": …}` |
| plan closed | reports it | `{"error": …}` | `{"error": …}` |
| emits a widget | yes (`reason: "read"`) | yes (`reason: "step"`) | yes (`reason: "abandoned"`) |

Abandoning lives on `plan_step_forward` rather than in a tool of its own because
it is the same decision from the other side — this plan moves on, or this plan
stops — so an agent that can advance a plan already has the means to end one
honestly. It takes an optional `reason`, which is the only explanation the log and
the user's widget will ever carry.

Both carry `output_visibility={}` — hidden — on purpose: the user reads the plan
as the widget, not as the tool card's JSON, and showing both would print the same
plan twice, once in a shape nobody wants to read.

### The widget

`plan.state` (persisted as a `plan_state` marker) carries the whole `PlanState`
plus a `reason` of `created` / `read` / `step`. It fires three times over a
plan's life: on creation, and on each tool call.

Each one is a **plain append** to the feed, like a findings table — so the feed
carries a running record of the plan as it advanced rather than only its latest
state. A reload replays each snapshot with the statuses it was emitted with,
deliberately **not** restamped with the plan's state today.

`reason` never reaches a model; it exists only so the widget can title itself
("Plan", "Plan progress", "Plan complete", "Plan abandoned" — a closed plan's
state outranks the reason). A creation may also carry an `issue` string (§8),
rendered as a warning on that card. The marker carries no `role`, so the
widget itself is never rebuilt into any agent's message history — the model's copy
comes from its tool result, the user's from here, and neither path feeds the
other.

Client side: `webview/PlanView.tsx` renders it, `reducer.ts`'s `readPlanState`
converts both the live action and the replayed history entry (one reader, so they
cannot drift), and `session/agent-event-translation.ts` reshapes the wire's
snake_case. The client **never** computes a status — they arrive derived.

---

## 7. Who gets the tools

Today: the Problem Solver only, granted in `agent_problem_solver.md`'s `tools:`
list. Nothing is special-cased — the tools read the plan out of `ToolContext`, so
any agent whose frontmatter lists them works.

Two things to think about before granting `get_plan` more widely: a sub-agent
reading the whole plan pays tokens for context it may not need, and seeing later
tasks can invite work outside its own step. `plan_step_forward` belongs with
whoever *orchestrates* the plan, and giving it to two agents at once would mean
two writers racing a single pointer.

---

## 8. When a planner's result is wrong

Three shapes of wrong, each handled in a different place.

### The schema repaired it (structurally wrong)

A result missing a required field, carrying an undeclared key, or not an object at
all is repaired by `normalize_output`: missing required fields become `""`,
undeclared keys are dropped, `schema_compliance: false` is set, and the engine
emits `tool.incompliant` — so the user gets a message box naming the tool. A
`tasks` of `""` then yields no plan. Visible, and handled.

Missing *only* `codebase_context` is harmless: it becomes `""`, the plan is
created, and the widget just hides the notes toggle.

### The types were wrong (compliant, but unusable)

**`normalize_output` never type-checks.** It validates presence only. So:

| planner returned | `schema_compliance` | user warned by `tool.incompliant` |
| --- | --- | --- |
| `tasks: "1. do X 2. do Y"` | `true` | no |
| `tasks: ["step one", "step two"]` | `true` | no |
| omits `tasks` | `false` | yes |

The first two are "compliant" and yet `normalize_tasks` drops every element. Left
alone, the caller would hold a healthy-looking `tasks` array while `get_plan`
insisted there was no plan and `plan_step_forward` answered *"There is no plan in
this session"* — a flat contradiction from where the agent is standing.

So `_initialize_plan` compares what was **reported** (`_reported_task_count` — a
list's length, or 1 for a non-empty string) against what became a task, and any
shortfall is reported:

| | plan created? | caller told | user told |
| --- | --- | --- | --- |
| reported 0 tasks | no | nothing — a legitimate answer | nothing |
| reported *n*, 0 usable | **no** | `plan_issue` on the result | — (no widget to show) |
| reported *n*, some usable | yes, short | `plan_issue` on the result | `issue` on the widget |

`plan_issue` is an engine-owned key added to the spawn result, naming the
shortfall and the one requirement a task has — so the agent can re-invoke the
planner instead of discovering the problem later. Both cases also log at WARNING.

A `tool.incompliant` event is deliberately **not** reused here: its client wording
is specifically about schema *repair*, and nothing was repaired — the plan was
discarded. The partial case puts its warning on the plan widget instead, which is
the card actually missing the tasks.

The **declaration** behind this failure is caught far earlier, at load time (§2).

### The agent was wrong (a plan it should not have made)

Not a schema problem. See §4: abandon the plan with a reason, then re-plan.

---

## 9. Where the code is

| concern | file |
| --- | --- |
| records, statuses, `derive_state`, `closed`, `PlanConflictError` | `kodo/plan/_records.py` |
| log append/replay, `create_plan`, `step_plan`, `abandon_plan` | `kodo/plan/_store.py` |
| the `planner:` frontmatter flag | `kodo/subagents/_loader.py` |
| load-time contract check | `kodo/subagents/_registry.py` (`__validate_planner`) |
| the engine hook | `kodo/runtime/_engine/_subagents.py` (`_is_planner`, `_initialize_plan`, `_plan_dir`, `_reported_task_count`, `_plan_issue`) |
| `PlanConflictError` propagation | `kodo/runtime/_engine/_turns.py` |
| session stop | `kodo/runtime/_engine/_worker.py` |
| events | `kodo/runtime/_engine/_events.py`, `kodo/transport/_messages.py` |
| history replay | `kodo/runtime/_engine/_history.py` |
| tool specs | `kodo/toolspecs/_get_plan.py`, `_plan_step_forward.py` |
| tool handlers | `kodo/tools/_get_plan.py`, `_plan_step_forward.py` |
| widget | `kodo-vsix/src/webview/PlanView.tsx` |

Tests: `test/test_plan.py` (the store, the status rule, the abandon lifecycle),
`test/test_agents.py` (the flag and the load-time contract — names, types, and
the CI-only `required` scan over every shipped agent's frontmatter),
`test/test_engine_subagents.py` (the hook, the conflict, the `plan_issue`
reporting), `test/test_tools_compliance.py` (every tool envelope, step and
abandon), `kodo-vsix/src/test/reducer.test.ts` (live vs. replayed widget,
abandoned and flagged).
