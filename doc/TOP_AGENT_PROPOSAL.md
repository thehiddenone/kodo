# Proposal — User-Installed Top-Level Agents

> Status: **proposal, not implemented**. Written 2026-09-16.
> Question asked: *"what should be done to allow creation of new top level agents
> without having to write code? Idea: an interface between prompts and the engine
> mediated by frontmatter and JSON definitions."*
> **Decision recorded 2026-09-16:** the target outcome is *"a user drops a file
> in `~/.kodo/` and a new top-level agent appears"*. Everything below is a
> roadmap to that, not a menu of alternatives.
> **Phases 1-3 are now planned in detail in [TOP_AGENT_PLAN.md](TOP_AGENT_PLAN.md)**
> (decisions recorded 2026-09-16: single packaged root but do the validation
> refactor now; rename `workflow.set` to `agent.set`; server-declared default with
> a user override; keep a `selectable` flag; rename the `guided_state` `workflow`
> field too). This document stays the rationale record — why those options and not
> the others.
> **Vocabulary note:** this document says *"entry agent"* throughout. That term is
> retired — see [TOP_AGENT_PLAN.md](TOP_AGENT_PLAN.md) §1, which uses `agent`, and
> `top_agent` only where `agent` is already taken. Read "entry agent" as "top-level
> agent" below.
> Reference: [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) §4,
> [SKILLS.md](SKILLS.md), [SECURITY.md](SECURITY.md),
> [WS_PROTOCOL.md](WS_PROTOCOL.md), [TOOLS.md](TOOLS.md).

---

## 0. TL;DR

**The destination is reachable, and cheaper than it looks — because Kōdo already
built this exact pattern once, for skills.**

Five findings, in order of importance:

1. **The engine is already entry-agent-agnostic.** `_run_entry_agent`
   ([_turns.py:166](../src/kodo/runtime/_engine/_turns.py#L166)) takes the agent
   name as a parameter and does everything else generically. The three
   `_run_*_with_input` wrappers add nothing but a name. Making the mode→agent
   mapping data is a **net deletion** of ~70 lines.

2. **`~/.kodo/skills/` is a working prototype of `~/.kodo/agents/`.** Fail-soft
   loading of untrusted files, a broken-entry row the user can see and delete,
   live pickup with no restart, list/delete/install-from-repo/install-from-local
   over the control connection, conflict-and-overwrite handling — all of it
   exists ([_store.py](../src/kodo/skills/_store.py),
   [_app.py:933-1000](../src/kodo/server/_app.py#L933),
   `skills.install*` at [_messages.py:361-422](../src/kodo/transport/_messages.py#L361)).
   Most of phase 4 is a port, not an invention.

3. **The trust question is not the one it looks like.** `SecurityLayer` judges
   **per call, on the tool and its input** — never on which agent called it. A
   user agent granting `run_command` gets byte-identical gating to the Problem
   Solver, which already holds `run_command` + `edit_file` + `create_file` and
   which the same user can already type anything into. The capability delta is
   ≈ zero. What changes is the **provenance of instructions** — see §5. That
   reframing makes the mitigation much lighter than "cap what a user agent may
   grant".

4. **There are exactly three genuinely unsolved problems:** the registry is a
   validated-at-construction process singleton and has no reload path (§4.6);
   the cross-agent validation pass raises on first error and so cannot blame one
   agent (§4.3); and the VSIX mode picker is two hardcoded rows (§4.8). Nothing
   else on the path is new work.

5. **Two latent bugs are already in the tree** and must be fixed on the way —
   §2.3. One silently disables the findings backlog for any fourth mode.

---

## 1. Inventory: what an entry agent already is

### 1.1 Already data — inherited free by a new entry agent

| Concern | Where it lives |
|---|---|
| System prompt | the `agent_<name>.md` body |
| Tool grants | frontmatter `tools:`, validated against `kodo.toolspecs` at load |
| Terminal behavior | a *tool* property. `judge` ends its run via `submit_evaluation`, which sets `ToolContext.stop_requested` from its own dispatch — no engine branch exists for it |
| Sub-agent roster | frontmatter `subagents:`, gated at dispatch by `AgentRegistry.allowed_subagents` |
| Capability tier / display name | frontmatter `capability:` / `display_name:` |
| Prompt validation | `{SHARED:working_rules}` + `{SHARED:security}` required ([_registry.py:325](../src/kodo/agents/_registry.py#L325)); editing discipline bound to `modifies_files`; findings blocks bound to `get_findings`; `{SKILLS}` bound to `use_skill`; `{SHARED:task_input}` and `{PHASE:…}` **rejected** on any agent with no `SubAgentSpec` |
| Registration | `AgentRegistry` globs `agent_*.md` beside `subagent_*.md` ([_registry.py:556](../src/kodo/agents/_registry.py#L556)) — dropping the file in *is* the registration |

Every one of those rules is keyed on **"has a `SubAgentSpec`"**, never on a
hardcoded list of entry-agent names. A fourth entry agent inherits all of them
unchanged — **including a user-authored one**. That is the single most valuable
pre-existing property for this project: see §4.4.

### 1.2 Already generic in the engine

| Mechanism | Evidence |
|---|---|
| The entry-agent turn | `_run_entry_agent(agent_name, …)` — [_turns.py:166](../src/kodo/runtime/_engine/_turns.py#L166) |
| One shared history across modes | `_main_messages` is agent-agnostic; each line is tagged `entry_agent` |
| Crash resume | `_last_entry_agent` reads that tag — *"any entry agent may have been holding the floor … resume must not assume the Guide"* ([_resume.py:146](../src/kodo/runtime/_engine/_resume.py#L146)) |
| Tool dispatch | `_make_dispatcher(agent_name, …)` — [_turns.py:1208](../src/kodo/runtime/_engine/_turns.py#L1208) |
| Session greeting | `SessionGreeter` is mode-blind |

### 1.3 Still code

**`kodo`:** `_shared.py:23-31` (three name constants) · `_llm.py:458`
(`_entry_agent_name`'s `if` ladder) · `_worker.py:104-119` (dispatch branch) ·
`_turns.py:119-164` + `_proto.py:167-183` (three wrappers) · `_core.py:650` and
`_transient.py:1241` (**the same accepted-mode tuple, twice**) ·
`_get_findings.py:30` + `_guided_dev_status.py:28` (`ctx.mode != "guided"`) ·
`validator/_harness.py:82` + `validator/__main__.py:253`.

**`kodo-vsix` — 8 files** hardcode `'guided' | 'problem_solving'`:
`webview/ModeControls.tsx` (`_MODE_DESC` :18-19, the prop union :368-369, `isPS`
:484, the frozen-note ternary :518, two literal `<MenuOption>` rows :519-527) ·
`webview/types.ts` (:670-671, :837-838) · `webview/App.tsx` (:528-529) ·
`webview/reducer.ts` (:1355-1356) · `session/mode-toggle-controller.ts`
(:27-28, :191-199, :245) · `session/types.ts` (:45-46) · `session/controller.ts`
(:321) · `extension/session-resume.ts` (:107).

---

## 2. What must be true before `~/.kodo/agents/` can exist

### 2.1 The mode→agent mapping must be data

Seven of the ten Python sites above are one fact — *"which agent backs which
mode"* — written seven times. A registry-derived table collapses them:

```python
# _llm.py — 7 lines become 1
def _entry_agent_name(self: EngineHost) -> str:
    return self._registry.entry_agent_for_mode(self._session.workflow_mode)

# _worker.py — 16 lines become 5
name = self._entry_agent_name()
if self._agent_available(name):
    await self._run_entry_agent(name, text, attachments, nudge_detail=nudge_detail)
else:
    await self._handle_input_no_agent(name, text)
```

…and the three wrappers plus their `EngineHost` declarations (~50 lines) are
deleted outright. **The diff is negative**, and this is true whether or not D
ever ships.

### 2.2 `kodo.state` cannot see the registry — and that is the fix

`src/kodo/state/` imports **nothing** from `kodo` (verified). So `TransientStore`
structurally cannot consult `AgentRegistry`, which means the rehydration-side
validation at `_transient.py:1241` must simply **go away**: store the raw string,
and coerce it through the registry when the engine restores the session at
[_core.py:396](../src/kodo/runtime/_engine/_core.py#L396).

That is also the permanent fix for the hazard ADDING_A_SUBAGENT.md §4.3 already
flags — *"the mode is validated in two places… Getting only the first means the
mode works until the session is resumed."* Under D, where modes are open-ended,
a second validation site is not a hazard but a guaranteed bug.

### 2.3 Two latent bugs that block a fourth mode

**(a) `ctx.mode != "guided"`** — [_get_findings.py:30](../src/kodo/tools/_get_findings.py#L30),
[_guided_dev_status.py:28](../src/kodo/tools/_guided_dev_status.py#L28).
`_make_dispatcher` passes `effective_workflow_mode` to **every** agent,
sub-agents included. A user agent that spawns `coder` / `architect` / any critic
gets `get_findings` answering `{"error": …}` — and the entire author/critic
review loop is built on that backlog ([FINDINGS.md](FINDINGS.md) §3).

**Fix: delete both checks.** `guided_dev_status` is granted to exactly one agent
(`agent_guide.md`), so its mode check is pure redundancy behind the tool grant.
`get_findings` is granted to the guide plus 17 pipeline sub-agents, which no
other entry agent can reach *unless it lists them* — in which case refusing the
tool is the wrong answer. **The grant is already the gate**, and a second gate
keyed on a value about to become user-authored is a liability.

**(b) `coerceWorkflowMode`** ([session/types.ts:45](../../kodo-vsix/src/session/types.ts#L45))
maps anything that isn't `'problem_solving'` to `'guided'`. A session persisted
as `"judge"` — which the validator creates today — already displays as
**"Guided"** in the session picker. Under D every user mode would do the same.

---

## 3. Roadmap

Four phases. Unlike the previous draft, **these are steps, not alternatives** —
D requires all four. What differs is sequencing risk and what is independently
useful if you stop early.

| Phase | Deliverable | Independently useful? |
|---|---|---|
| **1** | Engine generic: registry-owned mode table, one dispatch branch, three wrappers deleted, mode coercion out of `kodo.state`, both §2.3 bugs fixed | **Yes** — net code deletion, two bug fixes, no new surface |
| **2** | `AgentRegistry` gains a second, **fail-soft** root + a `reload()`; `modes/*.json` catalog; `agents.list` / `agents.delete` / `agents.reload` | Partly — enables in-repo agent authoring without a rebuild |
| **3** | Server serves the mode catalog; VSIX picker renders `modes.map(…)` | **No** — but **mandatory for D**: a user agent nobody can select is useless |
| **4** | `ensure_root()`, install surface (`agents.install_local` / from repo), trust prompt, `kodo agents scaffold` | Completes D |

Phase 3 was optional in the previous framing. **With D as the destination it is
not.** That is the single biggest consequence of the decision.

---

## 4. The design, question by question

### 4.1 Layout

```
~/.kodo/agents/
  agent_reviewer.md          # frontmatter + system prompt
  reviewer.json              # mode metadata (optional; see 4.2)
```

Flat files, not directories — an entry agent is one prompt plus ~6 scalars,
where a skill is a bundle with `scripts/`, `references/`, `assets/`. Mirrors
`~/.kodo/skills/` in every other respect, including
`ensure_root()` on startup so the folder the user is told about actually exists
(exact precedent: `SkillStore(kodo_skills_dir()).ensure_root()` at
[_app.py:2649](../src/kodo/server/_app.py#L2649)).

### 4.2 Frontmatter or JSON?

**Both, split by audience.** The `.md` is what the **model** reads; the `.json`
is what the **engine and the UI** read:

```json
{
  "name": "reviewer",
  "notes": "Engineer-facing rationale. Never shown to a model or a user.",
  "mode": "reviewing",
  "label": "Reviewer",
  "description": "One agent audits an existing codebase against its specs.",
  "rank": 30,
  "selectable": true
}
```

Three reasons to prefer JSON over more frontmatter keys, all sharpened by D:

- The frontmatter parser ([_loader.py:294](../src/kodo/agents/_loader.py#L294))
  is hand-rolled, flat-only, and returns everything as `str`. It already carries
  bespoke coercion for `standalone` / `user_review` / `planner`. Adding
  `rank: 30` and `selectable: true` makes that worse — and under D it is parsing
  **third-party text**, where every added key is another way to be wrong.
- `mode` and `label` are **public protocol values**: persisted in session state,
  sent over the wire, shown in the picker. They deserve typed, schema-checked
  declaration, not string-scraping.
- The `notes` convention — engineer rationale, never shown to a model — has no
  home in frontmatter.

Make the JSON **optional**: absent, `mode` and `label` default from the agent's
`name` and `display_name`, and `selectable` defaults true. The simplest possible
user agent is then genuinely **one file**.

Resolve the overlap explicitly: **the JSON wins, and `display_name:` is rejected
on an `agent_*.md` that has one.** Enforce at load; do not let two labels drift.

### 4.3 Two roots, two validation regimes

This is the central architectural change, and it contradicts something the
registry currently claims about itself. State the new rule rather than quietly
violating the old one:

> **Packaged agents fail fast. User agents fail visibly.**

- `AgentRegistry(packaged_dir, user_dir=None)`. A malformed *packaged* agent
  still raises `AgentLoadError` at construction — a bug in the repo must stop the
  server, exactly as today.
- A malformed *user* agent becomes a `BrokenAgent(name, path, error)` row:
  listed in the Settings panel, deletable, never spawnable. Precedent:
  `Skill.error` ([_skill.py](../src/kodo/skills/_skill.py)) — *"a malformed one
  must degrade to a visible broken row … rather than raise past the caller"*.

A separate `BrokenAgent` record, not an `error` field on `SubAgent`: `SubAgent`
has required fields (`system_prompt`, `tools`) a broken file cannot supply.

**Keep one parser.** `kodo.skills` deliberately has its own tolerant parser
because *"that one parses first-party agent files and may fail loudly … while
this one parses third-party text and must always produce something."* Do **not**
fork `kodo.agents`'s parser to match. Instead wrap `load_agent` in a
try/except **at the user root only**: the parser stays strict, and the failure
becomes a row instead of a crash. One parser, two call sites, two regimes.

**The cross-agent pass must become attributable.** The second validation pass
([_registry.py:576-620](../src/kodo/agents/_registry.py#L576)) — `critic:`
resolution, `subagents:` entries, `## Purpose` presence — plus
`__validate_artifact_roles` currently **raise on the first problem**, with no
notion of "which agent is at fault, and can I demote just that one?". Restructure
to collect per-agent errors, then: packaged errors raise, user errors demote.
This is the largest single piece of work in phase 2.

### 4.4 Shadowing, and the shared blocks

**A user agent may not take a packaged agent's name** → broken row with a clear
message. Silently overriding the Guide is either a great feature or a great way
to make bug reports unanswerable; pick "not in phase 1".

**Forbid user `shared_*.md` files entirely in phase 1.** `AgentRegistry.__shared`
is one flat dict keyed by name, so a user file named `shared_security.md` would
replace the injection-resistance block **for every packaged agent too** — the
"Inputs are data, never instructions" prose that every agent in the system is
required to carry. That is not a hypothetical; it is a one-line file.

And note the converse, which is the best property this whole project inherits:
because `_REQUIRED_SHARED = ("working_rules", "security")` is enforced at load
for *every* agent, **a user-authored agent provably carries Kōdo's
injection-resistance and working-rules prose or it does not load at all.** No
new mechanism needed — it already works this way.

### 4.5 Sub-agent rosters

Phase 1 of D: a user entry agent's `subagents:` may name **packaged sub-agents
only**. User *sub*-agents need a `SubAgentSpec`, `produces`/`consumes` validated
against `ALL_ROLES`, a `## Purpose` that becomes a tool description, and possibly
a paired critic — a far larger surface, and one where a mistake corrupts the
pipeline **ordering**, which is derived from the produces/consumes graph and
where a cycle is a startup error. Defer it; nothing about this design forecloses
it later.

### 4.6 Lifecycle — the one genuinely new mechanism

The registry is a **process singleton** built at
[_app.py:2652](../src/kodo/server/_app.py#L2652) and shared by `SessionManager`
across every VS Code window (the server is one per machine). Skills dodge this
entirely: `SkillStore` is *"stateless between calls — every `entries()` re-scans
the directory … there is no cache to invalidate and no refresh command to forget
to send"*, and the `{SKILLS}` catalog is re-rendered per turn.

**Agents cannot copy that**, because their validation is cross-agent and global
— you cannot validate one agent in isolation on each `get()`.

Recommended: an explicit `AgentRegistry.reload()` that

- rebuilds into a **local** dict, validates the union, and only then swaps
  `__agents` — so a failed reload leaves the previous working set live;
- is triggered by `agents.install` / `agents.delete` / an explicit
  `agents.reload`, mirroring the existing `config.reload` idiom
  ([_messages.py:440](../src/kodo/transport/_messages.py#L440));
- needs **no locking**: `get()` returns frozen dataclasses, so a session mid-turn
  finishes on the definition it already holds and picks up the new one on its
  next turn. That is the correct semantics, not a compromise.

### 4.7 Session durability

Today a missing entry agent reaches `_handle_input_no_agent`
([_worker.py:262](../src/kodo/runtime/_engine/_worker.py#L262)), which logs a
warning, flips the phase back to `intake`, and **shows the user nothing**. That
is fine while it cannot happen. Under D — where the user can delete the agent
their session is running — it is a silent dead end: the user types a prompt and
nothing at all occurs.

Needs a visible notice naming the missing mode and offering the default, plus a
decision on whether to auto-fall-back or hold. Small fix; easy to forget.

### 4.8 The client

Mandatory for D (§3). `hello.ack` gains
`workflow_modes: [{id, label, description, rank}]` (selectable entries only)
plus `default_workflow_mode`. Exact precedent, documented at
[_messages.py:327-338](../src/kodo/transport/_messages.py#L327):
`housekeeper_llm.get.ack`'s `options` array exists *"so the panel renders one
radio button per catalog entry with no id hardcoded client-side; adding a new
entry to that dict is the only change needed for a new radio button to appear."*

`session.list` rows already carry `workflow_mode`
([_session_manager.py:309](../src/kodo/server/_session_manager.py#L309)) — add
`workflow_label` so `session-resume.ts`'s `kindLabel` stops being a ternary.

Client-side the union becomes `string` and `ModeControls`'s two literal rows
become a `.map`. **Cost to weigh:** this trades away TypeScript's
exhaustiveness checking on the mode union. The mitigating fact is that after
this change there is no behavioral switch on mode left client-side — the union
is guarding nothing. Version skew is a non-issue: the VSIX installs
`py-kodo==${extVersion}` (`uv-setup.ts:710,813`), so the two ship in lockstep.

---

## 5. The threat model, reframed

The instinctive framing — *"a user agent granting `run_command` is a privilege
escalation"* — does not survive reading [kodo.security](../src/kodo/security/):

- `SecurityLayer.evaluate` is **fully deterministic**, judges **per call on the
  tool and its input**, and **never** considers which agent is calling. A user
  agent granting `run_command` gets byte-identical gating to the Problem Solver.
- The Problem Solver **already** holds `run_command`, `edit_file`,
  `create_file`, `create_directory` and `toolchain_deps`, and the same user can
  already type any instruction into it.

**The capability delta is ≈ zero. What changes is the provenance of
instructions.** Today the instruction source is a message the user typed one
second ago. Under D it is a file that persists, is shareable, may have been
downloaded from a repo, and — critically — runs unattended: while Autonomous is
in effect the layer operates as `permissive` because *"there is no user to ask"*.

That reframing changes the mitigation:

- **Do not cap what a user agent may grant.** It would make user agents strictly
  less capable than the Problem Solver the same user already drives, while
  closing none of the actual gap (a persuasive prompt, not a tool list).
- **Do gate on first appearance.** Show what a newly discovered agent grants and
  require an explicit enable — a "trust this extension" prompt, not a capability
  cap. The panel already has the shape: `skills.install.ack` returns
  `installed`/`conflicts` and the panel confirms before overwriting.
- **Do consider a narrower autonomous rule:** refuse to run a *user-authored*
  entry agent while Autonomous is in effect unless that specific agent has been
  enabled for autonomous use. This targets the real risk — unattended execution
  driven by a third-party prompt — precisely, and costs nothing in the
  interactive case.

---

## 6. What is still genuinely hard

1. **Two validation regimes in one class.** Phase 2 makes `AgentRegistry` both
   fail-fast and fail-soft depending on which root a file came from. That is
   defensible, but it must be *written down in the class docstring* as the new
   contract, not left for a reader to infer from a try/except.
2. **Open-world testing.** `test_agents.py` and `test_subagentspecs.py` sweep a
   closed, curated set exhaustively. Phase 2 needs a fixture corpus of
   deliberately broken user agents — bad YAML, unknown tool, missing
   `{SHARED:security}`, name collision, dangling `critic:`, `subagents:` entry
   that does not exist — each asserting it produces a **row**, not an exception,
   and that the *other* agents still load.
3. **Prompt quality is untouched by all of this.** `agent_guide.md` is 326 lines
   of prompt; the wiring this proposal removes is about 20. D makes adding an
   entry agent *possible*, not adding a *good* one *easy*. Ship a
   `kodo agents scaffold <name>` that emits a valid skeleton with the mandatory
   shared blocks already in place — otherwise the first thing every user hits is
   an `AgentLoadError` about `{SHARED:working_rules}`.

---

## 7. Decisions still needed

1. **`selectable: false` for user agents — keep it?** It exists for `judge`
   (wire-only, validator-driven). A user agent that opts out of the picker is
   only reachable by something sending `workflow.set` directly. Probably keep
   for symmetry, but it has no user story yet.
2. **Auto-fall-back or hold** when a session's mode names a deleted agent
   (§4.7)?
3. **The autonomous rule in §5** — narrow gate on user-authored entry agents, or
   treat them identically to packaged ones? This is the only decision here that
   is genuinely about risk appetite rather than engineering.
4. **Does `mode` stay an alias, or collapse into the agent name?** Recommended:
   keep the alias, defaulted to `name`, so the three shipped modes keep their
   persisted values (`"guided"`, `"problem_solving"`, `"judge"`) and every new
   agent gets `mode == name` for free. The collapse stays available later.
