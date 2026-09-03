# Findings — the shared author/critic backlog

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
┌─ session  ~/.kodo/sessions/<session-id>/findings/ ─────────────────┐
│                                                                     │
│  <project>/specs/architecture.md.jsonl                              │
│    {"type":"finding","id":"F1","kind":"gap","description":"…",…}    │
│    {"type":"review_round","reviewer":"architect_critic",…}          │
│    {"type":"finding","id":"F1","state":"fixed"}                     │
│    {"type":"finding","id":"F2","kind":"…","description":"…"}        │
│    {"type":"review_round","reviewer":"architect_critic",…}          │
└─────────────────────────────────────────────────────────────────────┘
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
| `id` | Engine-minted, `F1`, `F2`, … sequential **per (session, document)**. |
| `kind` | Category, from the reviewing critic's own vocabulary section. |
| `description` | Plain terse English: what's wrong and the concrete fix. |
| `excerpt` | The few lines of code or prose where it was detected, verbatim. |
| `first_line` / `last_line` | The span it covers. |
| `state` | `outstanding` or `fixed`. |

The field names are deliberately the ones the retired `concern_item` shape
already used (`kind`/`description`/`excerpt`), so the seven critics' existing
vocabulary sections did not have to be rewritten around new words. `id` and
`state` are the new half.

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

Log path: `<session-dir>/findings/<logical document path>.jsonl`, where the
logical path is the folder-prefixed path agents already use everywhere
(`billing-service/specs/architecture.md` →
`findings/billing-service/specs/architecture.md.jsonl`). Every segment is
sanitised; a path that escapes the findings directory is refused.

### Two entry types, replayed in order

1. **`finding`** — `{type, timestamp, id, reported_by, …changed fields…}`.
   The **first** entry for an id creates the finding; every later entry for that
   id **patches** it. Fields absent from an entry are unchanged — this is the
   "omitted fields remain the same" rule, applied literally at the storage
   layer. Current state = replay the file.
2. **`review_round`** — `{type, timestamp, reviewer, outstanding, opened, closed}`.
   One per completed critic round. It is what makes "has this document been
   reviewed since its last revision?" answerable, and it carries the round's
   progress counters for the loop's stall detection.

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
  "path": "billing-service/specs/architecture.md",
  "findings": [
    {"kind": "gap", "description": "…", "excerpt": "…",
     "first_line": 40, "last_line": 44},        // no id  → new finding
    {"id": "F1", "state": "fixed"},             // id     → update
    {"id": "F2", "description": "…still wrong, now for a different reason"}
  ],
  "summary": "…"
}
```

- **No `id`** → the engine mints the next one and creates the finding
  `outstanding`.
- **With `id`** → patch. Only the fields present change; everything else is
  preserved. This is how a critic marks something fixed (`state: "fixed"`), and
  how it revises a finding's wording or span without losing its identity.
- **Not mentioned at all** → **nothing changes.** An outstanding finding stays
  outstanding until a critic explicitly closes it. Silence never resolves
  anything, so a critic that overlooks its own backlog cannot silently close it.

**`accept` is gone.** The verdict is derived: a document is accepted when zero
findings are outstanding after the round's updates are applied. A critic can no
longer return `accept: true` while leaving problems on the table, because it no
longer returns a verdict at all — it returns evidence, and the engine draws the
conclusion.

### The user's rejection is a finding

When the interactive approval gate comes back `reject` with feedback, the engine
mints that comment as an outstanding finding
(`kind: "user_feedback"`, `reported_by: "user"`, no line numbers) in addition to
writing the usual `review_result` entry. The author therefore reaches the user's
objection through exactly the same `get_findings` call as everything else —
one backlog, one procedure, no second channel.

## 4. The loop

`_run_review_loop`, per round:

1. Spawn the author (identical `instructions` every round; `for_revision_path`
   set from round 2 onward). No concern text is rendered into the task any more —
   `_revision_instructions` is deleted.
2. Non-empty `reason` → `escalated`, stop (unchanged).
3. Spawn the critic against the author's `primary_path`.
4. The engine applies the critic's `findings` to the log and appends a
   `review_round` entry, yielding `{outstanding, opened, closed}`.
5. `outstanding == 0` → drive the acceptance flow (§5) and read the document's
   status back. Settled → `accepted`, stop.
6. `closed == 0 and opened == 0` → `not_converging`, stop. This is the **stall
   detector** that replaces the old count heuristic: it fires only when a round
   genuinely did nothing — fixed nothing, found nothing — instead of when
   arithmetic on two unrelated lists happened not to decrease.
7. Otherwise, next round; `max_rounds` when the budget runs out.

`review.outcome` values are unchanged (`accepted` / `escalated` /
`not_converging` / `max_rounds` / `not_reviewed`), so the Guide's playbook for
each still applies. The `review` block now reports `outstanding` (a count)
rather than a `concerns` list.

## 5. Acceptance

`_finalize_document` runs when a round leaves zero outstanding findings.

| Condition | Behaviour |
| --- | --- |
| Autonomous mode | straight to `accepted`, no gate |
| `edit_control == "allow_all"` | straight to `accepted`, no gate |
| user agrees at the gate | `review_result: approve`, then `accepted` |
| user rejects with feedback | `review_result: reject`, **and** the comment minted as an outstanding finding |

The `allow_all` shortcut is new. Edit Control set to *Allow All* already means
"don't stop me for file changes"; stopping for a document sign-off in that
posture was inconsistent with every other gate.

Note what the two shortcut rows do *not* write: no `review_result`. That entry
means "the user decided at the gate", and in those two postures no gate fired —
writing one would fabricate a decision nobody made.

A rejection puts the document back to `needs_revision` and gives the enclosing
loop another round with the user's objection sitting in the backlog alongside
the critic's own findings.

## 6. Document status, after `feedback` was dropped

`guided_state` no longer has a `feedback` entry type, and `ConcernItem` /
`feedback_entry()` / `append_feedback()` are deleted. Three entry types remain
in the document's project-scoped log: `new_revision`, `review_result`,
`accepted`.

Status is therefore derived from **two** stores, merged by the single seam
`kodo.tools.document_status()` — used by both `guided_dev_status` and the review
loop, so there is exactly one implementation of the rule:

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
| [runtime/_engine/_core.py](../src/kodo/runtime/_engine/_core.py) | `_finalize_document` |
