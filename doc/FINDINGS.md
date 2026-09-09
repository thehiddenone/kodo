# Findings — the shared author/critic backlog

> Reference: [GUIDED_DEV_MODE.md](GUIDED_DEV_MODE.md) — the pipeline this backlog serves.

**Status:** implemented.

A **finding** is one defect a critic raised against one document, with an
identity that survives the round it was raised in. Findings live in the engine,
in a per-session append-only log; both halves of an author/critic loop reach
them through the same `get_findings` tool, and the **critic alone** changes a
finding's state, through its own `return_result`.

This replaces the previous *concerns* mechanism, in which a critic's rejection
list was rebuilt from scratch every round, had no identity, no state, and no
storage beyond one line of the reviewed document's `.jsonl` log.

---

## 1. Why

The old loop (`_run_review_loop`) worked like this: spawn the author → spawn the
critic → the critic returns `{path, accept, concerns[]}` → the engine appends a
`feedback` entry to the document's evolution log → if rejected, re-spawn the
author with the concern list **rendered as markdown into its `instructions`**.

Four things were wrong with it.

1. **No identity.** Round 3's concern list is a *different list* from round 2's.
   Nothing could answer "is this the same problem the critic already raised, or
   a new one?" — so nothing could answer "did this round make progress?"
   either. The engine's stand-in was `not_converging`: stop when the concern
   *count* fails to drop. A round that fixed two problems and found two new ones
   looked identical to a round that did nothing.

2. **No state.** A concern existed only while it was being restated. A critic
   that forgot to re-list a real problem silently dropped it; a critic that
   re-listed a fixed one silently reopened it. Neither was detectable.

3. **The critic had no memory.** Every critic prompt claimed *"Your prior
   findings stay in context"* — and every one of them was wrong. Each round
   spawns a **fresh subsession** (`_spawn_subagent` → new id, `messages=[seed]`),
   so a critic has never once seen its own prior round. The prompts' whole
   "Consistency Across Iterations" section rested on context that did not exist.
   `get_findings` is what finally makes that claim true.

4. **Two different prompts for one job.** Round 1 sent the caller's
   `instructions`; round N sent `instructions` + a generated
   `## Concerns from review` block. Every author prompt had to describe both
   shapes, and a first pass read differently from a second one.

## 2. The model

```
┌─ session  ~/.kodo/sessions/<session-id>/findings/ ─────────────────────────┐
│                                                                             │
│  <project>/coder/LEADERBOARD.jsonl        ← keyed on the WORK PRODUCT       │
│    {"type":"finding","id":"proj_coder_LEADERBOARD_leaderboard.py_42",       │
│     "kind":"interface_drift","description":"…",                             │
│     "locations":[{"path":"proj/src/leaderboard.py","first_line":42,…},      │
│                  {"path":"proj/src/app_flow.py","first_line":118,…}]}       │
│    {"type":"review_round","reviewer":"code_critic",…}                       │
│    {"type":"finding","id":"proj_coder_LEADERBOARD_leaderboard.py_42",       │
│     "state":"fixed"}                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
        ▲                                    ▲
        │ get_findings (read)                │ return_result (write)
        │                                    │
   ┌────┴─────┐                        ┌─────┴──────┐
   │  author  │                        │   critic   │
   └──────────┘                        └────────────┘
```

**A finding**:

| Field | Meaning |
| --- | --- |
| `id` | Engine-minted, `<project>_<agent>[_<responsibility>]_<file>_<line>`, unique session-wide. |
| `kind` | Category, from the reviewing critic's own vocabulary section. |
| `description` | Plain terse English: what's wrong and the concrete fix. |
| `locations` | **A list** of `{path, first_line, last_line, excerpt}` — every place this one problem appears. |
| `state` | `outstanding` or `fixed`. |

The field names are deliberately the ones the retired `concern_item` shape
already used (`kind`/`description`, and `excerpt` inside a location), so the
critics' existing vocabulary sections did not have to be rewritten around new
words.

### The unit is a work product, not a document (2026-09-04)

