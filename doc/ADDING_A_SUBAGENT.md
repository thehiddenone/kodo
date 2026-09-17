# Adding or Editing an Agent

> The pipeline these agents run in, end to end: [GUIDED_DEV_MODE.md](GUIDED_DEV_MODE.md).
> How a session drives them: [SESSIONS.md](SESSIONS.md). The tool layer they
> call through: [TOOLS.md](TOOLS.md).

A working guide to Kōdo's two kinds of agent — how to add one, how to edit one,
and which of the two you actually want. Distilled from real changes.

The two repos: prompts, specs and engine live in **`kodo`** (`src/kodo/...`); the
VSIX front-end lives in **`kodo-vsix`**. Adding a **sub-agent** is entirely a
`kodo`-side change. Adding a user-selectable **entry agent** touches both.

## Contents

1. [Which kind of agent are you building?](#1-which-kind-of-agent-are-you-building)
2. [Anatomy of an agent](#2-anatomy-of-an-agent)
3. [Adding a sub-agent](#3-adding-a-sub-agent)
4. [Adding a top-tier (entry) agent](#4-adding-a-top-tier-entry-agent)
5. [Recipes by use case](#5-recipes-by-use-case)
6. [Artifact roles: `produces` / `consumes`](#6-artifact-roles-produces--consumes)
7. [Phase blocks](#7-phase-blocks-phase)
8. [Editing an existing agent](#8-editing-an-existing-agent)
9. [Tests](#9-tests)
10. [Run / verify](#10-run--verify)
11. [Reference tables](#11-reference-tables)

---

## 1. Which kind of agent are you building?

Kōdo has exactly two kinds, and they are not variations on each other — they
differ in who starts them, whether they have a typed contract, and whether adding
one requires touching the engine at all.

| | **Entry agent** (top tier) | **Sub-agent** |
|---|---|---|
| Files | `agent_<name>.md` | `subagent_<name>.md` **+** `specs/<name>.json` |
| Who starts it | the **user**, by selecting a workflow mode | another **agent**, via its `run_subagent_<name>` tool — or an engine service |
| Typed I/O contract | **none** — it talks to a human in prose | **required** — a `SubAgentSpec` with input + output JSON Schema |
| Terminal call | none; it just replies (Judge is the exception — `submit_evaluation`) | `return_result`, auto-granted and bound to its `output_schema` |
| `## Purpose` section | not needed (nobody delegates to it) | **required** if any caller lists it; it becomes the tool description |
| Conversation | multi-turn, persisted to `session.jsonl`, survives mode switches | one subsession, collapsed in the UI, result handed back to the caller |
| Engine changes to add one | **yes** — ~6 edits across `kodo`, plus `kodo-vsix` if user-selectable | **none** — drop in two files |
| Live examples | `guide`, `problem_solver`, `judge` | the other 23 |

**Rule of thumb:** if a *human* picks it from the UI, it is an entry agent. If an
*agent* decides to delegate to it, it is a sub-agent. When in doubt build a
sub-agent — it is pure data, it fails fast at startup if it is wrong, and it
costs no engine surface. There are three entry agents and there has not been a
new one in a long time; there are 23 sub-agents.

A third category exists but is not a third kind: **engine-driven sub-agents**
(`compactor`, `web_search`, `toolchain_depsmgr`). They are ordinary sub-agents
with ordinary specs, but the engine spawns them from a dedicated service instead
of a caller's tool — see [recipe 5.6](#56-an-engine-driven-sub-agent-no-caller).

---

## 2. Anatomy of an agent

Everything the registry loads lives under `src/kodo/agents/`, split by kind:

```
src/kodo/agents/
  agent_<name>.md          a top-level agent: frontmatter + system prompt body
  <name>.json              how that agent is selected (label, description, rank)
  shared_<name>.md         a reusable prompt block, pulled in with {SHARED:<name>}
  subagents/
    subagent_<name>.md     a sub-agent: frontmatter + system prompt body
    specs/<name>.json      that sub-agent's typed I/O contract (SubAgentSpec)
```

The shared blocks sit at the top because **both** kinds include them. Everything
else is filed by which kind it belongs to, and the Python follows the same split:
`kodo.agents` holds the parser, the registry and `TopAgent`; `kodo.agents.subagents`
holds `SubAgentSpec` and the artifact-role vocabulary only sub-agents use.

`AgentRegistry` (`_registry.py`) globs all of it at construction, expands every
`{SHARED:…}` token in one pass, and **validates the whole set**. Almost every
mistake in this document is an `AgentLoadError` at startup rather than a bad run
later — that is deliberate, and it is why the checks are worth knowing.

The catalog side is separate: `ALL_SUBAGENTS` is built at import time by globbing
`subagents/specs/*.json` (`specs/_loader.py`), so a spec file is self-registering. The
registry then cross-references spec ↔ `subagent_*.md` **by name** and fails if
either half is missing.

A sub-agent is, in one line: **"a tool with agentic behavior"** — a prompt plus a
typed contract, invoked like a tool and running like an agent.

---

## 3. Adding a sub-agent

A new sub-agent named `foo` needs the pieces below. Miss one and the registry
raises `AgentLoadError` at construction.

### 3.1 The prompt — `src/kodo/agents/subagents/subagent_foo.md`

- **Filename stem must be exactly `subagent_foo`**, matching `name: foo`.
- **Frontmatter** — see the [full key table](#frontmatter-keys) in §11. The
  minimum is `name`, `display_name`, `capability`, `tools:`. Every tool must
  resolve to a `ToolSpec` in `kodo.toolspecs`.
- **Body must contain a `## Purpose` section**, caller-agnostic and third
  person. It becomes the **description of the caller's `run_subagent_foo`
  tool** — the only thing a caller ever reads about your agent — so write it for
  whoever is deciding whether to delegate. The registry fails fast if an agent in
  some caller's `subagents:` list has none. (Critics and entry agents are exempt:
  nobody delegates to either.)
- Do **not** write a `## Tools` section. Granted tools are never described in the
  prompt, only in the LLM `tools` argument ([TOOLS.md](TOOLS.md) §7).
- **Body must include the shared blocks it needs**, via `{SHARED:<name>}` →
  `shared_<name>.md`. That token is the *only* mechanism — there is no `bases:`,
  no `callouts:` frontmatter, no auto-append. See the
  [shared-block table](#shared-blocks) in §11 for which to include and when.
- Do **not** restate anything a shared block or a tool description already says
  (minimal edits, silent reasoning, injection resistance, the `ask_user`
  discipline).

### 3.2 The spec — `src/kodo/agents/subagents/specs/foo.json`

JSON, not Python. `"name"` must equal the filename stem, and **the file is the
whole registration** — no import to add, no list to append to.

```json
{
  "name": "foo",
  "notes": "Why this contract looks the way it does. Engineer-facing; never shown to a model.",
  "input_schema":  { "shape": "pipeline_input", "input_paths": "What this agent must read." },
  "output_schema": { "shape": "author_output" },
  "produces": { "requirements": "paths" },
  "consumes": [ { "role": "architecture", "scope": "global" } ]
}
```

**Declare schemas as a `shape`** — the name of a builder in `specs/_shapes.py`
plus its arguments — not as expanded JSON Schema. The builder owns the envelope,
so naming the shape is what keeps your agent in step when the envelope changes.
The four shapes are in the [shape table](#schema-shapes) in §11.

Use `{"shape": "raw", "schema": {…}}` **only** when the contract genuinely is
your agent's own, as `planner`, `investigator` and `web_search` do. Don't
hand-roll a shared envelope, and never declare `schema_compliance` — the engine
injects it.

Other rules:

- **`"notes"` is required by convention** (a test enforces it): the
  engineer-facing rationale, since JSON has no comments. It is never shown to a
  model — it is not a second `description`. The caller-facing text is
  `## Purpose`.
- **Unknown keys are errors** — at the top level, inside a shape, and inside a
  `consumes` entry. A misspelled `require_responsability` fails at startup
  instead of quietly dropping a field from your agent's tool.
- **Both schemas reach the model as real JSON Schema on real tools** — the input
  on the caller's `run_subagent_foo`, the output bound to `foo`'s own
  `return_result` — so write per-field `description`s for a reader with no other
  source. Nothing restates them in prose.
- `author_output` also declares the **escalation** fields (`reason`/`options`,
  with `summary` doing double duty) and requires only `summary`, so a blocked
  author can return a compliant escalation. Pair it with `{SHARED:escalation}` in
  the body — the schema half without the prompt half is inert, and a test in
  `test_subagentspecs.py` fails if you ship one without the other. See
  [TOOLS.md](TOOLS.md) §5A.
- Every critic returns the **same** shape (`critic_output` takes no arguments):
  `{findings, summary}`. There is deliberately no `accept` — the verdict is
  derived from an empty backlog ([FINDINGS.md](FINDINGS.md) §3). Each `findings`
  entry is either a new finding (no `id`) or an update to an existing one (`id`
  plus only what changed). A critic's **concern vocabulary is prose** in its
  `### Concern vocabulary` section, not a schema enum — see
  [TOOLS.md](TOOLS.md) §5A for why. Choose the kinds deliberately; they are
  free-form per critic and not coupled to engine logic.

### 3.3 Registration — nothing to do

`ALL_SUBAGENTS` is built at import time by globbing `specs/*.json`, so dropping
the file in *is* the registration. (The registry still cross-references spec ↔
`subagent_*.md` by name and fails fast if either side is missing.)

Its **order** is derived too, from the `produces`/`consumes` graph: every agent
sorts after the producers of the roles it consumes, ties break by dependency
depth then name, and an agent declaring neither sorts last. So declare your
inputs accurately and your agent lands in the right place — there is no list to
hand-order any more, and editing one can no longer paper over a wrong
declaration. A cycle is a startup error.

### 3.4 Caller wiring

The agent(s) that may spawn `foo` list it in their frontmatter `subagents:`
allow-list (e.g. `agent_guide.md`, `agent_problem_solver.md`), **and** must
declare the `run_subagent` tool — the registry rejects either half without the
other.

Listing `foo` is what mints the caller's `run_subagent_foo` tool, carrying
`foo`'s own `input_schema`; the engine gates the spawn against the same list.
**A sub-agent no caller lists can never run, and has no tool anywhere.**

Tool generation (`_registry.py`): every non-critic in the allow-list gets a
`run_subagent_<name>` tool whose description is that agent's own `## Purpose`,
plus a sentence saying whether it is a workflow stage or a standalone specialist,
plus — for an author — the review-loop contract naming its critic. A critic gets
no tool at all; what a caller needs to know about it is in its author's
description. **Nothing about a sub-agent is written into a caller's prompt** —
there is no roster.

### 3.5 Worked example: a complete new sub-agent

A standalone, read-only specialist the Problem Solver can call to audit
dependencies. It is the simplest complete shape: no artifact roles, no critic,
its own contract.

**`src/kodo/agents/subagents/specs/dependency_auditor.json`**

```json
{
  "name": "dependency_auditor",
  "notes": "Read-only auditor behind no tool of its own. Declares a raw contract rather than pipeline_input because it is not a pipeline stage: it takes no artifact roles and files no work product, so there is nothing for the engine to resolve into input_paths. Output is deliberately a flat list plus a summary — the caller decides what to do about each finding; this agent never edits anything.",
  "input_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "instructions": {
          "type": "string",
          "description": "What to audit and why — the concern to focus on (licences, pinning, unused entries, known-bad versions)."
        },
        "roots": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Project roots to audit. Omit to discover them with get_root_paths."
        }
      },
      "required": ["instructions"]
    }
  },
  "output_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "issues": {
          "type": "array",
          "description": "One entry per problem found. Empty when the dependency set is clean — say so in summary rather than inventing an issue.",
          "items": {
            "type": "object",
            "properties": {
              "dependency": { "type": "string", "description": "Package name, exactly as declared." },
              "problem": { "type": "string", "description": "What is wrong, in plain English." },
              "fix": { "type": "string", "description": "The concrete change you recommend." }
            },
            "required": ["dependency", "problem", "fix"]
          }
        },
        "summary": {
          "type": "string",
          "description": "Always required. One line: what was audited and the headline finding."
        }
      },
      "required": ["summary"]
    }
  }
}
```

**`src/kodo/agents/subagents/subagent_dependency_auditor.md`**

````markdown
---
name: dependency_auditor
display_name: Dependency Auditor
standalone: true
capability: medium
tools:
  - get_root_paths
  - find_files
  - read_file
  - run_command
---
# Dependency Auditor

You are **Dependency Auditor**. You audit a project's declared dependencies and
report what you find. You change nothing.

{SHARED:task_input}

## Purpose

Audits a project's declared dependencies — pinning, licences, unused or
duplicated entries, versions with known problems — and returns a list of
concrete issues with a recommended fix for each. Read-only: it never edits a
manifest or runs an install. Invoke it when you need the dependency set assessed
before acting on it; act on its recommendations yourself.

## How You Work

1. Locate the manifests. Use `get_root_paths`, then `find_files` for the
   project's own convention — never assume a layout.
2. Read every manifest and lockfile you find before judging any of them.
3. Report one issue per genuine problem. A dependency you merely dislike is not
   a problem; say why it matters or leave it out.

## What to Avoid

- Do not recommend a version you have not seen evidence for.
- Do not report the same problem once per file it appears in — that is one
  issue.
- An empty `issues` list is a legitimate, useful result.

{SHARED:working_rules}

{SHARED:security}
````

**Caller wiring** — in `agent_problem_solver.md`'s frontmatter:

```yaml
subagents:
  - investigator
  - planner
  - developer
  - dependency_auditor   # <- added
```

That is the whole change. No Python, no registration, no engine edit. Note what
the example does *not* have: no `{SHARED:editing}` (it holds no file-modifying
tool — including it would be an error), no `{SHARED:escalation}` (its output has
no `reason` field), no `## Tools` section, and no `produces`/`consumes` (it files
no work product, which is also why it sorts at the tail of `ALL_SUBAGENTS`).

---

## 4. Adding a top-tier (entry) agent

An **entry agent** is what a user's prompt lands on. It is selected by the
session's **workflow mode**, holds a multi-turn conversation persisted to
`session.jsonl`, and hands work to sub-agents.

Unlike a sub-agent, **it is not data-driven.** The three modes are branched on in
the engine, so adding a fourth means editing Python. Be sure you actually want
one: most "new top-level capability" ideas are better served by a sub-agent an
existing entry agent can call.

> **Planned change.** [TOP_AGENT_PLAN.md](TOP_AGENT_PLAN.md) makes top-level
> agents data-driven too — a prompt plus a `<name>.json` config beside it, with
> no engine or kodo-vsix edit. When that lands, §4.3's six engine edits and
> §4.4's VSIX grep list collapse to "add two files", the "not data-driven"
> sentence above is deleted, and the term **"entry agent"** is retired in favour
> of `agent` (with `top_agent` only where `agent` is already taken — that plan's
> §1). Until then everything in this section is accurate for the tree as it stands.

### 4.1 What an entry agent has — and has not

| | |
|---|---|
| **Has** | `agent_<name>.md`; a `subagents:` allow-list; `run_subagent`; usually `ask_user` + `post_update`; free prose replies to the user |
| **Has not** | a `SubAgentSpec`, a `specs/<name>.json`, a `return_result` grant, a `run_subagent_<name>` tool anywhere, a `## Purpose` requirement |

Two prompt rules differ from a sub-agent's:

- **`{SHARED:task_input}` is forbidden.** The registry rejects it on any agent
  with no spec — that block points at a typed task brief an entry agent never
  receives. Its real input is the user's own message.
- **`{SHARED:callouts}` belongs here.** It is *convention*, not a gate: a
  sub-agent runs inside a subsession block whose open/close callouts the client
  draws, so only an entry agent needs the rules for drawing the user's attention
  itself.

`{SKILLS}` is usually right for an entry agent (it decides *what kind of task* it
is doing) and usually wrong for a sub-agent (its caller already decided). It is
mandatory iff you grant `use_skill`.

### 4.2 The prompt — `src/kodo/agents/agent_<name>.md`

Same format as a sub-agent's, stem `agent_<name>` instead of `subagent_<name>`.
`{SHARED:working_rules}` and `{SHARED:security}` are still mandatory and still
last, in that order.

### 4.3 The engine wiring (`kodo`)

Six edits. Adding a mode called `"reviewing"` with an agent named `reviewer`:

| # | File | Change |
|---|---|---|
| 1 | `runtime/_engine/_shared.py` | Add `_REVIEWER_AGENT_NAME = "reviewer"`, beside `_GUIDE_AGENT_NAME`. |
| 2 | `runtime/_engine/_core.py` (`handle_workflow_set`) | Add `"reviewing"` to the accepted-mode tuple — anything unrecognized falls back to `"guided"`. |
| 3 | `state/_transient.py` | Add `"reviewing"` to the same tuple on the **rehydration** path, or a resumed session silently reverts to `"guided"`. |
| 4 | `runtime/_engine/_llm.py` (`_entry_agent_name`) | Add the branch mapping the mode to the agent name. This is what picks the system prompt and tool set. |
| 5 | `runtime/_engine/_turns.py` | Add `_run_reviewer_with_input(...)`, a thin wrapper delegating to the shared `_run_entry_agent(...)`. Declare it in `_proto.py`'s `EngineHost` protocol too. |
| 6 | `runtime/_engine/_worker.py` | Add the dispatch branch: `_agent_available(...)` → your `_run_*_with_input`, else `_handle_input_no_agent(...)`. |

Edits 2 and 3 are the pair most easily missed — **the mode is validated in two
places**, once when set over the wire and once when read back off disk. Getting
only the first means the mode works until the session is resumed.

`_run_entry_agent` does the real work for all entry agents: the shared
agent-agnostic message history, attachment resolution, incremental persistence.
Your wrapper should add nothing but the name.

### 4.4 The front-end wiring (`kodo-vsix`)

Only if the mode is **user-selectable**. `judge` is the worked counter-example:
it is reachable solely by sending `workflow.set` with `mode: "judge"` over the
wire (which `kodo.validator` does), and kodo-vsix's picker never offers it — so
it needed **zero** VSIX changes.

For a selectable mode, `workflowMode` is a hardcoded union in several places —
grep `'guided' | 'problem_solving'` across `src/`. At minimum:
`webview/ModeControls.tsx` (the union, the picker entry, and its description
string), `webview/App.tsx` (the two coercions), `settings-webview/types.ts`,
`extension/kodo-settings-bridge.ts`, and `extension/session-resume.ts` (the
session-list `kindLabel`).

### 4.5 Is a sub-agent enough?

Before doing any of §4.3, check the cheaper options:

- **A new sub-agent on an existing entry agent's allow-list** — one JSON file,
  one prompt, one line of frontmatter. This is almost always the answer.
- **Editing an existing entry agent's prompt** — if what you want is different
  *behavior* from the same conversation, not a different conversation.
- **A new entry agent** — only when the user genuinely needs to choose a
  different top-level mode of working, with its own tool set and its own prompt,
  and the two would contradict each other in one file.

---

## 5. Recipes by use case

### 5.1 A plain sub-agent

Invoked, does one thing, returns. No critic, no roles, no flags. Declare `name`,
`display_name`, `capability`, `tools:`; add `standalone: true` if it is not a
pipeline stage. Spec: whatever shape fits. See the
[worked example](#35-worked-example-a-complete-new-sub-agent).

### 5.2 An author ↔ critic pair

Two pieces of frontmatter, and nothing else:

- the **author** declares `critic: <name>`;
- that critic declares `role: critic` on itself.

The registry validates the pair at load time (the critic must exist and must
declare the role) and refuses a critic that declares a `critic:` of its own.

Everything follows from those two. `run_subagent_<author>` becomes a **loop**
tool — it takes an optional `max_rounds` and returns a `review` block — and the
engine spawns the critic inside that call. A caller never names a critic, never
gets a tool for one, and never iterates by hand ([TOOLS.md](TOOLS.md) §5A).

**Both halves also need the findings protocol** ([FINDINGS.md](FINDINGS.md)),
which is how they actually communicate — the loop passes no findings through the
task:

- grant `get_findings` in the `tools:` frontmatter of *both* the author and the
  critic;
- include `{SHARED:findings_author}` in the author's prompt and
  `{SHARED:findings_critic}` in the critic's — **exactly one each**. The registry
  refuses to load an agent that grants the tool with neither block, includes
  both, or includes a block without the grant.

Write the prompt so it reads identically on a first pass and a tenth: the shared
block already says "call `get_findings` first, every time", so the agent-specific
prose must not describe a separate "revision round" shape. Use
[phase blocks](#7-phase-blocks-phase) when the two rounds genuinely differ.

A critic additionally keeps its own `### Concern vocabulary` section — the `kind`
values it may use — which the schema points at rather than duplicating.

```jsonc
// specs/architect_critic.json — every critic looks like this
{
  "name": "architect_critic",
  "notes": "Stage 2 critic.",
  "input_schema":  { "shape": "pipeline_input", "input_paths": "The architecture document under review." },
  "output_schema": { "shape": "critic_output" },
  "produces": {},
  "consumes": [ { "role": "architecture", "scope": "under_review" } ]
}
```

### 5.3 An author whose work the user signs off (`user_review:`)

`user_review: true` is the second, independent review declaration: it puts the
agent's finished work product to the **user** at the approval gate before it is
accepted. It is opt-in and off by default, and it composes with `critic:` in
every combination — see [GUIDED_DEV_MODE.md](GUIDED_DEV_MODE.md) §5a for the four
shapes.

- **It also makes the call a loop**, even with no critic. A rejection is minted
  as a finding, so the agent needs `get_findings` in its `tools:` and
  `{SHARED:findings_author}` in its body — the registry enforces that pairing
  anyway, but this is the reason.
- **The registry refuses it** on a `role: critic` agent, and on any agent whose
  spec `produces` nothing. In both cases there is no work product to sign off and
  the flag would silently never fire.

### 5.4 A sub-agent whose result is a plan (`planner:`)

`planner: true` makes the agent's result **the session's work plan**: the engine
parses it, records the ordered `tasks` and the `codebase_context`, and the agent
that commissioned the plan then tracks it with `get_plan` / `plan_step_forward`
instead of restating it every round. Full spec: [PLANNING.md](PLANNING.md).

**It is a declaration, never an inference.** Nothing in the engine knows that the
agent *named* `planner` plans; a second or third planner needs no engine change.

What the flag costs you is a **contract on the spec**. `kodo.plan`'s
`PLAN_OUTPUT_FIELDS` fixes the two output field names, and the `output_schema`
must declare both **with the right types** — `tasks` an array of objects each
carrying a `title`, `codebase_context` a string. `AgentRegistry` checks all of
that at load time, because `normalize_output` type-checks nothing at run time: a
mis-typed declaration would yield no plan, silently.

The shipped `planner` (abridged — see `specs/planner.json`):

```jsonc
{
  "name": "planner",
  "notes": "A researcher that ends with a plan instead of a report …",
  "input_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "instructions": { "type": "string", "description": "The task to plan, in full …" },
        "roots":        { "type": "array", "items": { "type": "string" }, "description": "Code roots …" }
      },
      "required": ["instructions"]
    }
  },
  "output_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "plan_warranted":   { "type": "boolean", "description": "False only when the work is genuinely indivisible." },
        "reason":           { "type": "string",  "description": "Why planning was or wasn't warranted." },
        "codebase_context": { "type": "string",  "description": "Anchored briefing — real paths, real symbols." },
        "tasks": {
          "type": "array",
          "description": "The ordered plan. Empty when plan_warranted is false.",
          "items": {
            "type": "object",
            "properties": {
              "title":        { "type": "string", "description": "Short imperative name for the step." },
              "subagent":     { "type": "string", "description": "Which sub-agent the caller should invoke." },
              "instructions": { "type": "string", "description": "How to build that sub-agent's input." }
            },
            "required": ["title", "subagent", "instructions"]
          }
        }
      },
      "required": ["plan_warranted", "reason", "codebase_context", "tasks"]
    }
  }
}
```

Three things worth copying from it:

- **`raw` is the right shape here.** A planner is not a pipeline stage — it takes
  no artifact roles and files no work product — so `pipeline_input`'s envelope
  would declare `input_paths`/`for_revision_paths` it never receives.
- **No `produces` / `consumes`.** A plan is not a work product. The flag is
  orthogonal to `critic:`, `user_review:` and `standalone:`; the plan is
  initialized from whatever result the declared flow finally produces.
- **`codebase_context` comes back either way.** An unplannable task must still
  repay the investigation, since the caller is about to build it in one step on
  the strength of that briefing.

**The registry refuses `planner:`** on a `role: critic` agent (a verdict is not a
plan) and on any agent with no spec at all.

### 5.5 A standalone specialist (`standalone:`)

`standalone: true` marks an agent that is **not** part of the ordered pipeline —
invoked on demand, with no upstream dependency on another agent's output. The
default (`false`) marks a workflow agent that advances the pipeline and consumes
the artifacts of the stage before it. The flag is stated as a sentence in the
generated tool description, since it is what tells a caller whether ordering
matters.

### 5.6 An engine-driven sub-agent (no caller)

`compactor`, `web_search` and `toolchain_depsmgr` have ordinary specs but no
caller: the engine spawns them from a dedicated service. Three consequences:

- They are in **no** agent's `subagents:` list, so they get no
  `run_subagent_<name>` tool.
- `compactor` and `web_search` are additionally listed in `_DIRECT_ONLY_AGENTS`
  (`runtime/_engine/_shared.py`), which makes `_spawn_subagent` short-circuit
  them — they can never be reached through `run_subagent` even by accident.
  `toolchain_depsmgr` deliberately is *not*: its ungated tool service is the only
  path to it anyway.
- They may omit `## Purpose` (nobody reads a description for them) and, if the
  engine seeds them some way other than `_render_task_input`,
  `{SHARED:task_input}` too — `compactor` is the one agent that describes its own
  input instead.

Adding one is an engine change (a service that spawns it), not just a spec.

### 5.7 A per-component pipeline stage

Pass `"require_responsibility": true` to `pipeline_input` **only** if your agent
genuinely runs once per component (stages 5–7). It is what puts
`responsibility_code` on the schema and on the tool at all.

Everything else — every product-level stage, every critic — leaves it out, and
the engine drops a stray one before it can reach the work-product id or
`self`/`dependencies` resolution (`SubAgentSpec.takes_responsibility_code`). A
critic never declares it: the engine builds a critic's whole task, and its
`self`-scoped needs are narrowed from the work product under review.

If your agent writes artifacts for **several** components in one run, also set
`component_paths` to the name of an output field holding `{codename: path}` —
without it, `self` and `dependencies` can only match all of its files or none.
Only `functional_designer` needs this today.

---

## 6. Artifact roles: `produces` / `consumes`

Nothing is hand-written and nothing is inherited. Each spec declares, in artifact
**roles**, what it produces and what it needs (`kodo.agents.subagents._artifacts`), and
the engine resolves those needs against the session's work-product ledger
(`kodo.workproducts`) to hand the agent a fully-formed `input_paths`.

```jsonc
// specs/requirements_critic.json
{
  "produces": {},                                    // critics write findings, not artifacts
  "consumes": [
    { "role": "requirements",  "scope": "under_review" },
    { "role": "architecture",  "scope": "global" },   // "global" is the default
    { "role": "narrative",     "scope": "global" }
  ]
}
```

- **`produces`** maps a role to the output field carrying its paths. An ordinary
  author maps its one role to `"paths"` (`PRODUCES_REMAINDER`) — "everything I
  reported". An agent filling two roles names a field per role, and the role
  mapped to the remainder gets whatever no named field claimed; that is how
  `functional_designer` keeps the Design Plan out of the pile of Functional
  Designs it wrote in the same run.
- **`consumes`** is a list of `{role, scope, required}`. `required` defaults to
  `true` and `scope` to `"global"`. The scopes are in the
  [scope table](#artifact-scopes) in §11. Declare a scope that can legitimately
  be empty as `"required": false`.
- Labels are the role itself for a single file, `<role>_<basename>` for several.
  A role declared at two scopes (`coder` wants its own design *and* its
  neighbours') is accumulated into one labelled group, not labelled twice.
- **Callers never pass paths.** `input_paths` and `for_revision_paths` are in
  `kodo.toolspecs.ENGINE_OWNED_TASK_FIELDS`: declared on your agent's
  `input_schema` (so the rendered task brief describes them) but stripped from
  the `run_subagent_<name>` tool, so no caller can set them or be asked for them.
  Do not write a prompt that tells a caller to supply a path. The stripping
  follows the declaration: an agent with an empty `consumes` has no resolution
  behind it, so it keeps a caller-supplied `input_paths` (the non-pipeline
  `developer` is the only one today). **Declare roles or own your paths — never
  neither.**

**The role vocabulary is closed.** `ALL_ROLES` in `_artifacts.py` lists every
legal role, and the registry refuses an agent naming one that is not there. If
your agent produces a genuinely new *kind* of artifact, add the role constant to
`_artifacts.py` and to `ALL_ROLES` first — do not reuse a near-miss role, because
the scope machinery will then hand your document to stages that asked for
something else.

The registry validates all of this **at load time**: an unknown role or scope, a
`produces` entry naming an output field the schema does not declare, or a
consumed role that no agent produces, all raise `AgentLoadError`. A typo stops
the server rather than one sub-agent, four stages later.

**When you add an agent**, declare both halves. If it writes files, give it a
`produces` entry so later stages can find them; if it reads any, declare the
roles rather than expecting a caller to pass paths.

An unmet **required** need **refuses the spawn** — the caller gets an escalation
naming the role instead of an agent that will invent the missing input. So mark a
need `"required": false` when it can legitimately be absent (a component with no
neighbours has no dependency designs), and make sure the stage that produces each
required role really does run first.

> Until 2026-09-04 the engine passed a hardcoded `{"target": <path>}` — one path,
> under a label in no agent's vocabulary — to every critic. Six of the seven
> declared more inputs than they received; `requirements_critic`, promised the
> architecture and given only the document, reconstructed the architecture's path
> from the worked example in its own schema description and read the resulting
> nonexistent file ~1133 times. A short-lived `inherit_author_inputs` stopgap
> forwarded the author's paths instead; declared roles replaced it on 2026-09-05.

---

## 7. Phase blocks (`{PHASE:…}`)

An agent spawned in a review loop does two different jobs across rounds: writing
from its inputs, and resolving a findings backlog against what it already wrote.
Give it different text for each by wrapping the text in a phase block:

```markdown
{PHASE:initial}
…only on a first pass…
{/PHASE}

{PHASE:revision}
…only when working the backlog…
{/PHASE}
```

The two phases are `initial` and `revision`; the engine picks between them from
whether a work product with members already exists. Writing the block *is* the
opt-in — there is no frontmatter flag — and an agent with no blocks renders
identically in every phase. Blocks may not nest, must be closed with a bare
`{/PHASE}` (never `{/PHASE:name}`), and are only valid in an agent that has a
`SubAgentSpec`; each of those is a load-time error. See
[GUIDED_DEV_MODE.md](GUIDED_DEV_MODE.md) §5b, including why this is a conditional
section rather than a separate `corrector` sub-agent.

---

## 8. Editing an existing agent

**Editing a prompt only** (wording, a new section, a tightened rule): change the
`.md`, then re-render it (§10) and run `test_agents.py`. Nothing else moves.

**Editing a contract** (a new output field, a changed description): change the
`.json`. If you add a field that `produces` maps to, or remove one it maps to,
the registry tells you at startup. Remember both schemas are model-facing — a
field description is the only explanation its reader gets.

**Adding a tool** to an agent: add it to `tools:`, then check the three paired
rules — a `modifies_files` tool requires `{SHARED:editing}`; `get_findings`
requires exactly one of `{SHARED:findings_author}` / `{SHARED:findings_critic}`;
`use_skill` requires `{SKILLS}`.

**Moving a responsibility to a new agent** (as when the Test Plan behavioral
review was split out of `test_coder` into the new `test_design_critic`):

- **Re-point the pairing**: change the author's `critic:` to the new critic.
- **Strip the moved role** from the old agent — its prompt sections, its
  frontmatter `tools:` that only served the old role, its `role:`/`critic:`
  frontmatter, and its spec (a dual-role `oneOf` collapses back to a single shape
  once one role leaves).
- **Hunt every mention**: `grep -rn` the old agent name across `src` **and**
  `doc` and `test`. Update the guide pipeline, the INTERNALS agent-tools table,
  any `oneOf`/dual-role comments in `toolspecs/_compliance.py`, escalation
  example `reason` strings, and the pairing assertions in `test_agents.py`.
  Escalation `reason` strings and critic `kind`s are free-form (no engine
  branches on them), so they are safe to rename — but stale ones mislead the next
  reader.
- **Memory + docs**: update `project_kodo.md` and the doc set in the same change
  (see the repo's memory-discipline rule in `CLAUDE.md`).

**Deleting an agent**: remove the `.md` and the `.json`, and remove it from every
`subagents:` allow-list. A spec with no prompt (or the reverse) is a startup
error, which is how you find the half you forgot.

### Pipeline placement (guide prompt)

If your agent is a pipeline stage (not `standalone`), update
**`agent_guide.md`**: the numbered **"The Pipeline You Run"** list, the **Stage →
agent map** table, and any cascade/escalation prose that names the stage. The
guide prompt is the source of truth for stage order; a tool description says what
its agent does, never where it sits in the sequence. Keep author and critic
adjacent in the `subagents:` list so the generated tools read in pipeline order.

Note that `ALL_SUBAGENTS`'s derived order is a *reading and diagnostic* order,
not the stage sequence — the graph does not know that end-to-end testing follows
coding, because the e2e specs consume designs rather than code. The Guide's
prompt is what sequences a run.

---

## 9. Tests

- `test/test_subagentspecs.py` — schema well-formedness and registry wiring,
  auto-parametrized over `ALL_SUBAGENTS`, so a new agent is covered for free; add
  a focused test if it has notable concern kinds.
- `test/test_subagent_spec_loading.py` — the JSON catalog itself: every shipped
  file loads, malformed ones are rejected with a named error, and the derived
  order really does put producers before consumers. Also parametrized over the
  shipped files, so a new spec is covered for free.
- `test/test_agents.py::test_shipped_guide_tools_carry_every_pipeline_pairing`
  checks each author's tool names its critic — update it when you change a
  pairing.
- `test/test_agents.py` also parametrizes a scan over **every** shipped
  `agent_*.md` / `subagent_*.md`: required blocks present, security last, no
  unknown block, editing-block-iff-write-tools,
  findings-block-iff-`get_findings`, no retired `bases:`/`callouts:`
  /`{PLACEHOLDER:…}`. That is where a forgotten token should fail — at build
  time, not on a running server.
- Both build `AgentRegistry(_REAL_AGENTS_DIR)`, the last-resort runtime copy of
  the same checks.

---

## 10. Run / verify

From `kodo` (deps `aiohttp` etc. may be absent in some envs — the agent/spec
tests don't need them):

```bash
PYTHONPATH=src python3 -m pytest test/test_agents.py test/test_subagentspecs.py test/test_main.py -q
PYTHONPATH=src python3 -m pytest test/test_subagent_spec_loading.py -q
PYTHONPATH=src python3 -c "from pathlib import Path; from kodo.agents import AgentRegistry; AgentRegistry(Path('src/kodo/agents'))"
ruff check src/kodo/agents/subagents/specs/
```

Your JSON must also **ship**: `pyproject.toml`'s wheel/sdist `include` covers
`src/kodo/**/*.json`, so a file added under `specs/` is packaged automatically —
but a spec stored anywhere else would not be.

Then **read the prompt your agent will actually receive** — your body with every
`{SHARED:…}` token expanded in place:

```bash
PYTHONPATH=src python3 -m kodo --system-prompt foo --model claude-opus-5
```

This is the fastest way to see the blocks land where you meant them to, and it
works for entry agents and sub-agents alike. It will **not** show your concrete
task or its schema — no schema is ever restated in a sub-agent's own prompt
(input or output). The input schema reaches a *caller* as real JSON Schema on
`run_subagent_foo`; the sub-agent itself sees concrete values, per-field
descriptions, and the `return_result` reminder rendered fresh per call under
`## Input Parameters` in its first user turn (`_render_task_input`,
[SESSIONS.md](SESSIONS.md) "Typed sub-agent interface") — check that path if you
need to verify a field's description reads well. It calls `AgentRegistry.get`
itself, so what `--system-prompt` prints is what the engine sends (see
[INTERNALS.md](INTERNALS.md) §9a). `--model`/`-m` only has to resolve — every
model gets the same prompt today — and can be omitted to use the first installed
model in the local registry. `test/test_main.py` sweeps *every* packaged agent
through it, so a new agent that fails to render fails there too.

The same CLI's `--tools foo --model claude-opus-5` prints the agent's granted
tools in the exact OpenAI wire shape, if you need to check that side too.

On Windows the canonical check is `mise exec node -- npm run check-types && ...`
in `kodo-vsix` for the front-end; the Python side is pytest + ruff as above.

---

## 11. Reference tables

### Frontmatter keys

Both `agent_*.md` and `subagent_*.md` use the same parser (`_loader.py`).

| Key | Applies to | Meaning |
|---|---|---|
| `name` | both | **Required.** Must match the filename stem after the prefix. |
| `display_name` | both | UI name. Defaults to a title-cased `name`. |
| `capability` | both | `max` / `high` / `medium` / `low`. Default `medium`. |
| `tools:` | both | Tool names; each must resolve to a `ToolSpec`. |
| `subagents:` | both | Allow-list of spawnable sub-agents. Requires the `run_subagent` tool. Order is preserved — it is the order the generated tools are built in. |
| `critic:` | sub-agent | Names this author's critic. Makes `run_subagent_<name>` a review loop. |
| `role: critic` | sub-agent | Marks a critic: no tool of its own, result is a verdict. Cannot also declare `critic:`, `user_review:` or `planner:`. |
| `standalone: true` | sub-agent | Not part of the ordered pipeline. |
| `user_review: true` | sub-agent | The user signs the work product off. Refused on a critic, or on an agent that `produces` nothing. |
| `planner: true` | sub-agent | The result **is** a plan. Requires `tasks` + `codebase_context` in the `output_schema`. |

### Shared blocks

`{SHARED:<name>}` → `shared_<name>.md`. Placement is the author's; the convention
is contracts where the body refers to them, `task_input` right after the opening
identity paragraph, and the rule blocks closing the file.

| Block | When |
|---|---|
| `working_rules` | **Mandatory**, second-to-last. |
| `security` | **Mandatory**, last. |
| `editing` | **Mandatory iff** you granted a tool whose `ToolSpec.modifies_files` is true. Forbidden otherwise. |
| `task_input` | Sub-agents with a spec. **Forbidden** on an entry agent. Omit only if the engine seeds the agent some other way (today: `compactor`). |
| `findings_author` / `findings_critic` | **Exactly one, iff** you granted `get_findings`. |
| `escalation` | If the agent can hand a blocker back. Pairs with `author_output`. |
| `dependencies` | If the agent reads or writes `DEPENDENCIES.md`. |
| `callouts` | Entry agents, by convention — a sub-agent's callouts are drawn by the client. |
| `{SKILLS}` | Not a shared block. **Mandatory iff** you granted `use_skill`; forbidden otherwise. |

### Schema shapes

Declared in a spec's `input_schema` / `output_schema`; built by
`specs/_shapes.py`.

| Shape | Arguments | Use for |
|---|---|---|
| `pipeline_input` | `input_paths` (required), `require_input_paths`, `require_responsibility`, `extra_properties`, `extra_required` | Any file-backed pipeline sub-agent's input. |
| `author_output` | `extra_properties` | A file-writing author or solo agent's output. Carries the escalation fields. |
| `critic_output` | *(none)* | Every critic's output. |
| `raw` | `schema` (required) | An agent whose contract is genuinely its own. Used verbatim. |

### Artifact scopes

| Scope | Means |
|---|---|
| `global` (default) | The single current work product filling this role for the project. |
| `self` | That role, narrowed to this spawn's `responsibility_code`. |
| `all` | Every work product filling this role. |
| `under_review` | The work product this critic round is reviewing — supplied by the engine from the round, never looked up. Exempt from the "somebody must produce it" check. |
| `dependencies` | The designs of components this one consumes or is consumed by, from the architect's component graph. Declare `"required": false` — a project whose architect predates the graph resolves it to nothing. |

### Spec JSON keys

| Key | Type | Default |
|---|---|---|
| `name` | string | **required**, must equal the filename stem |
| `input_schema` | shape object | **required** |
| `output_schema` | shape object | **required** |
| `notes` | string | `""` — engineer-facing; never shown to a model |
| `produces` | `{role: output field}` | `{}` |
| `consumes` | `[{role, scope, required}]` | `[]` |
| `component_paths` | string | `""` |
