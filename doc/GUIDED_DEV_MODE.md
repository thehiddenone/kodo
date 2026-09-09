# Kodo — Guided Development Mode

> Reference: [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) (how to write one), [FINDINGS.md](FINDINGS.md) (the author/critic backlog, artifact roles, work products), [TOOLS.md](TOOLS.md) §5A (`run_subagent`/`return_result`), [CHECKPOINTS.md](CHECKPOINTS.md), [STATE_AND_LIFECYCLE.md](STATE_AND_LIFECYCLE.md), [STUCK_DETECTION.md](STUCK_DETECTION.md), [WS_PROTOCOL.md](WS_PROTOCOL.md) §5.6/§6.2.

## 1. What it is

Guided mode builds a product by running a **fixed, ordered pipeline of
specialist sub-agents**, each writing one kind of document or code, each
reviewed before it is accepted. It is the opposite of a free-form coding
assistant: the order is fixed, the artifacts are tracked, and nothing advances
until the thing before it is settled.

*How* each stage is reviewed is the agent's own declaration, not engine policy:
its frontmatter names a `critic:`, sets `user_review: true`, both, or neither
(§5a). The same frontmatter can also give the agent different prompt text for a
first pass than for a correction round (§5b).

The alternative mode, **Problem Solver**, is the free-form one — an
investigator/planner/developer trio driven by whatever the user asks. Both modes
share the engine, the tools, the security layer and the checkpoint mirror; what
differs is the entry agent and, with it, the shape of the work.

Two ideas do most of the load-bearing here, and both are worth stating before
the mechanics:

- **The Guide owns the process; sub-agents own the files.** The entry agent
  never writes a narrative, a requirement, a design, a test or a line of code.
  It decides what runs next, on what, and when the user must be involved.
- **No model ever authors a file path.** A caller says *what* to do; the engine
  works out *which files* that means, from what the project has actually
  produced. §6 is the whole story, and §11 is what happened when that was not
  true.

## 2. Entering the mode

`workflow.set` (WS_PROTOCOL.md) sets `session.workflow_mode` to `"guided"` (the
default) or `"problem_solving"`. Like Autonomous mode, it is a **frozen toggle**:
`effective_workflow_mode` is snapshotted when a prompt starts, so flipping the
switch mid-turn never changes the mode a running turn is already executing under.

The mode picks the entry agent — `agent_guide.md` for Guided, and it is that
prompt, not the engine, that encodes the pipeline order.

A third mode, `"judge"`, exists for the validator (`agent_judge.md`) and is
reachable only over the wire; the extension's picker never offers it.

## 3. The cast

| Role | Who | Invoked by |
| --- | --- | --- |
| **Entry agent** | `guide` | the user's prompt |
| **Authors** | `narrative_author`, `architect`, `requirements_author`, `functional_designer`, `test_designer`, `test_coder`, `coder`, `e2e_test_designer`, `e2e_test_coder` | the Guide, one `run_subagent_<name>` call each |
| **Critics** | `architect_critic`, `requirements_critic`, `functional_design_critic`, `test_design_critic`, `code_critic`, `e2e_test_design_critic`, `e2e_test_code_critic` | **the engine**, inside their author's call |
| **Adjuncts** | `toolchain_builder`, `investigator` | the Guide, outside the pipeline |

A critic declares `role: critic` in its frontmatter and gets **no**
`run_subagent_<name>` tool: `AgentRegistry.run_subagent_specs` skips it. The
Guide cannot invoke a critic, cannot iterate a loop by hand, and never sees a
finding — the author and critic exchange those directly through their own
`get_findings` tool.

The **user** is the pipeline's other reviewer, and which stages they review is
declared the same way — see §5a.

The Guide's own tools are deliberately few: `guided_dev_status`,
`get_root_paths`, `find_files`, `find_text_in_files`, `read_attachment`,
`run_subagent`, `ask_user`, `rollback`, `finalize_project`,
`scaffold_new_project`, `disable_autonomous_mode`, `run_command`. It has no
`create_file`/`edit_file` — the "you own the process, not the files" rule is
enforced by the toolset, not merely asserted in prose.

## 4. The pipeline

