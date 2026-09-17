# Plan — Top-Level Agents as Prompt + Config

> Status: **phases 1-2 implemented 2026-09-16** (§4.1a and §4.3a record what
> actually landed and the deviations); phases 3-5 still planned.
> Written 2026-09-16.
> Rationale, design space and rejected alternatives: [TOP_AGENT_PROPOSAL.md](TOP_AGENT_PROPOSAL.md).
> Supersedes [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) §4.3-4.4 **once landed** —
> until then that section is still accurate for the tree as it stands.
> Touches both repos: `kodo` (`src/kodo/…`) and `kodo-vsix`.

Turn Guide, Problem Solver and Judge into **prompt + config** definitions over
one generic engine, and give kodo-vsix a data-driven agent picker.

---

## 0. Scope

**In scope.** The three packaged top-level agents become fully declarative: an
`agent_<name>.md` prompt plus a `top_agents/<name>.json` config, with no Python
branch anywhere naming them. The engine runs whichever one the session selects.
kodo-vsix renders its Agent picker from a catalog the server serves.

**Out of scope, deliberately.** The `~/.kodo/agents/` user-install tier
(proposal §4.1-4.7). This plan is sized so that tier drops in later **without
re-architecture** — which is why §4.3 does the attributable-validation work now,
while the agent set is closed and exhaustively testable.

**Definition of done.** Adding a fourth top-level agent requires: one `.md`, one
`.json`, zero Python edits, zero TypeScript edits.

---

## 1. Vocabulary — read this first

The old word for this concept was **"entry agent"**. It is retired. The rule,
decided 2026-09-16:

> **Use `agent`. Where `agent` is already taken by something else, use
> `top_agent`.**

"Taken" is a factual test, not a matter of taste — each row below was verified
against the tree. The distinction being protected is real and load-bearing:

| Concept | Means | Changes |
|---|---|---|
| the **running** agent | who holds the floor *right now* — often a sub-agent mid-pipeline | many times per prompt |
| the **top agent** | which top-level agent this session is configured to run | when the user picks |

### 1.1 Where `agent` is free — use it

| Surface | Name | Verified free because |
|---|---|---|
| WS message | `agent.set` | no `agent.*` message exists |
| Its payload key | `{name: "guide"}` | matches `skills.delete {name}` / `housekeeper_llm.set {id}` convention |
| `hello.ack` catalog | `agents: […]`, `default_agent` | `hello.ack` has no `agents`/`agent` key at top level |
| `session.list` row | `agent`, `agent_label` | row keys are `id`/`name`/`created_at`/`last_modified`/`workflow_mode`/`taken`/`workspace` |
| Validator CLI | `--agent` | no collision |
| `Harness.Modes.workflow` | `.agent` | `Modes` holds `autonomous`/`workflow`/`edit_control`/`command_control` |
| Prompt files | `agent_<name>.md` — **unchanged** | already the right word; `agent_` vs `subagent_` is the existing discriminator |

### 1.2 Where `agent` is taken — use `top_agent`

| Surface | Collides with | Name |
|---|---|---|
| `SessionState.workflow_mode` / `effective_workflow_mode` | `SessionState.agent` — the *running* agent (`_subagents.py:1773`, `None` when idle) | `.top_agent` / `.effective_top_agent` |
| `state` wire payload | same; `current_agent` already carries the running one | `top_agent` / `effective_top_agent` |
| `TransientStore.workflow_mode` | the message tag below lives in the same class | `.top_agent` |
| `session.jsonl` message tag `entry_agent` | **`"agent"` is taken by subsession markers** (`_subagents.py:1899,1907,1917,1945,1958`) | `top_agent` |
| `guided_state` `new_revision` field `workflow` | the record already carries `author`, itself an agent name | `top_agent` (§5.3) |
| `_run_entry_agent` | `_run_agent_turn` — one turn of *any* agent | `_run_top_agent` |
| `_entry_agent_name()` | `agent_name`, a parameter threaded through the whole engine | `_top_agent_name()` |
| `_entry_capability()` | — | `_top_agent_capability()` |
| `_last_entry_agent` | — | `_last_top_agent` |
| `_entry_turn_seq` | `_run_agent_turn` | `_top_agent_turn_seq` |
| `is_entry_turn` (watchdog kwarg) | — | `is_top_agent_turn` |
| config directory | `subagents/agents/` reads as nonsense next to `subagents/specs/` | `subagents/top_agents/` |
| kodo-vsix `workflowMode` | `agentName` (`webview/types.ts:205`, `session/types.ts:172`) | `topAgent` / `effectiveTopAgent` |