The backlog is keyed on a **work product** — every file one
`run_subagent_<author>` review loop wrote (`kodo.workproducts`) — rather than on
one document. The original one-file model came from the first authors each
writing exactly one; a coder does not, and a feature spanning five files has to
land in one go or the build breaks. So the reviewable unit is the thing that
builds, and a critic sees the whole set.

That is also why `locations` is a **list**: the defect a file-by-file review
structurally cannot catch is the one *between* files — a function defined in one
and mis-called in another. That is **one** finding with two locations, never
two findings. Filing it twice makes the backlog lie about how much is wrong and
lets one half be closed while the other stands.

**Ids describe their subject rather than counting.** `F1`/`F2` restarted at 1 in
every log, so an id meant nothing outside the one log it came from. An id is now
minted from the finding's own first location, with the **basename** (the full
path is already in `locations`); a collision falls back to another line the
finding actually covers before resorting to a numeric suffix.

> **An id is a name, not a position.** It is frozen at creation, and the file is
> revised between rounds — so its embedded line number drifts within a round of
> being minted. Never resolve an id back to a location; `locations` is the only
> current answer to "where".

**A file that leaves the work product takes its findings with it.** When a
round's membership drops a file, `close_findings_for_paths` auto-closes every
outstanding finding whose locations all name removed files: nothing will re-read
that file, so such a finding could never be verified fixed and would block the
loop forever. A finding whose *other* side is still present is left open — it is
still actionable, and closing it would discard exactly the cross-file defect the
unit exists to catch. No `review_round` line is written for this; it is
bookkeeping, not a review.

### Storage is per-session, not per-project

Findings live under the **session** directory
(`~/.kodo/sessions/<session-id>/findings/`), not under the project's
`.kodo/guided_dev_state/`. Two sessions may run different models, different
settings, and different critics over the same tree; one session's backlog is
not a fact about the project, it is a fact about that session's review. Keeping
them session-scoped means a new session starts from a clean backlog rather than
inheriting judgments made under a configuration it cannot see.

The consequence, stated plainly: **a document reviewed in session A and reopened
in session B shows no outstanding findings in B.** What survives across sessions
is the document's own evolution log (`new_revision` / `review_result` /
`accepted`), which is project-scoped and unchanged.

Log path: `<session-dir>/findings/<work product id>.jsonl`, where the id is
`<project>/<agent>[/<responsibility_code>]` (`billing-service/coder/AUTH` →
`findings/billing-service/coder/AUTH.jsonl`). Every segment is sanitised; a key
that escapes the findings directory is refused.

The leading **project** segment is load-bearing: a session may bind several
projects, and without it two of them running the same agent on the same
responsibility would share one backlog.

Identity is deliberately stable across **separate `run_subagent` calls**, not
only across rounds within one loop. The Guide routinely invokes the same author
again on the same subject ("continue resolving outstanding critic findings"),
and the backlog has to survive that — a per-loop minted id would silently orphan
it. Membership, by contrast, is per-revision: round two may add or drop a file
without changing the id.

### Inputs are resolved, not passed (2026-09-05)

An agent's ``input_paths`` is built by the engine from what the session has
actually produced. Each spec declares its contract in artifact **roles**
(`kodo.subagents._artifacts`):

```python
REQUIREMENTS_CRITIC = SubAgentSpec(
    produces={},                                    # critics write findings
    consumes=(
        Need(ROLE_REQUIREMENTS, SCOPE_UNDER_REVIEW),
        Need(ROLE_ARCHITECTURE),                    # SCOPE_GLOBAL by default
        Need(ROLE_NARRATIVE),
    ),
)
```

`produces` maps a role to the **output field** carrying its paths, because one
agent may fill several roles: `narrative_author` writes the Narrative and the
Tech Stack, `functional_designer` the Design Plan and every Functional Design.
The role mapped to `PRODUCES_REMAINDER` (`"paths"`) takes whatever no named
field claimed — which is how the Design Plan stays out of the pile of designs
written in the same run. The engine derives the ledger's `roles` map from this
and **never** from anything the model labelled: the model reports *where* it
wrote, the spec says *what* those files are for.