```
 1  narrative_author                            👤      → Narrative + Tech Stack
 2  architect        ↔ architect_critic         👤      → architecture + component graph
 3  requirements_author ↔ requirements_critic   👤      → requirements
 4  functional_designer ↔ functional_design_critic 👤   → Design Plan + one design per component
 5  test_designer    ↔ test_design_critic  👤 per comp. → one Test Plan per component
 6  test_coder       ↔ code_critic            per comp. → test code + stubs (all failing)
 7  coder            ↔ code_critic            per comp. → implementation (all passing)
 8  e2e_test_designer ↔ e2e_test_design_critic  👤      → End-to-End Test Plan
 9  e2e_test_coder   ↔ e2e_test_code_critic             → the integration suite (run to green)
```

`👤` marks a stage whose frontmatter declares `user_review: true` — the user
signs its work product off before it is accepted (§5a). Stage 1 has **no**
critic, so the user is its only reviewer; the three code stages have a critic
and no gate, because code correctness is settled by the critic and by the tests
going green, and gating every multi-file code change is where a gate stops being
read and starts being clicked through.

Stages **5–7 run per component**, in the order the Design Plan sets — their
specs are the only ones that declare a `responsibility_code`, and so the only
tools that offer the Guide one. Stages **1–4 and 8–9 are product-level** and run
once each. The pipeline is single-threaded: one sub-agent invocation at
a time, no parallelism.

> **Stage 4 is product-level, despite writing per-component documents.**
> `functional_designer` decides the component *order*, so it cannot be run one
> component at a time: it writes the Design Plan and every codename's Functional
> Design in a single call, and its spec does not require a `responsibility_code`.
> That is why its work product needs the per-file `designs: {codename: path}`
> attribution described in §6 — without it, `SELF` scope could only match all of
> its designs or none.
>
> This used to be a live footgun: `responsibility_code` was caller-settable on
> *every* stage's tool, and it feeds the work-product id, so a stray one on
> stage 4 split its record across calls and every later stage asking for "this
> component's Functional Design" stopped finding one. The only guard was a line
> in `agent_guide.md` telling the Guide not to. It is now the engine's, not the
> model's: the field exists only on a per-component stage's tool, and a stray
> one is dropped before anything reads it (§6).

`X ↔ Y` is **one** `run_subagent_X` call, not two. The engine spawns the critic
inside it and runs the revision rounds (§5).

### The stage 8–9 gate

The **Architect determines** end-to-end testability and returns it as
`end_to_end_testable: "applicable" | "excluded"`; the Guide **acts on** that
determination and no other agent re-checks it. `excluded` means stages 8–9 are
skipped entirely and the pipeline is complete when stage 7 finishes for every
component. A later architecture revision can flip the verdict either way, which
is one of the invalidation-cascade cases in §9.

### Stage 6 is deliberately red

`test_coder` writes the tests **and** minimal production stubs so that every
test *fails* — the TDD-correct starting state for stage 7, whose contract is to
make them pass. Any tooling that judges "did the build succeed?" between stages
has to know this; it is why the engine-run build gate is still unbuilt (§12).

## 5. One stage, in detail

`_run_review_loop` (`runtime/_engine/_subagents.py`) drives a single
`run_subagent_<author>` call. One **round** is:

1. **Resolve the author's inputs** from its declared artifact roles (§6). An
   unmet required role refuses the spawn outright.
2. **Spawn the author** with the caller's `instructions` **unchanged** every
   round, plus `for_revision_paths` — the whole prior member set, seeded from
   the ledger so a re-invocation continues rather than restarting — and in the
   round's **phase** (§5b). Outstanding findings are *never* written into the
   task; the author reads them itself.
3. **Escalation check** — a non-empty `reason` ends the loop where it stands
   (`outcome: "escalated"`). No review is run: no amount of revision fixes a
   blocker whose resolution lives outside the author.
4. **Record the work product** — every path the author reported, as one
   reviewable set (§7), auto-closing findings for any file that left it.
5. **Review it** — spawn the critic against the whole set with its own resolved
   inputs, or, for an author with no critic, put the set straight to the user's
   approval gate (§5a).
6. **Apply the round's findings** to the work product's backlog, push the user's
   findings table (§8a), and close the round.
7. **Derive the verdict**: nothing outstanding → drive acceptance (§8) and read
   the status back.

The loop ends on one of five outcomes, reported in the `review` block:

| `review.outcome` | Meaning | What the Guide does |
| --- | --- | --- |
| `accepted` | The round left nothing outstanding and the work product settled. | Move on. |
| `escalated` | The author hit a blocker it cannot defensibly resolve. | Triage (§9). |
| `max_rounds` | The budget ran out with findings outstanding. | Raise the budget, reopen an upstream document, or escalate. |
| `not_converging` | A whole round closed nothing **and** opened nothing. | Diagnose — do **not** just re-run with a bigger budget. |
| `not_reviewed` | The author reported no paths. | Something went wrong upstream; check status first. |

`max_rounds` defaults to **5** (`MAX_ROUNDS_DEFAULT`) and is capped at **10**
(`_MAX_REVIEW_ROUNDS`). `not_converging` is an exact no-progress signal, not
arithmetic on counts: stateful findings make "closed nothing, opened nothing"
answerable directly, where the retired heuristic also fired on a round that
fixed two problems and found two others.

### There is no `accept` field

A critic returns **evidence, not a verdict**: its `findings` list, and nothing
else. The verdict is *derived* — the work product is accepted when the backlog
is empty. A critic therefore cannot report a pass while leaving problems open,
and the two can never disagree. See FINDINGS.md §3.

## 5a. Who reviews a stage is frontmatter, not policy

Two independent frontmatter flags decide what one `run_subagent_<author>` call
actually does. Both live on the **author**, and neither is ever named by a
caller:

| `critic:` | `user_review:` | One call is |
| --- | --- | --- |
| set | absent | author→critic rounds; accepted the moment the backlog empties |
| set | `true` | author→critic rounds, then the user signs the set off |
| absent | `true` | author→**user** rounds — the gate *is* the review |
| absent | absent | a single pass, no review at all |

`user_review` is **opt-in and off by default**. Before it existed, whether a
human saw an artifact was decided by whether somebody had paired a critic with
its author: `_finalize_work_product` only ever ran from the critic path, so every
critic-reviewed work product was gated and `narrative_author` — the one document
written *with* the user, and the one every later stage derives from — never was.
That is a question about the artifact, and it now gets asked about the artifact.

**A gate-only author still runs a loop.** `_run_review_loop` takes an empty
critic name and calls `_run_user_review_round` in place of the critic round: the
rejection is minted as a `user_feedback` finding, so round two's author reaches
the objection through the same `get_findings` call it would use for a critic's,
and `not_converging`/`max_rounds` bound it exactly as they bound a critic loop.
Such an author therefore needs `get_findings` and `{SHARED:findings_author}` in
its own frontmatter and body — `narrative_author` gained both here.

Two load-bearing consequences:

- The registry **refuses** `user_review: true` on an agent whose spec produces
  no artifact role, and on any `role: critic`. In both cases there is no work
  product to sign off and the flag would simply never fire.
- **Approval closes the backlog when there is no critic.** The rule everywhere
  else is that only a critic closes a finding, because only a critic verifies.
  An author with no critic has nobody to do that, its findings can only have come
  from the user's own earlier rejections, and the user has now looked at the
  revised work and approved it — so their approval *is* the verification
  (`_close_findings_on_approval`). Without it the first rejection would leave a
  finding outstanding forever.

An unknown author (renamed, removed) fails **open**: no gate, straight to
accepted. Work nobody can re-run is better accepted than parked at a gate
forever.

**The gate also settles individual findings.** A gate-only loop does not reach an
approval while the user keeps rejecting, so without this the backlog only grows:
round 1 raises A, round 2 the author fixes A and the user objects to B, and round
3's author re-reads a complaint it already fixed. So `prompt.approval` carries
the outstanding findings and the response carries `resolved_finding_ids`, applied
before the accept/reject branch and validated against what is actually
outstanding. With a critic the list is always empty — that gate is reached only
on a clear backlog — so the controls never appear there. FINDINGS.md §5.

## 5b. Phases — the same author, a different job

An author's two jobs are genuinely different work: writing a document from
nothing, and surgically resolving a backlog against one that already exists.
Until now both got the same prompt, so every authoring standard was restated on a
round whose whole task was "fix these three things and change nothing else".

An agent opts in by writing **phase blocks** in its body — the inclusion *is* the
declaration, exactly as with `{SHARED:…}`; there is deliberately no `phases:`
frontmatter key:

```markdown
{PHASE:initial}
…text used only on a first pass…
{/PHASE}

{PHASE:revision}
…text used only when working the findings backlog…
{/PHASE}
```