In **prose**, say "top-level agent" (or "top agent") wherever "agent" alone
would be ambiguous against a sub-agent.

---

## 2. Decisions baked in

| # | Decision | Consequence |
|---|---|---|
| 1 | **Single packaged root, but do the validation refactor now** | §4.3 — the prerequisite for a fail-soft second root later |
| 2 | **Rename `workflow.set` → `agent.set`** | §5.1. Outright rename, not a deprecation window. Legacy *values* survive via an alias table; the legacy *message* does not |
| 3 | **Server-declared default + user override** | §7. `default: true` in config, overridable by `default_agent` in `~/.kodo/etc/settings.json` |
| 4 | **Keep a `selectable` flag** | Judge stays registered and wire-reachable but absent from the served catalog |
| 5 | **Rename the `guided_state` `workflow` field too** | §5.3. Confirmed no existing users or projects to preserve |

---

## 3. Target shape

```
src/kodo/subagents/
  agent_guide.md              # unchanged prompt, minus `display_name:`
  agent_problem_solver.md
  agent_judge.md
  top_agents/
    guide.json                # NEW — the config half
    problem_solver.json
    judge.json
```

```json
{
  "name": "problem_solver",
  "notes": "Engineer-facing rationale. Never shown to a model or a user.",
  "label": "Problem Solver",
  "description": "One generalist agent tackles your request end to end.",
  "rank": 10,
  "selectable": true,
  "default": true,
  "aliases": ["problem_solving"]
}
```

- **`name`** must equal the file stem **and** the `agent_<name>.md` stem — the
  same rule `specs/*.json` already enforces, so config, prompt and filename
  cannot drift apart.
- **The wire value is the agent name.** `"guide"`, `"problem_solver"`,
  `"judge"` — no separate mode vocabulary. `aliases` carries the legacy
  persisted values (`"guided"`, `"problem_solving"`) for read-time coercion
  only; they are never emitted.
- **`description`** is the picker subtitle — it moves out of kodo-vsix's
  `_MODE_DESC` and becomes the single source.
- **`label`** replaces frontmatter `display_name:` on top-level agents, which
  becomes a load error there. One label, one place.
- The config is **required**. Unlike the user tier later, a packaged agent with
  no config is a build error, not a defaulted row.

---

## 4. Phases 1-2 — `kodo` only, no protocol change

### 4.1 Phase 1 — make the engine agent-agnostic

**Net effect: a deletion.**

Registry gains the table:

```python
@dataclass(frozen=True)
class TopAgent:
    name: str
    label: str
    description: str
    rank: int
    selectable: bool
    aliases: tuple[str, ...]

# AgentRegistry
def top_agents(self) -> tuple[TopAgent, ...]: ...      # rank, then name
def resolve_top_agent(self, value: str) -> str: ...    # name or alias -> name; default on miss
def default_top_agent(self) -> str: ...
```

In phase 1 the table is derived from the three `agent_*.md` files with hardcoded
labels; phase 2 moves it to JSON. Splitting it this way keeps phase 1 a pure
refactor with no new file format to review.

