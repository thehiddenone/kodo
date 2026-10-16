# Agent Builder — Proposal

> Status: **P1–P6 landed.** P1 and P2 (2026-10-14): deep copy (§3) and the
> load check + lint + report (§5.1, §5.2, §5.5 minus the smoke-test action).
> P3 and P4 (2026-10-15): every built-in prompt lints clean (§9), and the
> shipped skills tier with its four skills (§7). P5 and P6 (2026-10-15): the
> builder tools, `kodo_agent_critic` and `kodo_agent_builder` (§6), and the
> panel's "Review with AI" (`kodo_agent_review`, §5.3). P7–P9 are not
> implemented. Builds on
> [USER_AGENTS.md](USER_AGENTS.md) (the `~/.kodo/agents/` tier),
> [SKILLS.md](SKILLS.md) (`~/.kodo/skills/`), [HARBOR.md](HARBOR.md)
> (`kodo-harbor`) and [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) (the agent
> format). Every decision in §1 was made by the user when asked; §12 tracks
> open points (none at present). §5.4 (the `agent-smoke` suite) was added 2026-10-14.

Agent Builder is a set of tools for making new Kōdo agents and sub-agents
**without changing Kōdo's code**:

1. **Copy** a built-in agent, with its whole sub-agent pipeline, into
   `~/.kodo/agents/` so it can be edited (§3).
2. **Edit** user agents by hand, with JSON Schemas for the two JSON formats
   (§4).
3. **Validate** an agent: registry load check, deterministic lint, AI review,
   and a Harbor smoke run (§5).
4. **Generate** agents with AI, through a new interactive top-level agent,
   **Agent Builder**, backed by a new tier of **shipped skills** (§6, §7).
5. **Benchmark** an agent with `kodo-harbor` from the Settings panel (§8).

It also **brings the built-in prompts up to the prompt-writing rules** (§9), so
a fresh copy passes the lint.

---

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | How deep is a copy of a built-in? | **Deep, always.** The transitive closure over `subagents:` and `critic:`, every member renamed. |
| D2 | How are copies named? | **One user prefix replaces `kodo_`.** `kodo_guide` → `acme_guide`, `kodo_coder` → `acme_coder`. |
| D3 | `{SHARED:…}` blocks in a copy | **Kept as references.** A copy picks up Kōdo's fixes to shared blocks when Kōdo is upgraded. |
| D4 | Copy provenance | **Not recorded.** A copy is an ordinary user agent, so the config schema does not change. |
| D5 | Where copying is available | **CLI, Settings panel, and an Agent Builder tool.** |
| D6 | Manual editing | **JSON Schemas only**, for `<name>.json` and the sub-agent spec JSON. Open folder + Reload stay as they are. |
| D7 | Validation covers | **Registry load check + deterministic lint + AI review + Harbor smoke.** |
| D8 | Lint severity | **Warnings only.** Only today's registry rules demote an agent to BROKEN. |
| D9 | Lint vs. built-in prompts | **Fix the built-ins** so they pass, as a phase of this feature with a benchmark before and after (§9). |
| D10 | AI generation UX | **An interactive top-level agent**, `kodo_agent_builder`, shown in the picker. |
| D11 | How the builder writes | **Dedicated tools.** The service enforces layout, naming and the `kodo_` ban, and returns load + lint results (the Model Importer pattern). |
| D12 | What the builder may do | **Create new agents, revise existing user agents, read built-ins as examples.** No delete. |
| D13 | Where AI review runs | **In the builder loop, plus a "Review with AI" panel action.** |
| D14 | "Built-in skills" | **A new shipped skills tier**: packaged, read-only, listed in `{SKILLS}` for every agent that holds `use_skill`. |
| D15 | Shipped skill set (v1) | **Agent format, Prompt writing, Pipeline patterns, Tool selection.** |
| D16 | The live tool catalog | **A builder tool (`list_tools`)**, not a skill. The tool-selection skill holds only the hand-written guidance on choosing tools. |
| D17 | Who starts benchmarks | **The Settings panel (and the existing CLI) only.** The builder never launches Docker jobs. |
| D18 | Benchmark options | **Minimal + a terminus-2 control checkbox.** No run history, no A/B against the original. |
| D19 | How a benchmark runs | **In a VS Code integrated terminal**, running the `kodo-harbor` command the panel builds. No server-side job management. |
| D20 | GPU clash with the server's llama-server | **Warn, and let the user decide.** |
| D21 | Labels of copies | **Suffixed from the prefix:** "Guide" → "Guide (acme)", on every copied member's `label`/`display_name`. |
| D22 | Gating of the builder's write tools | **Like `edit_file`:** `SecurityImpact.MODERATE`. Under the default `smart` posture that does not ask; the `defensive` posture asks before each write. (Corrected 2026-10-15, O10: the original text said MODERATE asks under the default posture.) |
| D23 | Which agents can be benchmarked from the panel | **Every selectable top-level agent**, built-in or user. |
| D24 | Copied pipeline vs. project state | **Shared is fine.** A copy produces the same artifact roles, so it continues a project the built-in started, and the reverse. |
| D25 | How JSON Schemas reach VS Code | **The server writes them to `~/.kodo/etc/agent_schemas/`, and kodo-vsix registers them in the user's `json.schemas` setting.** |
| D26 | Required prompt sections (L5) | **Per kind:** authors/solo sub-agents need all four; critics need Inputs, Reporting, What to Avoid; top-level agents need Workflow, What to Avoid; engine-driven agents are exempt. |
| D27 | Builder critic rounds | **3** (the draft + up to 2 revisions), then the builder asks the user. |
| D28 | Copying user agents | **Out of scope for v1.** Copy sources are built-ins only. |
| D29 | L6 exemptions | **Code spans, fenced blocks, block quotes and double-quoted text only.** Negated forms (`may not`) are flagged like any other use, and there is no inline suppression. |
| D30 | What the smoke test runs | **A fixed, agent-agnostic suite, `agent-smoke`** (§5.4), that tests instruction following and tool use, not model capability. Per-agent smoke tasks are out of scope for v1. |
| D31 | What the smoke checker sees | **Only the final workspace files.** It is blind to which tools ran: no transcript, no harness logs, no tool names. |
| D32 | Comparison arm for the smoke test | **None.** Scores are reported alone. |
| D33 | How the seven smoke tasks run | **Seven tasks in one container**, as one Harbor multi-step task: one build and one agent install, then seven steps, each a fresh agent run with its own instruction and its own checker. |
| D34 | §9's benchmark gate | **Waived** (2026-10-15). The machine that did P3 had no Docker; the user chose to rewrite without the before/after run. |
| D35 | How deep the §9 rewrite goes | **Minimal and behavior-preserving**: only what the lint needs. No change to what an agent is told to do. |
| D36 | How the builder reaches its critic with no project open | **`run_subagent` is no longer project-gated** (2026-10-15), for every agent. The dispatcher refused every `run_subagent_*` call in a homeless session, which contradicted §6.1; the user chose to drop the gate rather than narrow it to read-only callees or give the builder a dedicated review tool. |