`AgentRegistry.get(name, autonomous, phase)` keeps the matching blocks and drops
the rest, after `{SHARED:…}` and `{SKILLS}` substitution. An agent with no phase
blocks renders identically in every phase, and `initial` is the default because
it is the *fuller* text — a spawn path that forgets the phase must degrade to
"say everything", never to "say nothing".

**The engine picks the phase from the same signal that drives
`for_revision_paths`**: whether a work product with members already exists for
this author and responsibility. Because the ledger is read *before* round 1, a
re-invocation continuing earlier work is correctly a `revision` from its very
first round.

There are **two** phases, not three. A user's rejection lands in the same backlog
as every critic finding, distinguished by `reported_by` (FINDINGS.md); splitting
`revision` into critic and user variants would re-divide what that design
deliberately unified, and the author reads the merged backlog either way.

> **Why not a `corrector` sub-agent?** It was the obvious alternative and it is
> the wrong shape. The reviewable unit is keyed by *agent name*
> (`<project>/<agent>[/<responsibility>]`), so a second name forks the
> work-product ledger and the findings backlog with it. A corrector would also
> need its author's entire domain standards — you cannot fix a "compound
> requirement" finding without knowing the requirement rules — and a twin
> `SubAgentSpec`. A conditional section buys the same behavioural shaping with
> none of that, and stays reversible.

Malformed blocks are load-time errors: an unknown phase name, an unclosed or
stray token, a nested block, a closing token that names its phase
(`{/PHASE:revision}` matches no close, so the block would silently swallow the
rest of the prompt), or a phase block in an agent with no `SubAgentSpec` — which
is never spawned with a phase, so the text would render nowhere.

## 6. Where files come from

This is the part that is easy to get wrong, and was.

Each spec declares its contract in a closed vocabulary of **artifact roles**
(`kodo/subagents/_artifacts.py`):

```python
REQUIREMENTS_CRITIC = SubAgentSpec(
    produces={},                                     # critics write findings
    consumes=(
        Need(ROLE_REQUIREMENTS, SCOPE_UNDER_REVIEW),
        Need(ROLE_ARCHITECTURE),                     # SCOPE_GLOBAL by default
        Need(ROLE_NARRATIVE),
    ),
)
```

**Roles** (11): `narrative`, `tech_stack`, `architecture`, `requirements`,
`design_plan`, `functional_design`, `test_plan`, `test_code`, `code`,
`e2e_test_plan`, `e2e_test_code`.

**Scopes** (5) answer *which one?*:

| Scope | Resolves to |
| --- | --- |
| `GLOBAL` | The current work product filling that role for the project. |
| `SELF` | That role, narrowed to this spawn's component. |
| `DEPENDENCIES` | The components this one consumes **or is consumed by**, from the architect's graph. Both directions: an interface has two sides. |
| `ALL` | Every work product filling the role. |
| `UNDER_REVIEW` | The work product this critic round is reviewing — supplied by the round, never looked up. |

`produces` maps a role to the **output field** carrying its paths, because one
agent may fill two: `narrative_author` writes the Narrative *and* the Tech
Stack; `functional_designer` writes the Design Plan *and* every Functional
Design. The role mapped to `PRODUCES_REMAINDER` (`"paths"`) takes whatever no
named field claimed — which keeps the Plan out of the pile of designs.

Attribution is per **file** where it has to be: `functional_designer` runs once
for the whole product, so its `designs: {codename: path}` output is what lets
`SELF` and `DEPENDENCIES` narrow to one design rather than matching all or none.

**The caller cannot write a path at all.** `input_paths` and
`for_revision_paths` are `ENGINE_OWNED_TASK_FIELDS`: stripped from the generated
`run_subagent_<name>` tool (properties *and* `required`), while kept on the
sub-agent's own `input_schema` so the rendered task brief still describes them.
This mirrors `schema_compliance` on the output side. It holds for every agent in
this pipeline because every one of them declares roles; the stripping follows
that declaration, so an agent with none (the Problem Solver's `developer`, which
is not part of Guided mode) keeps its caller-supplied `input_paths` — hiding a
field is only right where resolution replaces it.

**Nor which component a stage is scoped to, unless it is a per-component
stage.** `responsibility_code` is a real caller decision for stages 5–7 — the
Guide picks which component runs next — and meaningless everywhere else, where
it is also *harmful*: it feeds the work-product id, so one aimed at a
product-level stage splits that stage's record. The engine owns it in two
layers:

- **The schema does not offer it.** `pipeline_input(require_responsibility=True)`
  is the only thing that declares the property, so it is absent from every
  product-level stage's and every critic's `input_schema`, and therefore from
  their `run_subagent_<name>` tools. A caller cannot set what it is never shown.
- **The engine drops a stray one.** Both spawn paths (`_run_unreviewed_author`,
  `_run_review_loop`) pass the task through `_scoped_task_input` first, and read
  the component through `_responsibility_code`; both consult
  `SubAgentSpec.takes_responsibility_code` rather than trusting what arrived. A
  model that emits the undeclared key anyway is ignored — logged, not refused,
  because the spawn is correct once the field is gone.

A **critic** never declares it: its task is built entirely by the engine
(`instructions` plus resolved `input_paths`), and its `SELF` needs are narrowed
from the work product under review, whose own `responsibility_code` the round
already knows. `test_design_critic` declared it until 2026-09-05 and was never
handed one.

**An unmet required need refuses the spawn**, returning a
`reason: "missing_required_input"` escalation naming the role. That is what
turns "the agent will guess" into "the caller is told the ordering is wrong",
and it enforces pipeline order for free — an architect invoked before any
Narrative exists is refused rather than left to invent one. A need that can
legitimately be empty (a component with no neighbours) is declared
`required=False`.

The registry validates the whole graph **at load time**: an unknown role or
scope, a `produces` entry naming a field the schema lacks, or a consumed role
nobody produces raises `AgentLoadError`. A typo stops the server, not one
sub-agent four stages later.

## 7. The reviewable unit is a work product

A **work product** is every file one `run_subagent_<author>` review loop wrote —
reviewed, accepted, and (eventually) built together.

It replaced a single `primary_path`, whose one-file-per-author model came from
the first three authors each writing exactly one document. A coder does not: a
feature spanning five files has to land in one go or the build breaks, and
reviewing it file-by-file cannot see the coherence *between* the files, which is
what is most likely to be wrong.

Consequences worth knowing:

- **Identity is derived and stable**: `<project>/<agent>[/<responsibility>]`. It
  survives *separate* `run_subagent` calls, because the Guide routinely invokes
  the same author again on the same subject ("continue resolving outstanding
  findings") and the backlog must survive that.
- **Membership is per-revision.** A file may join or leave between rounds. When
  one leaves, its findings are **auto-closed** — nothing will re-read it, so an
  outstanding finding against it could never be verified and would block the
  loop forever. A finding whose *other* side is still present stays open.
- **A finding carries `locations[]`**, a list — so "the signature in `a.py` does
  not match the call in `b.py`" is **one** finding with two locations, not two
  unlinked ones. That defect is the reason the unit exists.

## 8. Acceptance

`_finalize_work_product` runs when a round leaves zero outstanding findings, and
directly each round of a gate-only loop (§5a).

| Posture | Behaviour |
| --- | --- |
| Author does not declare `user_review` | straight to `accepted`, no gate |
| Autonomous mode | straight to `accepted`, no gate |
| Edit Control `allow_all` | straight to `accepted`, no gate |
| User agrees at the gate | `review_result: approve`, then `accepted` |
| User rejects with feedback | `review_result: reject`, **and** the comment minted as an outstanding finding |

**One decision settles the whole set.** The `prompt.approval` request carries
every member file as `paths`; the client lists them and offers a single Accept.
Accepting members one at a time would permit exactly the half-accepted,
unbuildable state the unit exists to prevent. The outcome is written to *every*
member's own project-scoped log — that log stays per file, because it is a
commit history.

A rejection is anchored to whichever member the user had selected, minted as a
`user_feedback` finding so the author reaches the objection through the same
`get_findings` call as every critic finding. One backlog, one procedure.

The three shortcut rows write **no** `review_result`: that entry means "the user
decided at the gate", and in those postures no gate fired.

## 8a. The findings table — what the *user* is shown

Findings were, until now, entirely invisible to the user: `review.verdict`
carries counts, and the findings themselves never left the author/critic pair.
So a user watching a loop grind through five rounds saw collapsed subsession
blocks and no statement of what was actually wrong or whether it was shrinking.

`review.findings` (WS_PROTOCOL.md §5.6) fixes that. It is emitted after **every
round that could have changed the backlog** — each critic round, and each trip
through the approval gate — carrying the whole backlog, fixed items included,
plus `iteration`/`max_rounds` so the reader can see the loop converging. It is
silent when the backlog is empty: work that was right first time has nothing to
table, and an empty one on every clean accept would train the reader to skip it.

**No LLM ever sees it, structurally.** The event is persisted as a *marker*, and
markers carry no `role`, so they are never rebuilt into the LLM-facing message
history. The author reaches the same backlog through its own `get_findings` tool
— a separate path with its own auto-scoping — so what the user is shown and what
the model is told cannot become entangled.

Two rules the client does not get to re-decide:

- **Order is server-side** (`kodo.findings.sort_for_display`): outstanding
  before fixed; within each group by the first location's path, then line, then
  id for a stable total order. One implementation, not one per client.
- **One row is one finding**, placed by its first location — never one row per
  location. `locations` is a list precisely so a cross-file defect stays a single
  finding (§7); splitting it into rows would recreate the unlinked pair that
  model removed.

## 9. The Guide's control loop

```
guided_dev_status  →  pick the single next action  →  one run_subagent call
        ↑                                                      │
        └──────────────  observe outcome, post update  ────────┘
```

Entry is wherever the status scan says it is. A user who brings an existing,
accepted requirements document starts at stage 4; nothing accepted is
regenerated unless invalidation demands it.

### Escalation triage

Every escalation routes through the Guide, which sorts it:

- **Procedural** — about process (which file to rework, what order). The Guide
  resolves it.
- **Substantive** — about the product (what it should do, which reading is
  right). Goes to the user via `ask_user`; the decision and its rationale are
  recorded in a `<kodo_info>` callout.
- **Ambiguous rework target** — the report implicates no single file. Ask the
  user which side to fix.

The resolution reaches the sub-agent by re-running the stage with the decision
written into `instructions`. A blocker sent back unresolved comes straight back.

### Invalidation cascade

Conservative: everything downstream of a changed document is invalidated.

> Narrative / Tech Stack → architecture → requirements → Design Plan →
> per-component Functional Design → Test Plan → test code + stubs →
> implementation → End-to-End Test Plan → End-to-End suite

A product-level change invalidates everything below it for **all** components; a
per-component change only for **that** component. Retiring a codename
invalidates everything under it. Before executing a cascade wider than one
component, the Guide tells the user what will be lost and gets approval.

### Forward progress — two layers

**Layer 1, per loop**: the engine counts rounds; the Guide sizes the budget and
decides what to do with an unsettled outcome (the table in §5).

**Layer 2, pipeline-wide**: the Guide tracks rework counts per file. Individual
loops can each stay inside their budget while the system orbits — coder routes a
finding to test_coder, the plan is revised, tests are revised, coder fails
again. At roughly **3 rework cycles on one file without net progress**, the
Guide stops scheduling and diagnoses. The most likely root cause is an inherent
contradiction in the user's own input, which no amount of downstream rework can
reconcile.

That diagnosis is the **break-glass**: the Guide calls
`disable_autonomous_mode` — a contradiction in the user's intent cannot be
resolved by autonomous judgment — presents the diagnosis via `ask_user`, and
resumes from the cascade the resolution triggers.

## 10. State: three stores, three lifetimes

| Store | Scope | Holds | Why that scope |
| --- | --- | --- | --- |
| `kodo.guided_state` | **project** (`<root>/.kodo/guided_dev_state/…`) | per-file `new_revision` / `review_result` / `accepted` | A commit and acceptance history is a durable fact about the file. |
| `kodo.workproducts` | **project** (`<root>/.kodo/workproducts.jsonl`) | work-product membership, artifact roles, per-file component attribution, the architect's component graph | "The architecture is these two files" is a fact about the project — a new session must resolve it. |
| `kodo.findings` | **session** (`<session-dir>/findings/<work product id>.jsonl`) | the author/critic backlog | A backlog is a judgment *this session's* critics made, under models and settings a later session cannot see. |

All three are append-only JSONL with **no index** — current state is always a
replay of the log, which is what makes "omitted fields keep their values" true by
construction rather than by remembering to preserve them.

### Deriving a document's status

`kodo.tools.document_status` is the single seam merging the project log with the
findings backlog. Both `guided_dev_status` and the review loop use it, so there
is exactly one implementation of the rule:

| Document log's last entry | Findings | Status |
| --- | --- | --- |
| `accepted` | — | `accepted` |
| `review_result: approve` | — | `pending_acceptance` |
| `review_result: reject` | — | `needs_revision` |
| `new_revision` / nothing | any outstanding | `needs_revision` |
| `new_revision` / nothing | none, reviewed since the last revision | `pending_acceptance` |
| `new_revision` / nothing | none, not reviewed since | `pending_review` |

A work product's status is the **weakest** of its members': a five-file change
with one file still pending is not done.

Every write by any agent to a tracked path (`specs/`, `src/`, `test/` under a
bound root) also earns a **checkpoint commit** in the shadow-git mirror and a
`new_revision` entry — in both workflow modes, so a Problem-Solver edit to a
tracked document is recorded (tagged `workflow: "problem_solving"`) and the
Guide can reconcile when Guided mode resumes. See CHECKPOINTS.md.

## 11. When things go wrong

**A refused spawn** (§6) is the designed failure: loud, named, and recoverable
by running the missing stage.

**A looping model** is the undesigned one. Stuck detection (STUCK_DETECTION.md)
covers stalls, truncation, terse completions, mid-stream thinking loops, and
`<think>` inside tool-call arguments. §2.11 covers the round-boundary case: the
same tool call, with the same arguments, returning the same result, three times
in a row.

That last detector exists because of one incident worth remembering here. A
`requirements_critic` whose contract promised it the architecture was handed only
the document under review — the engine hardcoded `{"target": path}` for every
critic — and reconstructed the architecture's path from the **worked example in
its own schema description**, whose nested layout the pipeline never produces. It
then read that nonexistent file **1,133 times over 32 minutes**. Five other
critic runs in the same session silently reviewed on partial input and said
nothing at all.

Everything in §6 and §7 is the answer to that: roles instead of paths,
resolution instead of guessing, refusal instead of under-supply, and a
repetition detector to bound whatever still slips through.

## 12. Not built yet

**The build gate.** Nothing runs the project's build between an author and its
critic. The naive version is wrong for two reasons found while scoping it:
stage 6 leaves every test failing *by design*, and a project without
`scripts/` returns a build failure meaning "no toolchain", not "broken code".

The intended shape is a **dedicated sub-agent**: it knows which files changed,
runs the build, analyses the outcome, decides whether the failures are
attributable to those changes, and only then sends the work back — and it runs
**once after the critic's findings are settled**, not on every round, because
building per iteration is too expensive to be worth it.

## 13. File reference

| File | What it owns |
| --- | --- |
| [subagents/agent_guide.md](../src/kodo/subagents/agent_guide.md) | The pipeline order, triage rules, cascade, forward-progress layers |
| [subagents/_artifacts.py](../src/kodo/subagents/_artifacts.py) | Artifact roles, scopes, `Need` |
| [subagents/_subagentspec.py](../src/kodo/subagents/_subagentspec.py) | `produces` / `consumes` / `component_paths` |
| [subagents/specs/](../src/kodo/subagents/specs/) | One spec per sub-agent; `_shapes.py` builds the shared envelopes, including which stages declare a `responsibility_code` |
| [subagents/_loader.py](../src/kodo/subagents/_loader.py) | Frontmatter: `critic:`, `user_review:`, `role:`, `standalone:` |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | Load-time validation, `run_subagent_specs`, `render_phase` |
| [runtime/_engine/_subagents.py](../src/kodo/runtime/_engine/_subagents.py) | `_run_review_loop`, resolution, refusal, work-product recording |
| [runtime/_engine/_core.py](../src/kodo/runtime/_engine/_core.py) | `_finalize_work_product` — the acceptance flow |
| [workproducts/](../src/kodo/workproducts/) | Membership, roles, component graph, `resolve_needs` |
| [findings/](../src/kodo/findings/) | The author/critic backlog, and `sort_for_display` |
| [runtime/_engine/_events.py](../src/kodo/runtime/_engine/_events.py) | `emit_review_findings` — the user's findings table |
| [guided_state/](../src/kodo/guided_state/) | Per-file revision/acceptance history |
| [tools/_document_status.py](../src/kodo/tools/_document_status.py) | The one status-merge rule |
| [tools/_guided_dev_status.py](../src/kodo/tools/_guided_dev_status.py) | The Guide's view of the whole project |