**Discriminator:** the `agent_` filename prefix, which
[_loader.py:195](../src/kodo/subagents/_loader.py#L195) already distinguishes.
Add `SubAgent.is_top_level` from the matched stem. Do **not** add a frontmatter
flag — a second source of truth that can disagree with the filename.

| File | Change |
|---|---|
| `_llm.py:458` | `_top_agent_name()` → `self._registry.resolve_top_agent(self._session.top_agent)` (7 lines → 1) |
| `_worker.py:104-119` | one generic branch (16 lines → 5) |
| `_turns.py:119-164` | **delete** the three `_run_*_with_input` wrappers; `_run_entry_agent` → `_run_top_agent` |
| `_proto.py:167-183` | **delete** the same three from `EngineHost` |
| `_shared.py:23-31` | **delete** `_PROBLEM_SOLVER_AGENT_NAME`, `_JUDGE_AGENT_NAME`; `_GUIDE_AGENT_NAME` survives only as the legacy fallback at `_resume.py:157` for untagged sessions — rename it to say so |
| `_subagents.py:1894,1935,2005` | `self._session.agent or _GUIDE_AGENT_NAME` → fall back to the session's top agent, not a literal |
| `_core.py:650` | validate against `registry.top_agents()` |

**Kill the double validation.** [_transient.py:1241](../src/kodo/state/_transient.py#L1241)
stops validating; the engine coerces through the registry when it restores at
[_core.py:396](../src/kodo/runtime/_engine/_core.py#L396). This is **forced, not
stylistic**: `src/kodo/state/` imports nothing from `kodo` (verified), so it
structurally cannot consult the registry. It also retires the hazard
ADDING_A_SUBAGENT.md §4.3 calls *"the pair most easily missed"*.

**Delete `ToolContext.mode` entirely.** Verified exhaustively: `ctx.mode` has
**exactly two consumers**, and both are the `!= "guided"` gates that are wrong
anyway (proposal §2.3a).

- Delete the gate in `tools/_get_findings.py:30` — granted to the guide plus 17
  pipeline sub-agents; **the grant is already the gate**.
- Delete the gate in `tools/_guided_dev_status.py:28` — granted to exactly one
  agent, so the check is pure redundancy.
- Delete `mode` from `ToolContext` (`_context.py:714`), `ToolDispatcher`
  (`_dispatch.py:290`), and the `mode=` argument at `_turns.py:1208`.

`kodo.tools` then has **no** notion of workflow mode at all — the correct end
state for a tier that is meant to be agent-agnostic.

**Make the missing-agent path visible.** `_handle_input_no_agent`
([_worker.py:262](../src/kodo/runtime/_engine/_worker.py#L262)) logs a warning,
flips the phase to `intake`, and **shows the user nothing** — a silent dead end.
Harmless while it cannot happen; it can the moment the selection is data. Emit a
recoverable error naming the missing agent and fall back to the default.

### 4.1a Phase 1 — what actually landed

Implemented 2026-09-16. `3939 passed`, lint and mypy clean; net **+533 / −363**
across 29 source files, and the engine lost its three `_run_*_with_input`
wrappers, both `ctx.mode` gates, `ToolContext.mode`, `ToolDispatcher(mode=)` and
two of the three agent-name constants.

Everything in §4.1 landed as written. Two things did **not**, both discovered by
tests, both worth knowing before phase 2 or 3:

**1. `stored_top_agent_value()` — a wire-stability shim that phase 3 deletes.**
Resolving the selection to a canonical agent name and storing *that* changes the
value the client sees: `SessionState.to_dict()` still emits it under
`workflow_mode`, and kodo-vsix reads anything that is not `"problem_solving"` as
`"guided"`. A stored `"problem_solver"` would therefore have shown **Guide**
selected while the server ran Problem Solver — and `session-resume.ts`'s label
compares against `"guided"` exactly. So the registry has a second accessor that
answers with the agent's *legacy alias* while it has one, and `handle_workflow_set`
/ session restore use it. `SessionState.top_agent` defaults to `"guided"` for the
same reason. Resolution to a real agent happens at *read* (`_top_agent_name()`),
which is where it belongs.

The rule this enforces: **phase 1 changes how the selection is resolved, never
what travels.** Phase 3 deletes `stored_top_agent_value`, the `aliases` it reads,
and the legacy defaults together with the protocol rename.

**2. The reverse metadata check is deferred to phase 2.** §4.2's cross-check has
two directions — every top-level agent has metadata, and every metadata entry has
an agent. Only the first is implemented. While the metadata is a module constant
and the agents come from an *injected* directory, the second direction is wrong:
building a registry over a temp dir of synthetic agents is a normal thing for a
test to do (30 in `test_agents.py` do it), and such a registry legitimately has
none of the packaged three. Phase 2 moves the metadata to
`top_agents/<name>.json` beside the prompts, at which point both halves come from
the same directory and the symmetric check becomes meaningful — **add it there.**
A registry that loads no top-level agents also has no default, so
`default_top_agent()` returns `""` in that case.

Smaller notes:

- `_GUIDE_AGENT_NAME` became `_FALLBACK_AGENT_NAME`, used *only* as the
  `agent_name=` default on the two generic turn/tool-result helpers (a default
  argument is evaluated at import, so it cannot ask the registry; every real call
  site passes the name). Every runtime path asks `default_top_agent()` instead.
- The phase-1 table had no `label`, so labels came from frontmatter
  `display_name` — which for `guide` is **"Kōdo"**, where the picker has always
  said "Guide". **Settled in phase 2** (§4.3a deviation 1): `label` and
  `display_name` are separate fields, and `top_agents/guide.json` sets
  `"label": "Guide"`.
- Test fakes that stub `AgentRegistry` now **delegate** the three top-agent
  methods to a real registry rather than returning fixed values. A fake that
  invents resolution answers let a test pass against behaviour the engine does not
  have — which is exactly what happened first time round.

### 4.2 Phase 2 — config as data

`src/kodo/subagents/top_agents/_loader.py`, mirroring
[specs/_loader.py](../src/kodo/subagents/specs/_loader.py) exactly: glob at
import time, `name` must equal the stem, **unknown keys are errors** at every
level. No new conventions to learn.

Five new load-time errors, all fail-fast:

1. An `agent_*.md` with **no** `top_agents/<name>.json`, and vice versa.
2. An `agent_*.md` that **also** has a `specs/<name>.json` — a top-level agent
   has no typed I/O contract by definition; a spec on one is a category error.
3. An `agent_*.md` declaring frontmatter `display_name:` while its config
   declares `label`.
4. More than one config declaring `default: true`.
5. A `name` or `alias` that is not unique across the whole top-agent set.

### 4.3 The attributable-validation refactor

Today the second validation pass
([_registry.py:576-620](../src/kodo/subagents/_registry.py#L576)) and
`__validate_artifact_roles` **raise on the first problem**, with no notion of
*which agent is at fault* or whether just that one could be demoted.

Restructure both to collect per-agent errors into `dict[agent_name, list[str]]`,
then decide what to do with the collection. **Behavior is unchanged here** — a
non-empty collection still raises `AgentLoadError`, now listing *every* problem
rather than the first — so this is a refactor with a strictly better error
message, testable against the closed packaged set.

**This is the single most valuable item in the plan.** It is the one piece the
`~/.kodo/agents/` tier cannot be built without, and it is far cheaper and safer
here, where the input set is curated and every test is deterministic, than later
against untrusted files.

---

### 4.3a Phase 2 — what actually landed

Implemented 2026-09-16. `3969 passed` (30 new), lint and mypy clean.

The `_TOP_AGENT_TABLE` constant phase 1 parked in `_registry.py` is gone:
`subagents/top_agents/{guide,problem_solver,judge}.json` are the source, loaded
by `subagents/top_agents/_loader.py`. All five §4.2 cross-checks are in, and
§4.3's collector is in — `AgentRegistry` now reports **every** problem in one
raise, grouped by the agent at fault:

```
2 agents failed validation:

broken (…/subagent_broken.md)
  - tool 'no_such_tool' has no ToolSpec in kodo.toolspecs
  - grants file-modifying tool(s) ['edit_file'], so its prompt must include {SHARED:editing}
  - critic 'ghost' has no subagent_ghost.md in the registry

orphan (…/agent_orphan.md)
  - no top_agents/orphan.json — a top-level agent declares how it is selected …
```

Three deviations, all discovered while building it:

**1. `label` and `display_name` coexist; §4.2's rule 3 was wrong.** That rule
said an `agent_*.md` declaring `display_name` while its config declares `label`
is an error. It is not — the two name *different things*, and `guide` is the
standing proof: the feed calls the agent "Kōdo" (`display_name`), the picker
calls the choice "Guide" (`label`). Rejecting the pair would have renamed a
control the user has always known by the other name. The config's `label` wins
where it speaks and falls back to `display_name` where it does not, and a test
pins the two apart. **This also closes §4.1a's open note** — `guide.json` sets
`"label": "Guide"`, so phase 3 can serve labels without changing the UI.

**2. Configs are loaded from the registry's directory, not at import time.**
§4.2 said "mirroring `specs/_loader.py` exactly", and the *file handling* does.
The *loading* deliberately does not: `specs/` builds `ALL_SUBAGENTS` at import
by globbing its own package, which would hand a registry over a temp directory
the three packaged configs and no agents to match them — reviving exactly the
problem §4.1a deviation 2 described. Reading both halves from the same
`agents_dir` is what makes the reverse cross-check a true statement, so **the
symmetric check §4.1a deferred is now in**, and correct.

**3. The raise→collect conversion had three latent short-circuit bugs**, each
caught by a test rather than review. A `raise` also *stops*; an `append` does
not. `__validate_planner` dereferenced `spec.output_schema` after recording
"has no SubAgentSpec", and indexed `props[...]` after recording that those very
fields were missing; the cross-agent pass called `paired.is_critic` after
recording `paired is None`. All three are now explicit early returns. **Worth
knowing for phase 4**, which converts more of these: every converted branch
needs checking for what the `raise` was implicitly guarding.

Smaller notes:

- `_validate_phase_tokens` raises from module scope, so `__validate_phases`
  catches it into the accumulator — otherwise one malformed token would still
  bypass the whole report.
- `TopAgent` gained `default` and `notes`, so the record is a faithful mirror of
  its file, exactly as `SubAgentSpec` mirrors a `specs/*.json`.
- `notes` is required by *test*, not by the loader — the same split `specs/`
  uses, since it is a convention for humans and nothing reads it.

## 5. Phase 3 — protocol

Both repos. Lands with phase 4.

### 5.1 The rename

| Old | New |
|---|---|
| `MSG_WORKFLOW_SET = "workflow.set"` payload `{mode}` | `MSG_AGENT_SET = "agent.set"` payload `{name}` |
| `state` `workflow_mode` / `effective_workflow_mode` | `top_agent` / `effective_top_agent` |
| `SessionState.workflow_mode` / `.effective_workflow_mode` | `.top_agent` / `.effective_top_agent` |
| `TransientStore.workflow_mode` | `.top_agent` (JSON key `top_agent`, reading `workflow_mode` as a fallback) |
| `session.jsonl` message tag `entry_agent` | `top_agent` |
| `session.list` row `workflow_mode` | `agent` **+ `agent_label`** |
| validator `--workflow` / `Modes.workflow: Literal[…]` | `--agent` / `agent: str` (no `Literal` — the set is open) |

**Values** are coerced through `registry.resolve_top_agent()` on every read path,
so `"guided"` / `"problem_solving"` in an existing `transient.json` resolve to
`guide` / `problem_solver`. Nothing on disk needs rewriting.

### 5.2 The served catalog

`hello.ack` gains:

```json
"agents": [
  {"name": "problem_solver", "label": "Problem Solver",
   "description": "One generalist agent tackles your request end to end.", "rank": 10},
  {"name": "guide", "label": "Guide",
   "description": "One coordinating agent drives specialists through design, tests and implementation.", "rank": 20}
],
"default_agent": "problem_solver"
```

`selectable: false` entries (Judge) are **absent** from the list but still
accepted by `agent.set` — today's behavior, now declared rather than implied.

Precedent to follow verbatim:
[_messages.py:327-338](../src/kodo/transport/_messages.py#L327) documents
`housekeeper_llm.get.ack`'s `options` array as existing *"so the panel renders
one radio button per catalog entry with no id hardcoded client-side"*.

### 5.3 The `guided_state` field rename

Confirmed safe: the `workflow` field written into per-file `.jsonl` evolution
logs is **write-only** — `derive_status` reads `type` and `decision`,
`last_revision_timestamp` reads `type` and `timestamp`, and nothing anywhere
reads `workflow`. With no existing users or projects to preserve, rename it
outright rather than leaving dead vocabulary in the record format.

`workflow` → `top_agent`, not `agent`: the record already carries **`author`**,
which is itself an agent name (the agent that made the edit, often a sub-agent).
`author` answers *who wrote this*; `top_agent` answers *under which top-level
agent's run*. Exactly the `current_agent` / `top_agent` distinction from §1.

Full call chain — all five sites:

| File | Change |
|---|---|
| `guided_state/_records.py:39,49` | `new_revision_entry(…, workflow: str)` → `top_agent: str`; emitted key `"workflow"` → `"top_agent"` |
| `guided_state/_store.py:49,58` | `append_new_revision(…, workflow=…)` → `top_agent=…` |
| `_checkpointing.py:303` | `workflow=self._host._session.effective_workflow_mode` → `top_agent=self._host._session.effective_top_agent` |
| `_checkpointing.py:1,56,92,280,286` | prose: "both workflow modes" → "every top-level agent"; the `workflow: "problem_solving"` example → `top_agent: "problem_solver"` |
| `tools/_context.py:645` | docstring referring to tagging revisions "with which workflow produced them" — rewrite (the `mode` field it documents is being deleted in §4.1 anyway) |

Tests carrying `workflow=` literals: `test_guided_state.py` (13 sites incl. the
assertions at :105 and :132, and the test *name* at :87
`test_new_revision_entry_carries_commit_and_workflow`), plus `test_document_status.py`,
`test_engine_document_flow.py`, `test_engine_core.py`, `test_tools_guide.py`,
`test_validator_harness.py`.

Doc: `STATE_AND_LIFECYCLE.md:97` is the schema of record for this entry type and
must be updated in the same commit; :49, :65, :165, :344, :368 carry "both
workflow modes" prose.

---

## 6. Phase 4 — the kodo-vsix agent picker

Eight files. The two menu primitives (`MenuGroup`, `MenuOption`) are **already
generic** — they take `label` / `desc` / `selected` / `onSelect` — so the Agent
group becomes a `.map` with no component work.

| File | Change |
|---|---|
| `webview/types.ts` (:670-671, :837-838) | `'guided' \| 'problem_solving'` → `string`, field `topAgent`/`effectiveTopAgent`; add `agents: AgentRow[]` + `defaultAgent: string` |
| `webview/reducer.ts` (:1355-1356) | hydrate the catalog from `hello.ack`; drop the hardcoded default |
| `webview/App.tsx` (:528-529) | pass the catalog through; drop both coercions |
| `webview/ModeControls.tsx` | **delete** `_MODE_DESC.guided`/`.problem_solving` and `isPS` (:484); the two literal `<MenuOption>` rows (:519-527) become `agents.map(…)`; the frozen-note label (:518) reads from the catalog |
| `session/types.ts` (:45-46) | `coerceWorkflowMode` → `coerceTopAgent(value, catalog, fallback)`, **validating against the served list** instead of collapsing everything unknown to `'guided'` (the proposal §2.3b bug fix) |
| `session/mode-toggle-controller.ts` (:27-28, :191-199, :245) | field → `topAgent`; `applyNewSessionDefaults()` sends the **served** default, hardcoding nothing; `setWorkflow` → `setTopAgent(name: string)` |
| `session/controller.ts` (:321) | webview message `workflow_set` → `agent_set` |
| `extension/session-resume.ts` (:107) | `kindLabel` reads the row's `agent_label` instead of a ternary |

Group heading stays **"Agent"** — already the right word and already what the UI
says.

---

## 7. The default, and the user override

Precedence, highest first:

1. `default_agent` in `~/.kodo/etc/settings.json` — the user's pick.
2. `"default": true` in a config — the shipped default.
3. Lowest `rank`, then name — so a missing or misconfigured default still resolves.

**Server.** New settings key (`SETTINGS.md` §2.8), defaulted in the same table as
`housekeeper_llm` (`server/_config.py:230`). An unknown or non-selectable value
falls through to (2) rather than erroring — a settings file naming a deleted
agent must not break startup.

**WS surface.** `default_agent.get` / `.set`, modelled on `housekeeper_llm.get` /
`.set` — `.set` replies `{ok: false, error}` for an unknown name and persists
nothing.

**Client.** A row in the Kōdo Settings panel's General section listing the
selectable agents, mirroring the Housekeeper LLM subsection. `hello.ack`'s
`default_agent` already carries the resolved winner, so the session path needs no
extra round trip.

---

## 8. Migration and compatibility

| Surface | Handling |
|---|---|
| `transient.json` `workflow_mode` | Read as a fallback key; values coerced via `aliases`. Never rewritten in place — the next `update()` writes `top_agent`. |
| `session.jsonl` `entry_agent` tag | Read as a fallback key alongside `top_agent`; `_last_top_agent` accepts either. |
| Sessions persisted as `"judge"` | Resolve normally. They currently display as **"Guided"** because of the `coerceWorkflowMode` bug — fixed by §6. |
| `.jsonl` evolution logs | **Renamed** (§5.3) — no existing users or projects to preserve. |
| kodo ↔ kodo-vsix | Ship in lockstep — the VSIX installs `py-kodo==${extVersion}` (`uv-setup.ts:710,813`) — so the outright rename carries no release-skew risk. It does carry a dev-tree risk: §11.1. |

---

## 9. Tests

| Phase | Tests |
|---|---|
| 1 | `test_engine_worker.py` / `test_engine_turns.py` / `test_engine_llm.py` reference the deleted wrappers — retarget to `_run_top_agent`. New: every registered top agent dispatches; an unknown selection falls back to the default **and emits a user-visible error**; `test_main.py:39`'s `_PINNED_AGENT` still resolves. |
| 2 | Parametrize over `top_agents()` rather than naming Guide/PS/Judge — the CLAUDE.md rule about deriving from the live registry. New: each of the five §4.2 cross-checks raises; the §4.3 collector reports **all** problems, not the first, and names the agent at fault. |
| 3 | `agent.set` accepts a name and each alias, rejects unknown; `hello.ack` omits `selectable: false` entries but `agent.set` still accepts them; a `transient.json` holding `"problem_solving"` resumes onto `problem_solver`; a `session.jsonl` holding `entry_agent` still resumes. |
| 5.3 | `new_revision` entries carry `top_agent`; `derive_status` and `last_revision_timestamp` are unaffected (they never read the field). |
| 4 | kodo-vsix: picker renders N rows from a served catalog; `coerceTopAgent` keeps an unknown value out without collapsing to Guide; a new session adopts the served default. |
| 7 | `default_agent` precedence, all three levels; an unknown or non-selectable value falls through instead of raising. |

---

## 10. Documentation to update

| Doc | Change |
|---|---|
| `ADDING_A_SUBAGENT.md` §4 | Rewrite: §4.3's six engine edits and §4.4's VSIX grep list become "add two files". §4.1's "it is not data-driven" is the claim this plan deletes. Retire "entry agent" for §1's vocabulary. **On landing, not before.** |
| `WS_PROTOCOL.md` | §7.6 `workflow.set` → `agent.set`; §5.1 `hello.ack` gains `agents` + `default_agent`; §5.2 `state` field rename; `session.list` row gains `agent_label`; new §7.6 pair for `default_agent.get`/`.set`. |
| `STATE_AND_LIFECYCLE.md` | §1.1 (line 97) is the schema of record for `new_revision` — rename the field there **in the same commit as §5.3**; plus the "both workflow modes" prose at :49, :65, :165, :344, :368. |
| `SETTINGS.md` | New §2.8 `default_agent`; add it to the §3 default file. |
| `SESSIONS.md` | Lines 22-25 describe sessions in terms of "both workflow modes" — reword to "whichever top-level agent". |
| `TOOLS.md` | Remove any mention of mode-gated tools (§4.1 deletes the concept). |
| `VALIDATOR.md` | `--workflow` → `--agent`. |
| `GUIDED_DEV_MODE.md` | Check for "Guided mode" used to mean an engine behavior rather than an agent — after §4.1 the engine has no such concept. |
| `TOP_AGENT_PROPOSAL.md` | Keep as the rationale record; its "entry agent" vocabulary is superseded by §1 here. |

**Already fixed, independently of this plan:** `WS_PROTOCOL.md` §4.1 claimed the
client re-syncs `mode.set` / `workflow.set` from `.kodo/settings.json` after
`hello.ack`. Verified false — no such file is read; `applyNewSessionDefaults()`
hardcodes the value. Corrected in this pass.

---

## 11. Risks

1. **The outright rename breaks mismatched dev trees.** Released builds ship in
   lockstep so users are safe, but anyone running `KODO_DEV_PATH` against a stale
   `kodo-vsix` checkout gets a silently ignored `workflow.set` and a session stuck
   on the default. *Mitigation:* land phases 3 and 4 in one commit across both
   repos, and make the server **log** an unrecognized message name rather than
   dropping it silently.
2. **§4.3 is a validation refactor with no behavior change** — easy to get subtly
   wrong and hard to notice, because the packaged set is valid and stays valid.
   *Mitigation:* land it with a fixture corpus of deliberately invalid agent sets
   asserting *which* agent is blamed, not just that it raised.
3. **`agent` vs `top_agent` will drift** unless §1 is treated as the contract.
   The two words are close enough that a future edit will reach for the wrong one.
   *Mitigation:* put §1's rule in `ADDING_A_SUBAGENT.md` §1 when it is rewritten,
   where an agent author will actually meet it.
4. **Deleting `ToolContext.mode` is hard to walk back.** If a future tool must
   vary by top agent, the dispatcher already has `agent_name` — the natural answer
   is a per-tool declaration, not a resurrected global mode.
5. **The picker gets longer than two rows.** `MenuGroup` has no scroll of its own
   (the popup has `maxHeight`); worth a look once the list is dynamic.

---

## 12. Sequencing

```
Phase 1  kodo        engine agent-agnostic          DONE 2026-09-16 (§4.1a)
Phase 2  kodo        config as data + §4.3 refactor DONE 2026-09-16 (§4.3a)
   ── land and verify against the existing VSIX ──
Phase 3  kodo        protocol rename + catalog      ┐ one commit,
         kodo        guided_state field rename §5.3 │ both repos
Phase 4  kodo-vsix   picker from catalog            ┘
Phase 5  both        default_agent + Settings row
```

Phases 1 and 2 are independently shippable and independently valuable — they
delete code and fix two bugs without changing a single wire message. Phases 3
and 4 **must land together** (§11.1). Phase 5 is additive and can trail.

Phases 1 and 2 are in, and nothing on the wire has changed yet. Phase 3 is the
first one a client can see, and it must land with phase 4 in the same commit
(§11.1).