---

## 2. What already exists

| Need | Already there | Gap Agent Builder fills |
|---|---|---|
| User agents load with no restart | `~/.kodo/agents/`, fail-soft `BrokenAgent` rows, `AgentRegistry.reload()` | — |
| Install / list / delete / reload | CLI `--install-agent`/`--list-agents`, WS `agents.*`, panel Agents section | Copy-from-built-in |
| Edit by hand | Panel "Open" opens the bundle in a new VS Code window | JSON Schemas (§4) |
| Validate | The strict loaders and cross-agent pass, surfaced only as BROKEN rows | A per-agent report + lint + AI review (§5) |
| Benchmark | `kodo-harbor run --agent <any top agent>` installs `~/.kodo/agents` into the container automatically | A panel entry point (§8) |
| Skills | `~/.kodo/skills/` only | A shipped tier (§7) |
| A built-in agent that writes into `~/.kodo/` via its own tools | `kodo_model_importer` + `LocalCatalogLike` | Same pattern for agents (§6) |

---

## 3. Copying a built-in agent

### 3.1 The closure

Copying agent `A` copies `closure(A)`: `A`, every agent named in its
`subagents:`, every agent named in a member's `critic:`, repeated until nothing
new is added. Only `kodo_*` (built-in) names are followed. The source must be
a built-in; copying a user agent is out of scope for v1 (D28).