Scopes answer "which one?": `SCOPE_GLOBAL` (the current one for the project),
`SCOPE_SELF` (this spawn's component), `SCOPE_ALL`, `SCOPE_UNDER_REVIEW`
(supplied by the round itself, never looked up), and `SCOPE_DEPENDENCIES` — the
designs of the components this one consumes **or is consumed by**, expanded from
the architect's `components` graph. Both directions, deliberately: an interface
has two sides, and changing one without seeing the other is how cross-file drift
gets written in the first place.

Attribution is per **file**, not per work product. `functional_designer` writes
every component's design in one whole-product run, so its work product carries no
`responsibility_code`; its `designs: {codename: path}` output supplies the map,
and `WorkProduct.component_of()` falls back to the work product's own code for
the ordinary per-component stage. One role may legitimately be declared at two
scopes — `coder` asks for its own design *and* its neighbours' — so paths are
accumulated per role before labelling rather than labelled per need.

**A caller cannot write a path at all** (2026-09-05). `input_paths` and
`for_revision_paths` are `ENGINE_OWNED_TASK_FIELDS`: stripped from the
`run_subagent_<name>` tool (properties *and* `required`), while staying on the
sub-agent's own `input_schema` so the rendered task brief still describes them.
A caller says what to do in `instructions`; the engine works out which files
that means. This mirrors `schema_compliance` on the output side — engine-owned,
and never model-writable.

`for_revision_paths` is seeded from the ledger at loop start, not left empty
until round two: the Guide routinely re-invokes an author on the same subject
("continue resolving outstanding findings"), and that call's round 1 is the
work's round N. Without the seed the author would be told to revise nothing and
its findings would be scoped to no work product — the two things it needs to
carry on.

An unmet **required** need **refuses the spawn**: the caller gets an escalation
(`reason: "missing_required_input"`) naming the role, and the review loop stops
on it without spending a round. An agent handed less than its contract promises
is an agent that will invent the difference — which is exactly what the traced
failure was. It also enforces pipeline order for free: an architect spawned
before any Narrative exists is refused rather than left to guess. Scopes that
can legitimately be empty (a component with no neighbours) are declared
`required=False`.

**The membership ledger is project-scoped** (`<root>/.kodo/workproducts.jsonl`),
unlike the findings backlog beside it in the session. A backlog is a judgment
*this session's* critics made; "the architecture is these two files" is a fact
about the project. Session scope would mean a new session on an existing tree
resolved every role to nothing — and, with refusal on, refused the whole
pipeline.

Labels are the role for a single file, `<role>_<basename>` for several. The
registry validates the whole graph at load time, so an unknown role, an unknown
scope, a `produces` entry naming a field the schema lacks, or a consumed role
nobody produces stops the server rather than one sub-agent four stages later.

An unmet **required** need **refuses the spawn**: the engine returns a
`missing_required_input` escalation naming the roles nothing filled, shaped like
an author's own escalation so the caller reads it through the path it already
has, and the review loop stops on it without spending a round. Running the agent
anyway is the failure this whole mechanism exists to prevent — an agent promised
the architecture and handed nothing does not fail cleanly, it invents a
plausible path and reads it until something stops it. A need that can
legitimately be empty is declared `required=False` and is simply left out.

`input_paths` and `for_revision_paths` are off the caller-facing schema
entirely: `kodo.toolspecs.ENGINE_OWNED_TASK_FIELDS`, stripped from the generated
`run_subagent_<name>` tool of every agent whose inputs the engine resolves. The
one exception is an agent that declares **no** roles at all — `developer`, driven
by the Problem Solver, which has already read the tree and knows the files it
means. Nothing would resolve its `input_paths`, so hiding the field would take
away the only way to point it at one; its tool keeps it, and resolution leaves
what the caller named untouched.

### Two entry types, replayed in order

1. **`finding`** — `{type, timestamp, id, reported_by, …changed fields…}`.
   The **first** entry for an id creates the finding; every later entry for that
   id **patches** it. Fields absent from an entry are unchanged — this is the
   "omitted fields remain the same" rule, applied literally at the storage
   layer. Current state = replay the file.
2. **`review_round`** — `{type, timestamp, reviewer, outstanding, opened, closed}`.
   One per completed critic round. It is what makes "has this been reviewed
   since its last revision?" answerable, and it carries the round's progress
   counters for the loop's stall detection.

Membership itself lives in a **separate** log, one per project:
`<root>/.kodo/workproducts.jsonl`, replayed the same way
(`kodo.workproducts`). One file rather than one per work product, because the
question asked most often is the reverse one — *which work product is this file
part of?* — which `guided_dev_status` asks for every tracked document in the
project, and answering it from one replay beats scanning a directory.

## 3. The contract

### `get_findings` — both agents, one tool

```
input:  {show_all?: boolean}          # default false
output: {findings: [ …finding… ]}
```

Default returns only `outstanding` findings; `show_all: true` returns fixed ones
too, so a critic can see what it previously closed and avoid re-raising it.

The tool is **auto-scoped**: it takes no path. The engine knows which document
the current author/critic round targets and binds it to the run
(`ToolContext.findings_path`); neither agent can query, or be confused by,
another file's backlog. Outside a review round — and on an author's very first
pass, before any file exists — the scope is empty and the tool returns
`{"findings": []}`. That is not an error: it is what makes one prompt correct on
pass 1 and pass N alike.

Guided mode only, like `guided_dev_status`.

### `return_result` — the critic writes, nobody else

A critic's output schema is now:

```jsonc
{
  "findings": [
    // no id → new finding. `locations` is a LIST: one entry per place this
    // ONE problem appears, so a defect spanning two files stays one finding.
    {"kind": "interface_drift", "description": "…",
     "locations": [
       {"path": "billing-service/src/a.py", "first_line": 40, "last_line": 44, "excerpt": "…"},
       {"path": "billing-service/src/b.py", "first_line": 118, "last_line": 118, "excerpt": "…"}
     ]},
    {"id": "billing-service_coder_AUTH_a.py_40", "state": "fixed"},   // id → update
    {"id": "billing-service_coder_AUTH_a.py_51",
     "description": "…still wrong, now for a different reason"}
  ],
  "summary": "…"
}
```

There is **no `path`** either, as of 2026-09-04: a critic reviews a whole work
product, so one reviewed path could not describe it — and the engine already
knows which work product it spawned the critic against. Asking the model to
restate it only created a way for findings to land in the wrong backlog.

- **No `id`** → the engine mints the next one and creates the finding
  `outstanding`.
- **With `id`** → patch. Only the fields present change; everything else is
  preserved. This is how a critic marks something fixed (`state: "fixed"`), and
  how it revises a finding's wording or locations without losing its identity.
  The one exception to "only what changed": `locations`, when sent, **replaces**
  the list wholesale — a critic re-reporting them has re-read the files and is
  describing where the problem is *now*, so merging would resurrect spans it
  deliberately dropped.
- **Not mentioned at all** → **nothing changes.** An outstanding finding stays
  outstanding until a critic explicitly closes it. Silence never resolves
  anything, so a critic that overlooks its own backlog cannot silently close it.

**`accept` is gone.** The verdict is derived: a work product is accepted when
zero findings are outstanding after the round's updates are applied. A critic can no
longer return `accept: true` while leaving problems on the table, because it no
longer returns a verdict at all — it returns evidence, and the engine draws the
conclusion.

### The user's rejection is a finding

When the interactive approval gate comes back `reject` with feedback, the engine
mints that comment as an outstanding finding
(`kind: "user_feedback"`, `reported_by: "user"`) in addition to writing the
usual `review_result` entry. It is anchored to whichever member file the user
had selected at the gate, so the author knows which one to revisit; an objection
about the set as a whole carries no location. The author therefore reaches the user's
objection through exactly the same `get_findings` call as everything else —
one backlog, one procedure, no second channel.

## 4. The loop

`_run_review_loop`, per round:

1. Spawn the author (identical `instructions` every round; `for_revision_paths`
   — the whole prior member set, not one entry point — set from round 2 onward).
   No concern text is rendered into the task any more.
2. Non-empty `reason` → `escalated`, stop (unchanged).
3. Record the author's reported `paths` as this round's work-product membership,
   auto-closing findings for any file that left (§2).
4. Spawn the critic against the **whole set**. Every member reaches it in
   `input_paths` — a single member as `under_review`, several as
   `under_review_<basename>` — and the task line says plainly that they are one
   change to be judged together.
5. The engine applies the critic's `findings` to the log and appends a
   `review_round` entry, yielding `{outstanding, opened, closed}`.
6. `outstanding == 0` → drive the acceptance flow (§5) and read the status back.
   A work product's status is the **weakest** of its members' — a five-file
   change with one file still pending is not done. Settled → `accepted`, stop.
7. `closed == 0 and opened == 0` → `not_converging`, stop. This is the **stall
   detector** that replaces the old count heuristic: it fires only when a round
   genuinely did nothing — fixed nothing, found nothing — instead of when
   arithmetic on two unrelated lists happened not to decrease.
8. Otherwise, next round; `max_rounds` when the budget runs out.

`review.outcome` values are unchanged (`accepted` / `escalated` /
`not_converging` / `max_rounds` / `not_reviewed`), so the Guide's playbook for
each still applies. The `review` block now reports `outstanding` (a count)
rather than a `concerns` list.

## 5. Acceptance

`_finalize_work_product` runs when a round leaves zero outstanding findings, and
directly on each round of a gate-only loop (GUIDED_DEV_MODE.md §5a).

**Whether the gate fires at all is the author's own frontmatter.**
`user_review: true` says this artifact is worth a human's attention; without it
the work product is accepted the moment nothing is outstanding. Before that flag,
"does a human sign this off?" was answered by whether somebody had paired a
critic with the author, since the gate could only ever fire from the critic path.

**One decision settles the whole set.** Accepting members one at a time would
permit exactly the half-accepted, unbuildable state the unit exists to prevent,
so the gate fires once, carrying every member file (`paths` on
`prompt.approval`), and its outcome is written to every member's own
project-scoped log. That log stays **per file** — it is a commit and evolution
history, and is correctly per file — so acceptance fans out rather than moving.

| Condition | Behaviour |
| --- | --- |
| the author does not declare `user_review` | straight to `accepted`, no gate |
| Autonomous mode | straight to `accepted`, no gate |
| `edit_control == "allow_all"` | straight to `accepted`, no gate |
| user agrees at the gate | `review_result: approve`, then `accepted` |
| user rejects with feedback | `review_result: reject` on **every** member, **and** the comment minted as one outstanding finding, anchored to the file they had selected |

The `allow_all` shortcut is new. Edit Control set to *Allow All* already means
"don't stop me for file changes"; stopping for a document sign-off in that
posture was inconsistent with every other gate.

Note what the three shortcut rows do *not* write: no `review_result`. That entry
means "the user decided at the gate", and in those postures no gate fired —
writing one would fabricate a decision nobody made.

A rejection puts the whole work product back to `needs_revision` and gives the
enclosing loop another round with the user's objection sitting in the backlog alongside
the critic's own findings.

### The one exception to "only a critic closes a finding"

§3's rule holds because a critic *verifies*: an author saying it fixed something
is not the same as the fix being real, which is why verification is a separate
agent's job.

An author with **no `critic:`** has no such agent. Every finding on its work
product came from the user's own rejections at this gate, and the user has now
read the revised work and approved it — so their approval is the verification,
and `_close_findings_on_approval` closes whatever is still outstanding, recorded
as a review round by `user`. Without it the very first rejection would leave a
finding outstanding forever: the backlog would never empty, and the user's own
findings table would keep showing a defect they had already accepted a fix for.

This applies **only** where there is no critic. Where one exists, closing stays
its job — the user approved the set, not each individual fix.

## 6. Document status, after `feedback` was dropped

`guided_state` no longer has a `feedback` entry type, and `ConcernItem` /
`feedback_entry()` / `append_feedback()` are deleted. Three entry types remain
in the document's project-scoped log: `new_revision`, `review_result`,
`accepted`.

Status is therefore derived from **two** stores, merged by the single seam
`kodo.tools.document_status()` — used by both `guided_dev_status` and the review
loop, so there is exactly one implementation of the rule. Its findings half is
keyed on the file's **work product**, so `guided_dev_status` reaches it through
the membership log (`work_product_for_path`); a file in no work product has
never been reviewed this session and correctly reads as having an empty backlog:

| Document log's last entry | Findings | Status |
| --- | --- | --- |
| `accepted` | — | `accepted` |
| `review_result: approve` | — | `pending_acceptance` |
| `review_result: reject` | — | `needs_revision` |
| `new_revision` / nothing | any outstanding | `needs_revision` |
| `new_revision` / nothing | none, and a `review_round` newer than the last `new_revision` | `pending_acceptance` |
| `new_revision` / nothing | none, no review since the last revision | `pending_review` |

The "review_round newer than the last new_revision" comparison is what
distinguishes *not yet reviewed* from *reviewed clean*. Both logs timestamp
every entry in ISO-8601 UTC from the same process, so the comparison is a plain
string comparison.

A legacy `feedback` entry left in a log by an older build is ignored — it falls
through to the `new_revision` branch rather than being interpreted.

## 7. Prompt wiring

Two shared blocks, included by the halves that need them:

- **`shared_findings_author.md`** → `{SHARED:findings_author}`, included by the
  8 authors. States: call `get_findings` before you start, every pass; fix every
  outstanding one; you do not close findings — the critic verifies and closes.
- **`shared_findings_critic.md`** → `{SHARED:findings_critic}`, included by the
  7 critics. States: call `get_findings` first, every pass; re-verify each
  outstanding one against the current file and close what is fixed; raise new
  ones without an `id`; silence closes nothing; you return evidence, not a
  verdict.

Both are written to read identically on a first pass and a tenth. The registry
enforces the pairing the same way it enforces `{SHARED:editing}`: an agent
granted `get_findings` that includes neither block fails to load.

## 8. File reference

| File | Role |
| --- | --- |
| [findings/_records.py](../src/kodo/findings/_records.py) | `Finding`, `RoundSummary`, the two entry constructors, `merge_finding` |
| [findings/_paths.py](../src/kodo/findings/_paths.py) | logical path → session log path, with segment sanitising |
| [findings/_store.py](../src/kodo/findings/_store.py) | `read_findings`, `apply_findings`, `record_user_feedback`, `last_round_timestamp` |
| [toolspecs/_get_findings.py](../src/kodo/toolspecs/_get_findings.py) | the `get_findings` spec |
| [tools/_get_findings.py](../src/kodo/tools/_get_findings.py) | its handler |
| [tools/_document_status.py](../src/kodo/tools/_document_status.py) | `document_status()` — the two-store merge seam (§6) |
| [subagents/specs/_shapes.py](../src/kodo/subagents/specs/_shapes.py) | `finding_item()` / `critic_output()` |
| [subagents/shared_findings_author.md](../src/kodo/subagents/shared_findings_author.md) | author half of the protocol |
| [subagents/shared_findings_critic.md](../src/kodo/subagents/shared_findings_critic.md) | critic half |
| [runtime/_engine/_subagents.py](../src/kodo/runtime/_engine/_subagents.py) | `_run_review_loop`, `_run_review_round`, `_record_findings`, `_findings_dir`/`_findings_snapshot`/`_document_status` |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | `_review_output_schema` (the `review` block) + the shared-block pairing check |
| [runtime/_engine/_core.py](../src/kodo/runtime/_engine/_core.py) | `_finalize_work_product` |
| [workproducts/](../src/kodo/workproducts/) | `WorkProduct`, `work_product_id`, `record_membership`, `work_product_for_path` — the membership half (§2) |