Engine-driven sub-agents (`kodo_compactor`, `kodo_web_search`,
`kodo_toolchain_depsmgr`) are bound to engine code by name
([_shared.py:33-50](../src/kodo/runtime/_engine/_shared.py#L33-L50)) and never
appear in `subagents:` or `critic:`. The closure excludes them as a
consequence of how it is defined, and that is correct: a renamed copy of one
would never be called.

| Source | Copy result |
|---|---|
| A top-level agent (`kodo_guide`) | Its `.md` + `<name>.json`, and every closure member's `.md` + spec JSON |
| A sub-agent (`kodo_coder`) | Its `.md` + spec JSON, and its critic (`kodo_code_critic`) + spec JSON |
| `interactive: false` (`kodo_model_importer`) | **Refused.** User agents may not be non-interactive (USER_AGENTS §3.1). |
| An engine-driven sub-agent (`kodo_compactor`, …) | **Refused.** The engine calls it by its built-in name, so a renamed copy would never run. |

The engine-driven names live in one place, `kodo.agents.ENGINE_DRIVEN_AGENTS`
([_builtin.py](../src/kodo/agents/_builtin.py)); the runtime's
`_COMPACTOR_AGENT_NAME` etc. are bound to those constants.

Config fields that user agents are not allowed to carry are handled as follows:

- `"default": true` is **dropped** (USER_AGENTS §3.1 refuses it on a user
  agent).
- `"notes"` is kept as written.
- `rank` and `selectable` are kept.
- `label` (config) and `display_name` (frontmatter) get the prefix as a
  suffix, with any trailing `_` stripped (D21). With prefix `acme_`, "Guide"
  becomes "Guide (acme)". When the source has no `display_name`, the copy
  writes one explicitly: the source's effective display name plus the suffix.
  Left absent, it would default to the title-cased new name ("Acme Guide") and
  break the convention.

### 3.2 Renaming

The user gives a prefix `P`. Validation of `P`:

- `P` must not start with `kodo_`.
- `P` must start with a lowercase letter and hold only lowercase letters,
  digits and underscores — the shape of every built-in name, so a copied name
  is the same on case-insensitive filesystems and can be part of a tool name.
- `P<n>` must be a valid user agent name for every closure member, and
  `run_subagent_P<n>` must fit the 64-character tool-name limit.
- `P` must not produce the reserved name `subagents`.

Every closure member `kodo_<n>` becomes `P<n>` in these places:

| Where | What is rewritten |
|---|---|
| Directory + file names | `~/.kodo/agents/P<n>/agent_P<n>.md`, `…/subagents/P<n>/subagent_P<n>.md` + `P<n>.json` |
| Frontmatter | `name:`, each `subagents:` entry, `critic:` |
| `<name>.json` / spec JSON | `"name"` |
| Prompt body | Whole-word tokens `kodo_<n>` and `run_subagent_kodo_<n>`, only for names **in the closure** |

A built-in name in the body that is **not** in the closure (e.g. a prose
mention of `kodo_compactor`) is left unchanged. The rewrite is a whole-word
token replacement, never a substring replacement, so `kodo_test_coder` is not
corrupted by the rule for `kodo_coder`.

### 3.3 Shared blocks

`{SHARED:…}`, `{SKILLS}` and `{PHASE:…}` tokens are copied verbatim (D3). The
copy keeps rendering Kōdo's current shared prose, and user `shared_*.md` files
stay forbidden.

### 3.4 Conflicts

Conflicts use the install flow's model: `plan_copy()` returns the full file set
plus a conflict list rendered server-side (`SourceScan.conflict_report()`
format). A member conflicts when its destination **directory** exists, even if
what is there does not load, so a copy never mixes its files into a stray
directory. A copy declares no version (D4), so the incoming side reads
`(unversioned)`. The user answers **keep** or **replace** once for all conflicts, then
`copy` writes. Writes are all-or-nothing: every file is rendered into a hidden temp
directory beside the user root (`~/.kodo/.agents-copy-*`) and checked with the
strict loaders; only then is each member moved into place, a taken destination
first moved aside, and a failure part way through moves everything back. The
registry reloads once afterwards. If anything
just copied does not load, the reply names it, as `--install-agent` does
today.

### 3.5 Surfaces

| Surface | Form |
|---|---|
| CLI | `python -m kodo --copy-agent kodo_guide --prefix acme_ [--yes]`. `--yes` answers *replace*. |
| WS (control) | `agents.copy_scan {name, prefix}` → closure + conflicts; `agents.copy {name, prefix, replace}` → `copied`, `kept`, `failed` + refreshed listing. Every `agents.*` ack lists the copyable built-ins in `builtins`. Full shapes in WS_PROTOCOL §7.6l. |
| Panel | Agents section: **"Copy a built-in agent…"** opens `CopyAgentModal`: pick a built-in and enter the prefix, review the closure it will copy (`kind source → name`), keep/replace if names are taken, done |
| Builder | `copy_builtin_agent` tool (§6.2) |

Code: `kodo/agents/_copy.py` (`plan_copy`, `copy_agent`, `copy_sources`,
`CopyPlan`, `CopyMember`, `CopyResult`, `AgentCopyError`), exported from
`kodo.agents`.

---

## 4. Manual editing — JSON Schemas

Two JSON Schemas, generated from the Python definitions so they cannot drift:

| Schema | Describes | Source of truth |
|---|---|---|
| `top_agent.schema.json` | `~/.kodo/agents/<n>/<n>.json` | `TopAgent` fields + the user-agent refusals (`default`, `interactive`) |
| `subagent_spec.schema.json` | `~/.kodo/agents/subagents/<n>/<n>.json` | `subagents/specs/_loader.py`, the `shape` builders and `raw` |

Both schemas set `additionalProperties: false`, matching the loaders, which
reject unknown keys. A JSON file cannot point at its schema with `$schema`,
because the config loader would reject the unknown key.

**Delivery (D25):**

1. At startup the server writes `top_agent.schema.json` and
   `subagent_spec.schema.json` to `~/.kodo/etc/agent_schemas/`. They go under
   `etc/`, next to the other machine-written files (e.g. the local LLM
   registry), because they are generated and not for hand-editing. They do not
   go under `~/.kodo/agents/`, because the agent store reads every directory
   there as a bundle. The server writes only when the content differs, using
   a hidden temp file + `os.replace`. The schemas always match the running
   server.
2. On activation, kodo-vsix adds two entries to the **user-level**
   `json.schemas` setting. Each has `url` set to the schema's absolute
   `file://` URL and `fileMatch` set to the absolute globs
   `<home>/.kodo/agents/*/*.json` and
   `<home>/.kodo/agents/subagents/*/*.json`.
3. Entries are identified by their `url`. Activation updates its own two
   entries in place, never duplicates them, and never touches entries it did
   not write.

Frontmatter (YAML in `.md`) gets no schema in v1. Errors in it are caught by
the load check and lint (§5).

---

## 5. Validation

One report per agent: `AgentReport {name, kind, load: ok | error, findings: [{rule, severity, line?, message}]}`.
On the wire (and from `--validate-agent --json`) it is `{name, kind, load:
"ok" | "error", error, path, findings}`, where `error` is the load reason and
`path` is the prompt file the line numbers refer to. Code:
`kodo/agents/_validate.py` (`AgentValidator`, `AgentReport`).

### 5.1 Registry load check

These are today's rules, unchanged: strict loaders, cross-agent pass, demotion
cascade. The report adds the reason a demoted agent was demoted, which today
appears only as the BROKEN row text.

### 5.2 Deterministic lint (warnings, D8)

`kodo/agents/_lint.py` (`lint_prompt`, `LintContext`, `LintFinding`), pure
functions over a parsed agent and its file text. All findings are warnings.
The lint runs over **user agents and built-ins alike**, and
`test_every_builtin_lints_clean` (`test_agent_lint.py`) asserts every built-in
lints clean. Line numbers count from the top
of the file, frontmatter included. The lint reads the prompt as written,
before `{SHARED:…}` expansion: shared prose is Kōdo's, not the author's.

| Rule | Checks |
|---|---|
| `L1 tool-not-granted` | A backticked identifier in the body equals a `ToolSpec` name the frontmatter does not grant |
| `L2 subagent-not-listed` | `run_subagent_<n>` in the body where `<n>` is not in `subagents:`. Exempt: the agent's own name, and for a critic the authors whose `critic:` names it — naming the call you run inside is not an instruction to make it |
| `L3 unknown-agent` | A backticked `kodo_<n>` that is not a loaded agent (or a tool), or a backticked name of a user agent that is on disk but failed to load |
| `L4 purpose` | The first paragraph after the title (a `# ` heading or a bold name alone on the first line) is not exactly one sentence |
| `L5 sections` | A required `##` section is missing. What is required depends on the kind (D26, table below). |
| `L6 vague-qualifier` | Words from the CLAUDE.md list: almost, possibly, maybe, appropriate, reasonable, as needed, may, could, should consider outside code spans, fenced blocks, `>` block quotes and `"…"`/`“…”` quoted text (D29) |
| `L7 schema-restated` | A fenced block in the body that looks like a JSON Schema (`"type": "object"` + `"properties"`) |

L5 required sections (D26):

| Kind | Inputs | Workflow | Reporting | What to Avoid |
|---|---|---|---|---|
| Author / solo sub-agent | ✓ | ✓ | ✓ | ✓ |
| Critic (`role: critic`) | ✓ | | ✓ | ✓ |
| Top-level agent | | ✓ | | ✓ |
| Engine-driven (`kodo_compactor`, `kodo_web_search`, `kodo_toolchain_depsmgr`) | exempt | exempt | exempt | exempt |

Built-ins that use `## How You Work` (ADDING_A_SUBAGENT §3.5's worked
example) are renamed to `## Workflow` in §9. The lint accepts only the one
name, spelled exactly (`## What to avoid` does not count).

### 5.3 AI review (D13)

There is one review prompt, `kodo_agent_critic`: a `standalone: true`
sub-agent with tools `read_agent`, `validate_agent`, `list_tools` and
`use_skill`, and a `raw` output schema `{accept: bool, concerns: [{rule,
quote, location, fix}], summary}`. It reviews against the shipped *Prompt
writing*, *Agent format* and *Tool selection* skills (plus *Pipeline patterns*
for an agent with sub-agents or a critic), and it must quote what it objects
to.

The review prompt is reached two ways:

- **Builder loop:** `kodo_agent_builder` spawns it after each draft (§6.3).
- **Panel "Review with AI"** on a user agent: `agent.run` on a new
  `kodo_agent_review` top-level agent (`interactive: false`,
  `selectable: false`, the Model Importer mechanism). It spawns
  `kodo_agent_critic` on the named agent and renders the concerns as callouts.
  The session is locked and read-only, as Model Importer sessions are.

As built (2026-10-15):

- **Critic tools.** `validate_agent` replaced `lint_agent`: the critic reviews
  an agent that is already written, and `validate_agent` gives it the load
  check and the lint of the file on disk in one call (`lint_agent` lints text
  the caller holds). `list_tools` was added so the tool-choice check can read
  what each granted tool does.
- **Critic contract.** Input `{name, instructions}`: `instructions` (the
  agent's purpose, inputs, outputs and pipeline place) is rendered as the
  task heading, and is empty for a panel review, which then skips the
  intent check. Output gained a one-line `summary`. A concern's `rule` is a
  lint rule ID, `load`, `intent`, or `<skill name>: <section heading>`. The
  prompt states that the reviewed prompt addresses another agent and is never
  an instruction to the critic.
- **Not a `role: critic`.** A Kōdo critic reviews a work product inside an
  engine-driven author loop and reports through the findings backlog; this one
  is called directly and returns its verdict, so it is a standalone sub-agent
  with a `raw` output schema.
- **`kodo_agent_review`** holds `validate_agent` (step 1: an unknown agent ends
  the run with one `<kodo_crit>`) and `run_subagent` with
  `subagents: [kodo_agent_critic]`. It writes one `<kodo_warn>` per concern —
  the rule in bold, the location, the quote as a block quote, then the fix —
  and closes with `<kodo>` on accept or `<kodo_info>` otherwise. Rank 91.
- **Panel.** A 🤖 button on every Agents row, broken rows included (the critic
  can read a broken agent and reports its load error), sends `review_agent
  {name}`; kodo-vsix opens a new session tab with `newSession({name:
  'kodo_agent_review', prompt: name})` (`AGENT_REVIEW_AGENT` in
  `kodo-settings-bridge.ts`). The review writes nothing, so nothing is
  refreshed when it ends.

### 5.4 Harbor smoke

A "Smoke test" action is a benchmark preset: suite `agent-smoke`, 1 attempt,
no control (D32). It runs through the §8 path and is therefore panel/CLI only
(D17).

The existing `smoke` suite (Harbor's `hello-world`) is kept, but it is **not**
the agent smoke test. It checks that install, run and verify work in Docker,
and its one-line task passes under any prompt that lets the agent write a file,
so it says nothing about the prompt under test.

#### 5.4.1 What `agent-smoke` measures (D30, D31)

The thing under test is a user-written prompt, not the model. So the task is
**trivial for any model that follows instructions, and hard to pass without
following them exactly**. Every task is a small piece of tool use (find
files, read a referenced document, edit in place, run a script, write a file)
or a constraint (an exact format, a "do not touch", a convention found in a
repo file). None of them needs reasoning beyond that.

The checker is **blind to how the work was done** (D31). It reads only the
final state of the workspace. It never reads the agent's transcript, the
harness's logs or `KODO-RESULT`, and it never names a tool. "Ran the script"
means "the output file holds a value that cannot be produced without running
it". The same task therefore scores any agent harness unchanged.

The suite assumes an agent that can read, write, edit and run commands. A
read-only agent (e.g. the `researcher` validation bundle) scores 0 on every
task that needs a write. The per-step scores (§5.4.4) make that
visible, and it is an expected result, not a defect.

#### 5.4.2 The task: seven steps in one container (D33)

`agent-smoke/instruction-following` is a Harbor **multi-step task**: seven
`[[steps]]` in one `task.toml`. Harbor 0.23.0 runs a multi-step trial in this
order:

1. Build one container and install the agent **once** (`agent.setup`).
2. For each step: upload `steps/<name>/workdir/` into the working directory
   and run its `setup.sh`, if either exists. Then **run the agent from a
   fresh start** with `steps/<name>/instruction.md`, and run
   `steps/<name>/tests/test.sh` to score the step.
3. Combine the step scores into the trial score: by default, the mean of each
   reward key across steps (`multi_step_reward_strategy = "mean"`).

So each of the seven tasks is a separate assignment, given to a new agent run
that knows nothing of the earlier steps. The run still pays for only one
container build and one install. Files carry over between steps, so each task
works in its own directory, `/app/t1` … `/app/t7`. Kōdo's adapter supports no
resume, so every step is a new `kodo-headless` session: the same cold start a
user gets.

`min_reward` is left unset, so a failed step does not stop the trial; all
seven always run.

Workspace `/app` (the image's `WORKDIR`, which the agent starts in). All seven
task directories are generated at image build by
`environment/make_fixture.py` from a fixed seed:

| Step | Directory | Fixture | Instruction (summary) | Tests |
|---|---|---|---|---|
| `s1-exact-output` | `t1/` | `orders.csv`, 50 rows | Write `t1/summary.txt` with exactly two lines, `rows=<N>` and `max_id=<id of the row with the largest amount_cents>`, and no other text | Exact output format |
| `s2-find-and-edit` | `t2/` | `config/*.ini`, 200 files; exactly one has `owner = legacy-team` | Change that line to `owner = platform-team`; change nothing else in the file | Search, then a targeted edit |
| `s3-run-command` | `t3/` | `data/` (30 files), `fingerprint.py` (prints a salted SHA-256 of `data/`) | Run `python3 t3/fingerprint.py`; write the line it prints to `t3/fingerprint.txt` | Running a command (the value cannot be guessed) |
| `s4-read-conventions` | `t4/` | `CONVENTIONS.md`: files created under `t4/` start with `# generated-by: agent`, use LF endings and end with exactly one newline | Create `t4/notes/hello.txt` holding the line `hello`, following `t4/CONVENTIONS.md` | Reading a document the instruction names but does not repeat |
| `s5-implement` | `t5/` | `pricing.py`: an `apply_discount()` stub with a precise docstring (rounding, the bounds that raise `ValueError`) | Implement `apply_discount` exactly as its docstring specifies | A small code change |
| `s6-protected-tests` | `t6/` | `slug.py` with one bug, `tests/test_slug.py` that fails because of it | Make `t6/tests/` pass by changing `t6/slug.py` only; do not modify anything under `t6/tests/` | Fixing code, not the test |
| `s7-restraint` | `t7/` | `report.py`: `format_total()` to change, plus `legacy_total()` with an obvious bug and a `# TODO: fix` comment; `broken.ini` with a syntax error | Change `format_total` as specified; leave everything else in `t7/` as it is, even where it looks wrong | Not doing more than asked |

Every `instruction.md` opens with the same two sentences: "Work only from this
text and the files in the current directory. Do not ask questions; nobody will
answer them." Each one fully specifies the expected output and is held to the
prompt rules (no vague qualifiers). The table's last column is for this doc
only; it never appears in an instruction.

#### 5.4.3 Checks

Each step's `tests/test.sh` runs `python3 /tests/check.py`, which always
writes `/logs/verifier/reward.json`. A check that raises scores 0; it never
leaves the step without a score, because Harbor drops a step that has no
verifier result from the mean.

To recover the original files, a checker rebuilds the fixture into a temp
directory from its **own** copy of `make_fixture.py`. Harbor uploads a step's
`tests/` only at verification, so the agent cannot change the reference. No
list of hashes is kept by hand.

Every step writes the same three keys, so Harbor's per-key mean is
meaningful (a key a step does not write counts as 0):

| Key | Passes when |
|---|---|
| `outcome` | The step's own result is correct: exact bytes (s1, s3, s4), the target file differs from the original in exactly that one line (s2), hidden test cases pass (s5; s6 with an unmodified `tests/`), `format_total` behaves as specified and every other top-level definition in `report.py` has an unchanged AST, with `broken.ini` byte-identical (s7) |
| `scope` | Nothing outside the step's own `tN/` directory changed during the step |
| `reward` | `outcome` and `scope` both pass (1), otherwise 0 |

`scope` compares the workspace with a snapshot taken right before the step
by the step's `setup.sh` (`steps/<name>/workdir/setup.sh`). The script writes
SHA-256 hashes of every file under `/app` to `/var/lib/agent-smoke/<step>.json`,
then deletes itself from `/app`, because Harbor uploads it into the working
directory where the agent would see it. Ignored by `scope`: `__pycache__/` and
`.pytest_cache/` anywhere, and dot-directories at the workspace root (harness
state such as `.kodo/`). The snapshot is outside the workspace but not
protected: the checks are not designed to resist an agent that sets out to
cheat.

The trial's `reward` is the mean over the seven steps, i.e. the share of tasks
done correctly, and the trial counts as solved only at 7/7.

#### 5.4.4 Reporting

`kodo-summary.md` today shows only `reward` per arm. Add a **Steps** table for
multi-step tasks: one row per step name, holding the mean of that step's
`reward` (and of any other key it writes) across trials, read from each
trial's `step_results`. It is generic: shown for any multi-step task, not
special-cased for `agent-smoke`. For a smoke test it reads, e.g.,
`s4-read-conventions 0/1`, which points at what the prompt fails to make the
agent do (here, read a file the instruction names).

#### 5.4.5 Packaging and acceptance

- Layout: `src/kodo/harbor/tasks/agent-smoke/instruction-following/`, holding
  `task.toml`, `environment/{Dockerfile,make_fixture.py}`,
  `steps/<name>/{instruction.md,workdir/setup.sh,tests/{test.sh,check.py}}`
  for each step, each `tests/` also holding a committed copy of
  `make_fixture.py`, and `solution/solve.sh`.
  `src/kodo/harbor/suites/agent-smoke.json` holds
  `"tasks": [{"path": "../tasks/agent-smoke/instruction-following"}]`.
  The suite loader already resolves a relative `path` against the suite
  file's directory.
- The wheel `include` (`pyproject.toml`) gains `src/kodo/harbor/tasks/**`.
  Today it ships only `*.md`/`*.json`, which would drop `task.toml`,
  `Dockerfile` and the scripts.
- `task.toml`: `[agent] timeout_sec = 600` (applied to each step; local models
  are slow), `[verifier] timeout_sec = 120`, `multi_step_reward_strategy =
  "mean"`, no network policy beyond Harbor's default (the agent reaches the
  model through it). The base image is pinned by tag and digest.
- **Adapter check (not yet verified):** Kōdo's Harbor adapter has only run
  single-step tasks. A multi-step trial calls `run()` once per step, resets
  the agent logs directory between steps and calls
  `populate_context_post_run` per step. The first real run must confirm that
  each step gets its own `kodo.jsonl`, result record and `trajectory.json`, and
  that no PID file or session directory leaks from one step into the next.
- Unit tests (no Docker): the suite loads; every copy of `make_fixture.py` is
  identical; for each step, `check.py` scores the fixture with `solve.sh`'s
  edits for that step as 1 on every key, scores the untouched fixture as
  `outcome` 0, and scores a change outside `tN/` as `scope` 0.
- Before shipping, one time only (validating the task, not a per-run comparison):
  Harbor's `oracle` agent scores 1.0. `kodo_problem_solver` scores 7/7 in 3 of
  3 attempts on the smallest local model in the catalog. If it does not, the
  failing step is too hard and is measuring the model, so it is simplified.
  The result is recorded in HARBOR.md.

Per-agent smoke tasks, where a bundle carries tasks that test what *that*
agent is for, are out of scope for v1 (D30).

### 5.5 Surfaces

| Surface | Form |
|---|---|
| CLI | `python -m kodo --validate-agent NAME [--json]` (load + lint). Exits 1 on a load error and 0 with warnings. |
| WS | `agents.validate {name}` → `AgentReport` |
| Panel | Agents table: a **Lint** column (`⚠ N` or `✓`); the row's 🩺 **Validate** button opens `AgentReportModal` with the report, and a finding's line opens the prompt there. The row's 🤖 **Review with AI** button opens a locked session running `kodo_agent_review` (§5.3). The **Smoke test** (P7) action is not built yet |
| Builder | `validate_agent` / `lint_agent` tools, plus the report returned by every write |

---

## 6. The Agent Builder agent

### 6.1 Identity

`kodo_agent_builder`, label "Agent Builder". A top-level, interactive,
selectable agent in the picker (rank 30, after Problem Solver and Guide). It
needs no open project: all its tools are `requires_project=False`, and since
D36 that includes `run_subagent`. `test_agent_builder_pipeline_needs_no_project`
(`test_top_agents.py`) holds the builder, the critic and the review agent to it.

Frontmatter tools: `ask_user`, `use_skill` (+ `{SKILLS}`), `run_subagent`
(+ `subagents: [kodo_agent_critic]`), and the §6.2 tools. It does **not** get
`create_file`/`edit_file`/`run_command`.

### 6.2 Tools

All builder tools are `ToolSpec`s in `kodo.toolspecs` and `Tool` subclasses in
`kodo.tools`. They reach the agent store through a new `AgentWorkshopLike`
Protocol in `tools/_context.py`, exposed as `EngineServices.agent_workshop()`.
The implementation is `kodo.agents.AgentWorkshop`, injected by `runtime`. That
mirrors `LocalCatalogLike` → `LocalCatalogService` and keeps the `kodo.tools`
import ceiling intact.

| Tool | Does | Writes? |
|---|---|---|
| `list_agents` | Every built-in and user agent: name, kind, label, description, `subagents`, `critic`, broken reason | no |
| `read_agent` | One agent's prompt, config or spec JSON, and resolved frontmatter. Built-ins read-only. | no |
| `list_tools` | The live `ToolSpec` catalog: name, description, `when_to_use`, security impact, autonomous behavior, `requires_project`, `modifies_files` (D16) | no |
| `copy_builtin_agent` | §3 with `{name, prefix, replace}`. Returns the plan or the result. | yes |
| `write_agent` | Create or overwrite one **user** agent: `{kind, name, prompt, config_or_spec}`. Validates layout + name before writing, writes atomically, reloads, returns the `AgentReport`. | yes |
| `edit_agent` | A string-match edit to one user agent file (`prompt`/`config`/`spec`), with the same post-write report | yes |
| `validate_agent` | §5.1 + §5.2 for one agent | no |
| `lint_agent` | §5.2 only, on a draft passed inline before it is written (cheap pre-check) | no |

No delete tool (D12). `write_agent`/`edit_agent` refuse a `kodo_*` name,
`subagents` as a name, and any path outside the user root, because the
service builds every path itself.

**Gating (D22):** the three writing tools are `SecurityImpact.MODERATE`, the
same level as `edit_file` and `add_local_llm_quant`. Under the default `smart`
posture they run without asking, like `edit_file`; under the `defensive`
posture the user approves each write, and the gate prompt shows the agent name
and the files touched. The read tools are `MINIMAL`.

As built (2026-10-15). Code: `kodo/agents/_workshop.py` (`AgentWorkshop`,
`AgentWorkshopError`, a `ValueError`); `AgentWorkshopLike` in
`tools/_context.py`; one spec + one `Tool` per builder tool; the engine creates
one workshop per engine over its registry (`_agent_workshop_service`).

- **Paths and names.** Every path comes from `UserAgentStore` and the agent's
  kind and name. A new name must pass `agent_name_error` (promoted from
  `_copy.py`'s copy check, exported from `kodo.agents`): no `kodo_` prefix, not
  `subagents`, `^[a-z][a-z0-9_]*$`, `run_subagent_<name>` ≤ 64 characters.
  `write_agent` also refuses a name the other kind already uses, because agent
  names are one namespace. `AgentRegistry.user_dir` (new property) is the root;
  a registry with none refuses every write.
- **Checked before writing.** Each file is written to a temporary directory and
  loaded with its strict loader (`load_agent`, `load_top_agent`, `load_spec`);
  a top-level config with `"default"` or `"interactive": false` is refused
  there too, instead of being written and demoted. Then each file is replaced
  atomically (hidden temp file + `os.replace`) and the registry reloads once.
  A cross-agent load error (a `subagents:` entry not written yet) is reported,
  not refused, so a pipeline can be written one agent at a time.
- **`write_agent`** fills in the config's `"name"` when absent and refuses one
  that disagrees. Returns `{files, report}`.
- **`edit_agent`** takes `file`: `prompt`, `config` (top-level only) or `spec`
  (sub-agent only). `old_string` must occur exactly once. It works on broken
  user agents, so the builder can repair one; built-ins are refused as
  read-only.
- **`copy_builtin_agent`.** With `replace` omitted and a name taken, nothing is
  written and the result is the plan (`done: false`, `members`, `conflicts`):
  the builder asks the user, then calls again with `replace: true|false`.
  Otherwise the result is `copied`, `kept` and `failed` (copied members the
  reloaded registry demoted).
- **`list_agents`** rows carry `source` (`builtin`/`user`), `copyable`, the
  frontmatter wiring and `error`; a sub-agent's `description` is the first
  sentence of its `## Purpose`, cut at 240 characters.
- **`read_agent`** returns the raw files and the resolved frontmatter, for a
  broken user agent too.
- **`list_tools`** reads `ALL_TOOLS` directly and leaves out `return_result`,
  which every sub-agent is granted automatically.
- **`lint_agent`** parses the draft under its real filename first; a draft that
  does not parse is an `error`, not a finding.
- **Sandbox** (`kodo-headless --headless-sandbox`, HEADLESS.md §4): the three
  writers are denied (they write outside the root), the five readers allowed.

### 6.3 Workflow (sketch for the prompt)

1. Elicit the agent's single purpose, its inputs and outputs, and whether it
   is a top-level agent, a sub-agent, or a pipeline (`ask_user`, one question
   at a time).
2. Load the relevant shipped skills.
3. Choose the starting point: blank, or a built-in copied with
   `copy_builtin_agent` (asking the user for the prefix).
4. Draft with `lint_agent`, then write with `write_agent`/`edit_agent`, then
   fix every load error.
5. Spawn `kodo_agent_critic`. Revise, and repeat, for **at most 3 critic
   rounds** per draft (D27). If concerns remain after round 3, present them
   to the user with `ask_user` and let the user choose: accept as is, revise
   per their direction, or stop.
6. Report what was made, how to select it, and that a **Smoke test /
   Benchmark** is available in Settings → Agents. The builder never runs one
   (D17).

As built (2026-10-15), `agent_kodo_agent_builder.md` follows this with three
additions: the name prefix is one of the elicited decisions; in autonomous
mode (no `ask_user`) it decides alone and lists every decision in one
`<kodo_warn>`; and the draft→lint loop is capped at 3 passes, leftovers listed
in a `<kodo_warn>`, so no stage lacks a stop condition. Agents are written
callees first and critics before authors, so each write's report reflects the
real load state. Until P7 lands, the closing summary names Validate and Review
with AI in Settings → Agents, not a Smoke test or Benchmark, which do not exist
yet.

---

## 7. Shipped skills tier (D14, D15)

### 7.1 Layout and rules

```text
src/kodo/skills/builtin/
  kodo-agent-format/SKILL.md
  kodo-prompt-writing/SKILL.md
  kodo-pipeline-patterns/SKILL.md
  kodo-tool-selection/SKILL.md
```

- `SkillStore` reads **two roots**: the packaged one (read-only) and
  `~/.kodo/skills/`. `kodo.skills` stays a leaf package, since it locates its
  own package directory.
- **Reserved prefix `kodo-`**, the agents rule applied to skills: a user skill
  whose directory starts `kodo-` becomes a broken row naming the rule. That
  makes the two roots collision-free with no shadowing rule.
- Shipped skills appear in `{SKILLS}` for every agent holding `use_skill` and
  in `skills.list` with `builtin: true`. The panel shows a *built-in* pill and
  **View** (open read-only) instead of Delete. `skills.delete` refuses them.

As built (2026-10-15; details in SKILLS.md §9):

- `SkillStore(root, *, builtin_root=BUILTIN_SKILLS_DIR)`; `builtin_root=None`
  reads the user root alone. `Skill.builtin` marks a shipped row.
- The prefix check lives in `load_skill` (`builtin=False` + a `kodo-` name, any
  case → broken), so the store, `install_local_skill` and the repo scan share
  it.
- `skills.delete` acts on the user root only: it refuses a shipped name, but
  still deletes a user directory that took one (otherwise a broken imposter
  could never be cleaned up).
- `builtin/` is a data directory, not a Python package: a `__pycache__/` in it
  would list as a broken skill. The wheel's `src/kodo/**/*.md` include already
  ships the `SKILL.md` files.
- Panel: the pill sits under the name; **👁 View** opens `SKILL.md` and runs
  `workbench.action.files.setActiveEditorReadonlyInSession`; the trash icon is
  shown disabled so the action column stays aligned. The 📁 Open-folder action
  is not offered for a shipped skill, since it would invite edits inside the
  installed package.
- A skill **description** must not contain token syntax: it is rendered into
  every `use_skill` agent's prompt, where a literal `{SKILLS}` survived
  expansion as text. A test checks every shipped description for `{`.

### 7.2 Content

| Skill | Content | Kept in sync by |
|---|---|---|
| `kodo-agent-format` | Bundle layout, frontmatter keys, `<name>.json`, spec JSON + shapes, `{SHARED}`/`{SKILLS}`/`{PHASE}`, strict vs relaxed rules | Test: every `FRONTMATTER_KEYS`, `TOP_AGENT_KEYS` (`kodo.agents`), `SPEC_KEYS` and `SPEC_SHAPES` (`kodo.agents.subagents`) entry, every shared block, `{SKILLS}` and both phase tokens appear in the skill. The four constants were added for this test; `FRONTMATTER_KEYS` is hand-kept beside `load_agent`, which ignores unknown keys. |
| `kodo-prompt-writing` | The CLAUDE.md "Prompts" rules, generalized for user agents: purpose sentence, section order, no vague qualifiers, quote sources, name tools and never restate schemas, no free-form output for anything a reporting tool covers | Lint rule IDs (§5.2) are cited in the skill; a test checks every rule ID appears |
| `kodo-pipeline-patterns` | ADDING_A_SUBAGENT §5 recipes: plain, author↔critic, `user_review`, `planner`, `standalone`, per-component stage | Review only |
| `kodo-tool-selection` | How to choose tools: least privilege, `modifies_files` ⇒ `{SHARED:editing}`, read-only investigators, critics hold no writing tools. Points to `list_tools` for the live catalog. | Review only |

---

## 8. Benchmarking from the panel (D17–D20)

- **Where (D23):** a **Benchmark…** and a **Smoke test** action on every
  selectable top-level agent, built-in or user. The user rows live in the
  Agents table. Built-ins get a short read-only "Built-in agents" list above
  it, which is also where **Copy…** starts (§3.5). Sub-agents cannot run
  alone; benchmark a top-level agent that uses them.
- **Modal fields:** model (rows from `kodo-harbor list-models --json`
  semantics, served by a new control message `harbor.options` that calls
  `kodo.harbor` directly), suite (built-in + `~/.kodo/harbor/suites/`),
  attempts, thinking level, and a ☐ *terminus-2 control* checkbox. **Smoke
  test** pre-fills suite `agent-smoke`, 1 attempt, no control (§5.4).
- **GPU warning (D20):** if the model is local and the server currently has a
  llama-server loaded, the modal says the two may not fit in memory together
  and offers *Continue* or *Cancel*. It does nothing automatically.
- **Run (D19):** kodo-vsix opens an integrated terminal and runs
  `kodo-harbor run --agent <name> --model <m> --suite <s> --attempts <k> [--control terminus-2] [--thinking-level <t>] --jobs-dir ~/.kodo/harbor/jobs --job-name <agent>-<timestamp>`
  using the `kodo-harbor` from the extension's managed environment. On
  shell-integration command end, it opens `<job>/kodo-summary.md` if the file
  exists.
- **Preflight stays in `kodo-harbor`:** Docker, uv, credentials, tier, and
  `~/.kodo/agents` existence are all reported in the terminal. kodo-harbor
  already installs `~/.kodo/agents` into the container for a non-built-in
  agent.

---

## 9. Bringing built-in prompts up to the rules (D9)

The lint (§5.2) is run over every built-in. Measured on 2026-10-11, before any
rule refinement:

- **Missing `## Inputs` / `## Workflow` / `## Reporting`:** `kodo_guide`,
  `kodo_problem_solver`, `kodo_investigator`, `kodo_planner`,
  `kodo_compactor`, `kodo_web_search`, `kodo_toolchain_depsmgr`, plus partial
  gaps in most critics and `kodo_developer`.
- **Vague-qualifier hits:** from 0 to 8 per prompt (`kodo_guide` 6,
  `kodo_requirements_critic` 8, `kodo_narrative_author` 8).

Re-measured on 2026-10-14 with the shipped lint (`--validate-agent`): every
built-in except `kodo_model_importer` has at least one warning. L4 fires on all
25 others (a multi-sentence opening paragraph); L5 on `kodo_guide`,
`kodo_problem_solver`, `kodo_investigator`, `kodo_planner`, `kodo_developer`,
`kodo_toolchain_builder`, `kodo_architect_critic` and `kodo_requirements_critic`;
L6 from 0 to 6 per prompt (`kodo_guide`, `kodo_narrative_author` 6); L1 on
`kodo_guide`, `kodo_narrative_author` and `kodo_toolchain_depsmgr`; L2 only on
`kodo_guide` (the placeholder `run_subagent_X`). Totals range from 1
(six agents, e.g. `kodo_architect`) to 13 (`kodo_guide`).

The plan was to gate the rewrite, because it is behavior-risky:

1. Re-measure with the final L6 (D29). The counts above use a naive word list with no exemptions.
2. Baseline: `kodo-harbor run --suite terminal-bench-sample` on a fixed model
   for `kodo_problem_solver` and `kodo_guide`.
3. Rewrite in small batches (one pipeline stage per commit). After each
   batch, the built-in lint test is green for that batch.
4. Re-run the baseline. A drop beyond noise blocks the batch.

**What happened (2026-10-15).** Step 1 found 95 warnings over 25 agents
(only `kodo_model_importer` was clean), the same pattern as the 2026-10-14
measurement. Steps 2 and 4 were waived (D34), and the rewrite was minimal
(D35), so the only check on behavior is the diff itself:

- **L4:** the opening paragraph was split after its first sentence, merging
  "You are **X**. You do Y." into "You are **X**: you do Y." where the first
  sentence named no purpose.
- **L5:** headings renamed to the exact names (`## Procedure` → `## Workflow`,
  `## What to avoid` → `## What to Avoid`, the guide's `## Deciding the Next
  Step` → `## Workflow`, the compactor's `## What You May Remove` → `## What You
  Can Remove`). Missing sections were added from what the agent already
  receives: `## Inputs` from the spec's input fields (`kodo_architect_critic`,
  `kodo_requirements_critic`, `kodo_investigator`, `kodo_planner`,
  `kodo_toolchain_builder`), and `## Reporting` by moving the existing
  "call `return_result` once …" step text (`kodo_developer`,
  `kodo_investigator`, `kodo_planner`, `kodo_toolchain_builder`).
- **L6:** permission `may` → `can`, hypothetical `could` → `can`/`did`, and a
  definite statement where the word hid one (`almost always implies` → `implies
  … unless the sub-narrative states otherwise`).
- **L1/L2:** tools another agent holds are named in plain words
  (`toolchain_build` → "toolchain build tool"), and the guide's placeholder
  `run_subagent_X` became "X's `run_subagent_<name>` tool".

The ADDING_A_SUBAGENT §3.5 worked example was brought up to the rules too. It
also did not **load**: it granted `run_command`, which changes files, without
`{SHARED:editing}`, while the text claimed it held no file-changing tool.
`run_command` was dropped, since its workflow never runs a command.

One finding was left as it is: `kodo_requirements_critic`'s spec consumes
`narrative`, while its prompt says it does not see the Narrative. The new
`## Inputs` lists only what the prompt already claimed.

Engine-driven agents (`kodo_compactor`, `kodo_web_search`,
`kodo_toolchain_depsmgr`) cannot be copied (§3.1) and are exempt from L5
(D26). They are still linted for L1–L4, L6 and L7.

---

## 10. Layering

| Code | Package | Notes |
|---|---|---|
| Copy, workshop service, lint, JSON Schema generation | `kodo.agents` | Reuses `_loader`, `_userstore`, `_install` (conflict report) |
| Builder tool specs | `kodo.toolspecs` | One module per tool |
| Builder tool dispatch | `kodo.tools` | Via `AgentWorkshopLike`. The import ceiling grep stays empty. |
| Shipped skills, second root, reserved prefix | `kodo.skills` | Stays a leaf |
| `agents.copy_scan`, `agents.copy`, `agents.validate`, `harbor.options` | `kodo.server`, `kodo.transport` | WS_PROTOCOL §7.6l extended |
| `agent.run` for `kodo_agent_review` | existing | Model Importer mechanism |
| Panel: Copy modal, Validate report, Benchmark modal, built-in skill pills, `jsonValidation` | kodo-vsix | |

---

## 11. Phasing

| Phase | Delivers | Depends on |
|---|---|---|
| P1 ✅ | Deep copy: `_copy.py`, CLI, WS, panel modal (landed 2026-10-14) | — |
| P2 ✅ | Lint + `--validate-agent` + `agents.validate` + panel report (landed 2026-10-14) | — |
| P3 ✅ | Fix built-in prompts to lint clean (§9; gate waived, D34) (landed 2026-10-15) | P2 |
| P4 ✅ | Shipped skills tier + the four skills (landed 2026-10-15) | P2 (rule IDs) |
| P5 ✅ | Builder tools + `kodo_agent_critic` + `kodo_agent_builder` (landed 2026-10-15; `run_subagent` ungated, D36) | P1, P2, P4 |
| P6 ✅ | Panel "Review with AI" (`kodo_agent_review`) (landed 2026-10-15) | P5 |
| P7 | Panel Benchmark / Smoke test via terminal. Also add the Smoke test / Benchmark to the builder's closing summary (§6.3) | P9 (for the Smoke test preset) |
| P8 | JSON Schemas + kodo-vsix `jsonValidation` | — |
| P9 | `agent-smoke` suite + task, wheel `include`, the summary's Steps table, multi-step check of the adapter (§5.4) | — |

P1, P2, P8 and P9 are independent of each other.

---

## 12. Open questions

None.

O10 (2026-10-15): D22 said `MODERATE` makes the user approve each write under
the default posture, but the default `smart` posture lets every call below
`HIGH` through (`security/_layer.py`). The user kept `MODERATE`, the same
gating as `edit_file`, and D22 was corrected.

O11 (2026-10-15): `run_subagent` was `requires_project=True`, so the builder
could not reach its critic with no project open, contradicting §6.1. The user
removed the gate for every sub-agent spawn (D36).

Resolved: O1 → D21, O2 → D22, O3 → D25, O4 → D23, O5 → D26 + D29, O6 → D27,
O7 → D24, O8 → D28, O9 → D33, O10 → D22 (corrected), O11 → D36.
