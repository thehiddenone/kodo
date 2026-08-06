# Kodo — Automated Validation Harness (`kodo.validator`)

> Status: **phase 2 — evaluation mechanics**, plus **validation suites**
> (§10). The harness runs real kodo sessions end-to-end, can answer the
> agent's questions with the **validation LLM** (User Proxy Prompt), and can
> score the finished run with a judge session (Result Validation Prompt) into
> `ScenarioResult.score` + `report.md` — see §9. A `Scenario` is **content
> only**: which LLM(s) (+ flavor) exercise it is supplied by the caller of
> `run_scenario` (§3a) or by a `ValidationSuite` (§10) — a batch of
> LLM-under-test/scenario pairs, one fixed judge, and a final judge-produced
> comparative summary across every LLM the suite validated. The curated suite
> ships real task / UPP / RVP content in the **`kodo.validator.prompts`**
> package (§8b); ad-hoc runs still take caller-supplied `--upp-file`/`--rvp-file`,
> and with neither UPP nor RVP set, phase-1 behaviour (scripted answers,
> `score=None`) is unchanged.
> Reference: [WS_PROTOCOL.md](WS_PROTOCOL.md) (§7.6a/§7.6b), [SESSIONS.md](SESSIONS.md),
> [SECURITY.md](SECURITY.md), [SETTINGS.md](SETTINGS.md),
> [LLM_REGISTRY.md](LLM_REGISTRY.md), [LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md).

## 1. Purpose

Estimate the performance of kodo's agentic workflows (agent/sub-agent prompts)
automatically: run a session exactly the way a VS Code user would — same
server, same wire protocol, same tools, same gates — but with **no VS Code and
no human**, then leave behind a complete transcript that a scorer can grade
from 0 (fail) to 100 (perfect).

Design rule: the harness sits **entirely on the client side of the WebSocket**.
It never imports engine internals — only `kodo.common` (Envelope) and the
`kodo.transport` message constants — so a validation run exercises the exact
same contract the extension does, and protocol drift breaks the validator
loudly instead of silently diverging.

## 2. Architecture

```
src/kodo/validator/
  _home.py        clone_kodo_home()   — isolated ~/.kodo per run
  _server.py      ServerProcess       — python -m kodo.server subprocess
  _workspace.py   SimulatedWorkspace  — fake VS Code workspace on disk
  _client.py      ValidatorClient     — the pseudo-extension (WS protocol)
  _models.py      ensure_local_llms_installed() — the two mandatory LLMs (§3a)
  _user.py        UserSimulator / ScriptedUser — answers interactive gates
  _vllm.py        VLLMUserProxy       — questions answered by the VLLM (§9.1)
  _evaluate.py    run_evaluation()    — the RVP judge session (§9.2)
  _transcript.py  Transcript          — every frame + interaction, JSONL
  _harness.py     ValidationHarness   — one run, end to end; Modes; TurnResult
  _scenario.py    Scenario / run_scenario / ScenarioResult — content-only scenario
  _suite.py       ValidationSuite / LLMUnderTest / run_suite (§10)
  __main__.py     python -m kodo.validator / kodo-validator CLI (one scenario)
  prompts/        PromptRegistry / PROMPTS — reusable task/UPP/RVP/summary .md (§8b)
  scenarios/      the curated scenario content + selector resolver (§8a)
  suites/         the curated suites + selector resolver (§10.3)
```

Every run owns one directory:

```
<run_dir>/home/.kodo                isolated kodo home (see §3)
<run_dir>/home/server-console.log   server subprocess stdout+stderr
<run_dir>/workspace/<root>/…        simulated workspace folders
<run_dir>/transcript.jsonl          every frame + interaction, in order
<run_dir>/judge-transcript.jsonl    the RVP judge session's frames (§9.2)
<run_dir>/report.md                 the judge's score + report (§9.2)
<run_dir>/summary.json              per-turn digest (scenario runs)
```

A suite run (§10) owns a parent directory instead — one subdirectory per entry
(each exactly the layout above), plus `summary/` (the final round's own
harness) and `<run_dir>/suite-report.md` + `suite-summary.json`.

## 3. Isolated home (`clone_kodo_home`)

The server is a machine-wide singleton rooted at `$HOME/.kodo`
(`kodo.project.kodo_user_dir`). The harness therefore spawns the server with
`HOME`/`USERPROFILE` pointed at `<run_dir>/home`, and populates
`<run_dir>/home/.kodo` from a **template** kodo home (typically the real
`~/.kodo`, `--template-home` on the CLI):

- **symlinked**: `bin/`, `llama.cpp/` — llama.cpp builds and GGUF models;
  local inference works without copying tens of GB. (A download during a run
  writes through the symlink into the template's `llama.cpp/models` — accepted
  by design.) Also `titler/` — the session-titler's cached GGUF model *and* its
  own llama-server runtime state (`kodo.titling`, doc/INTERNALS.md §10c), for
  the same reason: without this, every run would redownload the model on
  startup. `start_titling` checks the local manager state before downloading
  anything, so once cached (shared via this symlink), no run ever re-downloads
  or re-lists it from HuggingFace.
- **skipped**: `sessions/`, `logs/`, the `kodo-server` discovery file —
  per-run state starts fresh, and a stale discovery PID can't confuse the
  spawned server.
- **copied**: everything else — `etc/settings.json`, the local-LLM registry, …
  so a run can mutate its settings freely.

`settings_overrides` (harness/scenario arg) deep-merges into the cloned
`etc/settings.json` before the server starts — e.g. pin `mode`/`models` for a
particular validation matrix. Cloud API keys are *not* in the home (the real
extension keeps them in VS Code SecretStorage); see §6.

## 3a. The two mandatory LLM keys

`ValidationHarness` requires two **local registry names**
(`kodo/doc/LLM_REGISTRY.md`), both mandatory — there is no default:

- **`llm_under_test`** — the model this run actually exercises. `start()`
  forces it onto the cloned `etc/settings.json` (`mode: "local"`,
  `models.local: llm_under_test`) *on top of* any `settings_overrides`, so a
  run always drives the model it claims to, regardless of what else a
  scenario pins.
- **`validation_llm`** — a fixed, capable model that answers the LUT's
  questions (User Proxy, §9.1) and judges the finished run (§9.2). Installed
  up-front either way, so runs that enable neither still leave it ready.

A `Scenario` itself names **neither** — it is content-only (prompts, roots,
modes, UPP/RVP text). The caller supplies both names: `run_scenario(scenario,
out_dir, llm_under_test=…, validation_llm=…)` for a single scenario (§8, §8a),
or a `ValidationSuite` for a batch that validates several LUTs at once (§10).
This is what lets the same scenario content run, unmodified, against several
different LLMs.

Both `llm_under_test` and `validation_llm` also take an optional **flavor**
id (`ValidationHarness.flavor`/`validation_llm_flavor`, `run_scenario`'s
`flavor`/`validation_llm_flavor` params, `LLMUnderTest.flavor` in a suite) —
see §8a.1. `run_scenario`'s `flavor` defaults to `"default"` so every run pins
a known, deterministic flavor unless the caller names another; a suite's
`judge_llm_flavor` defaults to `None` (leave the judge's current flavor as-is).

Both `llm_under_test` and `validation_llm` are always local (GGUF/llama.cpp)
models — cloud models are API-based and have no "download" step, so they're
out of scope for this mandatory pair.

**Ensuring presence.** After `hello()` (which returns `local_registry`, the
same list `doc/WS_PROTOCOL.md` §4.1 documents), `ValidationHarness.start()`
calls `_models.ensure_local_llms_installed()` for both names:

1. Unknown name (absent from `local_registry`), or present but not installed
   and not a downloadable `kind` (only `hardcoded_hf`/`custom_hf` can be
   auto-installed) → raises `LocalModelUnavailableError` immediately.
2. Already `installed: true` → nothing to do.
3. Otherwise → sends `local_llm.install {name}` (`doc/WS_PROTOCOL.md` §7.6) via a
   new fire-and-forget `ValidatorClient.send()` (not `request()` — the server
   never sends a correlated `response` for this message, only `event` frames,
   so `request()` would just time out waiting for one), then polls
   `manager-state.json` on disk once a second until every file finishes,
   fails, or `poll_timeout` (default 1800s) elapses — the exact disk-polled
   pattern `doc/LOCAL_MODEL_MANAGER.md` §11 documents for kodo-vsix, chosen so
   this package never has to import engine-side `kodo.llms` code (§1's "never
   import engine internals" rule). The models directory is resolved the same
   way the server does (`llm_models_dir` setting, else `llama.cpp/models`)
   without importing that resolution logic either — it's just JSON/path
   logic.

Because `llama.cpp/` is symlinked from the template (§3), a download during
`ensure_local_llms_installed` writes through to the template's real
`llama.cpp/models` — the same "accepted by design" sharing §3 already
describes for any other in-run download.

## 4. Workspace simulation (`SimulatedWorkspace`)

The engine's entire view of the user's workspace is the `workspace.folders`
message (`{physical_root, folders: {name: path}}` — WS_PROTOCOL.md §7);
`get_root_paths` and Problem Solver logical-path resolution are served from
that pushed map. So simulating a workspace is: create real directories under
`<run_dir>/workspace/`, optionally **seed** them by copying files/directories
from elsewhere (`add_root(seed_from=…)`, `seed()`, `write_file()`), and push
the payload. One root ≙ a single-root window; several ≙ a multi-root
workspace. `ValidationHarness.sync_workspace()` re-pushes after changes,
mirroring the extension's `onDidChangeWorkspaceFolders`.

Tool execution needs nothing further from the harness: all tools already run
inside the engine (server side); the workspace push is what points them at the
simulated roots.

## 5. Driving a session

`ValidationHarness` composes the pieces:

1. `start()` — clone home → spawn `ServerProcess` (free loopback port, ready
   when the port accepts) → `ValidatorClient.connect()` + `hello` (mints or
   resumes a session) → push `workspace.folders` if roots exist.
2. `apply_modes(Modes)` — the four toggles: `mode.set` (autonomous),
   `workflow.set` (guided / problem_solving), `edit_control.set`,
   `command_control.set`. They apply to the *next* prompt (frozen-toggle
   semantics, WS_PROTOCOL.md §5.1).
3. `submit_prompt(text)` — `prompt.submit`, then block until the turn ends;
   returns a `TurnResult` (final phase, assistant text, tool calls with
   prep+detail merged, interactions, errors, raw entries).
4. `shutdown()` — close WS, SIGTERM the server, close the transcript.

**No separate project-binding step.** As of 2026-07-24, Guided-mode scenarios
no longer call a `bind_project`/`project.set` step — there isn't one any more
(WS_PROTOCOL.md §7.1c). A `guided`-workflow scenario's bound projects are
simply its `scenario.roots` (step 1's `workspace.folders` push), exactly like
a `problem_solving` scenario's.

**Turn-end detection.** A turn is over once the phase was seen `running` since
submit and has settled back to a resting phase (`awaiting_user` / `done` /
`stopped` / `error`) with no simulated interaction in flight. Because
`awaiting_user` is also the phase *while* a `prompt.*` request is pending, and
the engine flips back to `running` shortly after an answer lands, the resting
condition must hold through a settle window (default 2 s) before it counts.

## 6. The simulated user (`UserSimulator`)

Server→client `kind=request` frames are answered automatically and **every
exchange is logged** as an `interaction` transcript note (request + response),
so phase 2 can score how the agent used the user:

| Request | Default `ScriptedUser` reply |
|---|---|
| `prompt.question` (ask_user) | scripted per-batch answers if provided, else first option / free-text fallback |
| `prompt.approval` (document review gate) | `agree` (configurable `feedback` + text) |
| `prompt.permission` (security gate) | `allow` (configurable `deny` + feedback) |
| `api_key.request` | explicit `api_keys` map → `KODO_VALIDATOR_API_KEY_<VENDOR>` → `<VENDOR>_API_KEY` env; none ⇒ `{"error":"cancelled"}` |

Any `UserSimulator` implementation can replace `ScriptedUser` (per scenario)
for adversarial or persona-driven simulation.

With a `user_proxy_prompt` configured, the harness wraps whatever simulator a
scenario supplies in a `VLLMUserProxy` (§9.1): `prompt.question` batches are
answered by the validation LLM, while approvals/permissions/API keys keep
the wrapped simulator's behaviour (allow-all + logging by default).

## 7. Transcript

`Transcript` records every frame in arrival order (`recv`/`send` +
frame kind), plus `note` entries: `lifecycle` (connect/modes/llms/shutdown),
`interaction` (§6), and `stream_assembled` (each token/thinking stream
re-assembled on `stream_end`, so scorers don't reassemble chunks). In-memory
list + append-only `transcript.jsonl`; read-side helpers (`assistant_text`,
`tool_calls`, `interactions`, `errors`, `cumulative_usd`) are what
`TurnResult` and the future evaluator consume.

## 8. Entry point

```bash
# ad-hoc run
uv run kodo-validator \
  --template-home ~/.kodo \
  --llm-under-test llamacpp-qwen36-27b-q4-k-xl \
  --validation-llm llamacpp-qwen36-27b-q8 \
  --flavor default \
  --root app=/path/to/seed-project --root lib \
  --workflow problem_solving --command-control permissive \
  --prompt "Find and fix the failing test" \
  --upp-file suites/prompts/upp.md \
  --rvp-file suites/prompts/rvp.md \
  --out kodo-validator-runs

# scenario file: defines SCENARIO or SCENARIOS (content-only — no LLM on the
# file itself, see §3a; can also carry ScriptedUser scripts)
uv run kodo-validator --scenario suites/smoke.py \
  --llm-under-test llamacpp-qwen36-27b-q4-k-xl --validation-llm llamacpp-qwen36-27b-q8
```

`--llm-under-test`/`--validation-llm` are **always** mandatory — whether the
scenario(s) came from `--scenario FILE` or inline flags, since a `Scenario`
names no LLM (§3a); every scenario `main()` resolves for one run executes
against the same pair. `--flavor` (default `"default"`) and
`--validation-llm-flavor` (default: leave as-is) pin flavors the same way
(§8a.1). `--upp-file`/`--rvp-file` (optional, independent) enable the §9
machinery for ad-hoc runs; a scenario file sets
`user_proxy_prompt`/`result_validation_prompt` directly.

`python -m kodo.validator` is equivalent. Exit code 0 iff every scenario
completed with no `error`-phase turn. Each scenario gets a fresh home + server.

To validate **several** LLMs together (each potentially with different
scenarios and its own flavor) and get one final comparative summary across
all of them, use a `ValidationSuite` instead (§10) —
`python -m kodo.validator.suites` / `hatch run validate-suite`.

## 8a. The scenario suite and `hatch run validate`

For a curated batch (as opposed to ad-hoc `--scenario FILE` runs), scenario
content lives **in the package** under `kodo/validator/scenarios/` and runs
through a build recipe:

```bash
hatch run validate tictactoe_detailed_task --llm-under-test NAME --validation-llm NAME
hatch run validate toolchain_python --llm-under-test NAME --validation-llm NAME
hatch run validate all --llm-under-test NAME --validation-llm NAME
hatch run validate --list                         # show available scenarios
```

**One scenario == one `.py` file** that defines a module-level `SCENARIO`
(a `Scenario`); a file may define `SCENARIOS` (a list) instead. The file is
**content-only** — modes, roots, timeouts, UPP/RVP text — and names no LLM
(§3a); every scenario this invocation resolves runs against the **same**
`--llm-under-test`/`--validation-llm`/`--flavor`. Task / UPP / RVP text itself
lives in the `kodo.validator.prompts` package (§8b) and the scenario pulls it
by name, so the same prompts can back several LLMs-under-test. Files live flat
in the `kodo.validator.scenarios` package — no per-model sub-directories,
since scenarios carry no LLM pin of their own (that's a `ValidationSuite`
concern, §10) — though the resolver still supports nesting scenarios in
sub-directories to group them by task family if one is ever added; a
directory name need not be a valid Python identifier, because scenarios are
loaded by **file path**, not imported.

A command-line **selector** (`kodo.validator.scenarios.resolve_selectors`) is a
dotted path under the package: `tictactoe_detailed_task` →
`tictactoe_detailed_task.py`; a sub-directory name (if any scenarios use one)
→ every scenario under it; `all` → the whole tree. Selectors are resolved and
de-duplicated into the full batch **before anything runs**.

**Pre-flight model note (no fail-fast).** Once the batch is known, the runner
disk-checks the template home (`~/.kodo`) with `missing_local_llms(kodo_dir,
{llm_under_test, validation_llm})` (reads `manager-state.json`, no server). Any
not-yet-installed model is **logged, not fatal** — the per-run harness
(`ensure_local_llms_installed`) downloads it into the global `~/.kodo` (through
the clone's `llama.cpp` symlink) before that scenario prompts. Artifacts land
under `~/.kodo-validation/runs` by default (`--out` overrides). Entry point:
`python -m kodo.validator.scenarios`.

To pin a *different* LLM per scenario in one batch (the reason most of the
shipped scenario families exist — a weak LUT for one task, a strong one for
another) — use a `ValidationSuite` (§10) instead of this per-invocation,
uniform-LUT runner.

### 8a.1 Pinning a flavor: validating **sampling parameters**

`run_scenario`'s `flavor` (default `"default"`) names a flavor id of
`llm_under_test` to make active before the first prompt;
`validation_llm_flavor` (default `None` = leave as-is) does the same for
`validation_llm`. In a `ValidationSuite`, these live on `LLMUnderTest.flavor`
(also defaults `"default"`) and `ValidationSuite.judge_llm_flavor` (defaults
`None`) respectively — see §10. Either way, the harness sends
`local_llm.set_active_flavor` over the WebSocket for each named model (never
touching `local-llm-registry.json` directly — the harness drives the server
only through the protocol the extension uses, §1), between
`ensure_local_llms_installed` and the first prompt.

That ordering is deliberate and worth understanding: at that moment
`llama-server` has **not yet been launched** for the run — it starts lazily on
the first prompt — and the server-side handler only restarts it when it is
already running for the affected model. So the call is pure persistence, and
the first launch picks the flavor's `llama_args` up as its initial launch
config. No restart, no race. `llm_under_test` and `validation_llm` are pinned
independently of each other and of which one is "active" — flavor selection
lives on the registry entry, not on run-time state.

This is what makes sampling testable end to end. A flavor's `llama_args` *are*
`llama-server`'s command line, so a pinned run exercises the **CLI-level**
layer of [SAMPLING.md](SAMPLING.md) §9 — exactly what a user gets by choosing
that flavor from the sidebar dropdown — rather than the request-level session
overrides. Re-running one scenario against a different `flavor=`/
`LLMUnderTest.flavor` is the intended way to compare presets
([QUANT_SAMPLING.md](QUANT_SAMPLING.md) §7). The `"default"` default pins a
known, deterministic flavor unless the caller names another.

Note that no scenario pins a **seed**: one deterministic run says nothing about
robustness, so a preset comparison should run a scenario several times and read
the pass rate rather than trusting a single score.

### 8a.2 Prompt attachments

`Scenario.attachments` is `{index into prompts: [source paths]}` — a dict
rather than a per-prompt field, so `prompts` stays a plain `list[str]` and
existing scenarios are untouched. Each source is copied into
`<run_dir>/attachments/` by `ValidationHarness.stage_attachment` before the run
starts (so a missing file fails immediately, not after a model download), and
sent with its prompt.

**No protocol change is involved.** Attachments are a *client-side* convention:
the extension prepends one control line, `<!--KODO_ATTACHMENTS:["/abs/a.md"]-->`,
to the prompt text, and the server parses and strips it before the LLM sees
anything ([WS_PROTOCOL.md](WS_PROTOCOL.md) §7.1). The harness does the same,
so `MSG_PROMPT_SUBMIT` still carries plain `text`. The `TurnResult.prompt`
recorded in the transcript is the **clean** text without the marker — what the
LLM was actually asked.

Attachments are staged **outside** the simulated workspace on purpose. The
point of attaching a file rather than seeding it into a root is that
`read_attachment` — which requires reproducing the attachment's UUID verbatim
from context — becomes the *only* way to reach it. A file inside a root would
be reachable with `find_files`/`read_file`, letting a run pass without ever
exercising that path.

`RootSpec.files` (`{path relative to the root: content}`) is the companion for
small inline fixtures, written after `seed_from` is applied. It keeps a
scenario's input data in the same file as the expected results its RVP asserts,
which is where a reviewer needs to see both.

**HuggingFace cache across runs.** Every run's server child has `HOME`
redirected to its throwaway home, which would otherwise push HuggingFace's
default cache under that home. `ServerProcess` (`build_child_env`) instead pins
`HF_HOME` to the **real, global** cache, covering both GGUF downloads
(`kodo.llms.local`) and the session titler's own model download
(`kodo.titling`). The titler needs no separate offline flag the way its old
`transformers`-based predecessor did: `start_titling` checks its local
manager state (shared globally via the symlinked `~/.kodo/titler`) before
downloading anything, so once cached, no run re-downloads or even re-lists it
from the Hub.

## 8b. Reusable prompts (`kodo.validator.prompts`)

Scenario prompt text is **not** inlined in the scenario `.py` files — it lives
as `.md` files under `kodo/validator/prompts/`, and scenarios pull it by name
through a package singleton:

```python
from kodo.validator.prompts import PROMPTS
PROMPTS.get("tictactoe/detailed_task")   # → prompts/tictactoe/detailed_task.md
```

A name is a `/`-separated path under the package (with or without the `.md`
suffix), so prompts group into sub-directories ("submodules") — one folder per
scenario family. `PromptRegistry.get` resolves and caches the file, rejecting
absolute names and `..` traversal before touching disk; `names()` lists
everything shipped. The `.md` files ship in the wheel via the existing
`include = ["src/kodo/**/*.md"]` rule.

**Convention.** Group a family in its own sub-directory (within
`kodo.validator.prompts`) and suffix the files `_task` (the prompt under
test), `_upp` (user-proxy prompt), and `_rvp` (result-validation prompt).
**Share one `_upp` and one `_rvp`** across variants that differ only in their
`_task`. The two shipped tictactoe scenarios do exactly this:
`tictactoe_detailed_task` uses `tictactoe/detailed_task` (fully specified) and
`tictactoe_sparse_task` uses `tictactoe/sparse_task` (deliberately vague,
forces `ask_user`), and **both** reuse `tictactoe/upp` and `tictactoe/rvp`.
Wire a new LUT to an existing family by pairing a new `LLMUnderTest` with that
scenario in a `ValidationSuite` (§10), reusing those prompts. Because the
`_task` prompt states whether the assistant should ask before building, the
*same* shared `_rvp` grades both — the judge reads the process expectation
from the task, not the rubric (§9.2).

A prompt can also carry a `{placeholder}` a scenario file fills in with
`str.format()` before handing it to `Scenario.prompts` — the variation doesn't
always need a second `_task` file. The `toolchain_<language>` family (12
files, `toolchain_c.py` … `toolchain_typescript.py`) does this: all twelve
pull the same `tictactoe_toolchain/task` and format in a different
`language`, and all share one `tictactoe_toolchain/upp` and
`tictactoe_toolchain/rvp`. This family exercises **Problem Solver's toolchain
triggers** (an explicit "write tests" + "set up the toolchain" ask), so a
clean run spawns `toolchain_builder` for the named language; its `_rvp` is
also the first to lean on the judge's `toolchain_build` capability (§9.2) to
get real build/test evidence rather than inferring from a read-only pass, and
explicitly excludes the computer opponent's playing strength/algorithm from
scoring — only its legality.

## 9. Phase 2 — the validation LLM in the loop

Phase 2 puts the **validation LLM** (VLLM) on both sides of the run: it plays
the *user* while the LLM-under-test (LUT) works (§9.1), and it plays the
*judge* once the LUT is done (§9.2). Three prompts are involved — the **PUT**
(prompt under test, the scenario's normal `prompts`), the **UPP** (User Proxy
Prompt: how to answer the LUT's questions), and the **RVP** (Result
Validation Prompt: how to rate the outcome). Writing the UPP/RVP content is
**phase 3**; phase 2 is the mechanics that carry them.

Both features are opt-in and independent: `Scenario.user_proxy_prompt`
enables §9.1, `Scenario.result_validation_prompt` enables §9.2; with neither,
phase-1 behaviour is bit-for-bit unchanged.

The whole phase rides on two protocol commands added for it
(WS_PROTOCOL.md §7.6a/§7.6b, first-class — any client may use them):

- **`llm.select {name}`** — synchronous local-model switch: persists
  `mode`/`models.local`, restarts llama-server, and replies only once the
  model actually serves (or failed). This is what makes the LUT↔VLLM swap on
  *one* llama-server safe and observable.
- **`llm.complete {prompt, system?, json_schema?, thinking_level?}`** —
  session-less one-shot completion on the active local model, scheduled
  through the shared LLMGateway feed; `json_schema` grammar-constrains the
  output so the reply is parseable **by construction**; `thinking_level`
  overrides the model's configured thinking tier for that one call only,
  without touching settings.json.

`Scenario.user_proxy_thinking_level` / `Scenario.result_validation_thinking_level`
are the corresponding scenario-file knobs (both optional, both valid tier
slugs for `validation_llm`'s thinking family — doc/LLM_REGISTRY.md §4.5). The
motivating case is `user_proxy_thinking_level="minimal"`: a reasoning
`validation_llm` left at its default tier can spend most of an `ask_user`
answer's latency thinking rather than answering — pinning it low keeps the
answer fast without touching the LUT's own thinking tier (a *different*
`base_llm` in the normal case).

### 9.1 User Proxy: questions answered by the VLLM (`_vllm.py`)

When the LUT calls `ask_user`, the engine's `fire_questions` awaits the
client's answer **with no timeout** (`runtime/_gates.py`) — the dangling tool
call is what makes the swap safe. `VLLMUserProxy.answer_questions` then runs:

1. `llm.select(validation_llm)` — wait until the VLLM is serving;
2. `llm.complete(system=UPP, prompt=PUT + question batch + wire contract,
   json_schema=answers_json_schema(n), thinking_level=user_proxy_thinking_level)`
   — the schema pins exactly *n* entries of `{selected: [str], free_text:
   str}`; `thinking_level` (when the scenario set one) rides this same call,
   scoped to just this completion;
3. `llm.select(llm_under_test)` — always, in a `finally`: the answer must
   resume the turn on the LUT even when answering failed;
4. reply to the dangling `prompt.question` with the parsed batch.

Normalization: `selected` entries must quote option texts verbatim; anything
else the VLLM "selected" is folded into `free_text` (it chose to say its own
thing — that is signal, not garbage). Unparseable completions retry (default
3 attempts) — then the run **aborts** (`VLLMProxyError`): a run whose answers
silently fell back to scripted defaults would report a score that lies about
how the LUT was steered. For the same reason a failed model switch aborts.

Permission gates are *not* proxied: they are always allowed and fully logged
(§6), per the validation contract. Document-review approvals keep the base
simulator's scripted `agree` — a deliberate scope choice; revisit if Guided
review quality needs judged feedback.

Every exchange stays observable: the `llm.select`/`llm.complete` frames are
in the transcript like all traffic, the final question→answer pair is an
`interaction` note (§7), and switches add `lifecycle {event: llm_selected}`
notes. Note the latency budget: each proxied batch costs two model loads +
one completion, all inside the turn — size `Scenario.turn_timeout`
accordingly.

### 9.2 Result validation: the judge session (`_evaluate.py`)

After the last turn (only if no turn ended in `error` — an infra failure must
not masquerade as a low-scoring run), `run_scenario` calls
`ValidationHarness.evaluate()`:

1. `llm.select(validation_llm)` — and it stays selected; every scenario gets
   a fresh home/server anyway;
2. open a **second session** on the same server (own WS connection,
   `window_id: kodo-validator-judge`, own `judge-transcript.jsonl`), whose
   `hello` carries `thinking_level=result_validation_thinking_level` when the
   scenario set one (WS_PROTOCOL.md §4.1) — seeding the judge session's
   `state.thinking_level` (doc/SESSIONS.md) once, at creation; unlike the
   UPP's per-call `llm.complete` override, the judge's turns are ordinary
   session dispatch with no per-call hook to ride, so this is the only place
   left to pin it. Then push the **same** `workspace.folders` payload, and
   pin friction-free modes (autonomous, **`judge`** workflow,
   allow_all/permissive — its gates are answered by a plain `ScriptedUser`);
3. submit one judge turn: the RVP + a mechanical context block — workspace
   root paths, the PUT(s), and the full interaction log (every question /
   permission / approval + answer) — plus the output contract. Because
   the judge is a *real* kodo session it reads the generated code itself,
   with real tools, by path — nothing is inlined;
4. read the verdict off the judge's **`submit_evaluation` tool call**. A
   session turn cannot be grammar-constrained and an agentic turn's assistant
   text is polluted with exploration narration + tool-argument fragments, so
   asking the judge to *print* a JSON verdict is unreliable. Instead the judge
   submits `{score, report}` through the `submit_evaluation` tool (a `NONE`-
   impact tool the `judge` agent declares, `kodo.toolspecs.
   SUBMIT_EVALUATION`), and `_verdict_from_tool_calls` reads the values off
   that call's `agent.tool_call_detail` rows — structured, no text parsing.
   `_parse_score` (fenced / bare / embedded JSON) stays as a **fallback** for
   a judge that answers in prose anyway; a turn that yields neither gets a
   follow-up turn asking for the verdict again (default 3 attempts), then
   `EvaluationError`.

**The `judge` workflow** (`workflow.set` mode `"judge"`, `kodo.subagents.agent_judge.md`)
is a **dedicated, validator-only entry agent** — almost entirely read-only
(`read_file`, `find_files`, `find_text_in_files`, `submit_evaluation`), no
editing, no general command execution, no sub-agents, no `ask_user`. It also
carries one narrow, scoped exception: `toolchain_build`, the same tool
Problem Solver/`toolchain_builder` runs use to execute a project's generated
`scripts/<step>.{sh,ps1}` build/format/static-analysis/test pair. This lets a
scenario whose RVP asks for it get *real, executed* evidence — actual build
and test-suite output — instead of the judge only inferring correctness by
reading scripts, without granting it any broader command-execution or editing
capability. `agent_judge.md`'s own prose gates the tool to RVP-directed use:
don't invoke it against a project whose rubric never asked for a toolchain
check. Earlier the judge turn ran as a `problem_solving` session, i.e.
through the full `problem_solver` agent (read/write/execute/sub-agent-spawning
tools, none of which judging needs) — a single-responsibility violation kept
only for lack of a narrower entry point. `judge` is a third `workflow_mode`
value alongside `guided` and `problem_solving` (WS_PROTOCOL.md §5.1/§7.4); it
is wired **only** in the engine and the validator harness — kodo-vsix's
workflow picker still offers just `guided`/`problem_solving` and never sends
`judge`, so it stays entirely invisible to and unreachable from the
extension.

**Scope of the verdict.** The judge scores the **whole delivery**, not just
code correctness: the built artifacts, any **written deliverables** the task
called for (design docs, plans, specs, reports — a Guided run can be as much
documentation as code), and the LUT's **conduct** — whether it followed the
task's working instructions, most importantly whether it stopped to `ask_user`
when told to. That last axis is read off the **interaction log** the harness
appends (step 3 above): an empty log against a task that said "ask first" is a
first-class process failure (a hefty deduction), while against a task that said
"just build" it is correct. The judge derives *which* rule applies from the
task prompt, not from a blanket policy — which is exactly why one shared `_rvp`
can grade both the detailed and sparse tictactoe variants (§8b). The prompt's
own scoring rules (start at 100, subtract per distinct defect, deduction sized
to severity, across all three axes) are the **default**; a scenario's RVP may
supply its own scoring guide instead, which takes precedence. The shipped
`tictactoe/rvp` deliberately carries **no** scoring band of its own — it is a
task rubric only, deferring the arithmetic to `agent_judge.md`.

The verdict lands in three places: `ScenarioResult.score` (+ the full
`EvaluationResult`), `<run_dir>/report.md` (human-readable score + report),
and an `evaluation` note in the main transcript (with `source: tool` or
`parsed_text`). `summary.json` records the score, attempt count, and judge
session id.

**Why a tool, not grammar (as the UPP uses):** the user proxy answers with a
one-shot `llm.complete` that *can* be grammar-constrained (`answers_json_
schema`), so its structured return is guaranteed by construction — no tool
needed there. The judge must be a real session (to read code with tools), and
a whole session turn cannot be grammar-constrained; a terminal tool call is
the equivalent structured-return channel for the agentic side.

Trade-off, made explicitly: judging through a kodo session means the verdict
is *mediated by kodo's own agent stack* (the judge's `judge`-workflow run is
part of the measurement chain). That is what buys tool access to the
workspace ("path to generated code"), and the judge stack is held constant
across scenarios, so comparisons between LUTs stay apples-to-apples.

### 9.3 What phase 3 owns

The UPP and RVP *texts* (persona, answering policy, task rubric), shipped as
`.md` files in the `kodo.validator.prompts` package (§8b) and pulled by name so
one UPP/RVP can back several scenarios — the mechanics deliberately treat both
as opaque strings. The only text phase 2 injects around them is the wire
contract (the JSON shapes) and the run context (roots, prompts, interaction
log).

## 10. Validation suites (`_suite.py`)

A `Scenario` is content-only (§3a) — nothing on it says which LLM(s) run it.
A **`ValidationSuite`** is the thing that says which LLM(s) (+ flavor) exercise
which scenarios, and which model judges all of them, so a batch can validate
**several** LUTs in one invocation and end with one comparative report.

```python
from kodo.validator import LLMUnderTest, SuiteEntry, ValidationSuite

SUITE = ValidationSuite(
    name="my-suite",
    entries=[
        SuiteEntry(llm_under_test=LLMUnderTest(llm="model-a"), scenario=scenario_1),
        SuiteEntry(llm_under_test=LLMUnderTest(llm="model-a"), scenario=scenario_2),
        SuiteEntry(
            llm_under_test=LLMUnderTest(llm="model-b", flavor="model-b-near-greedy"),
            scenario=scenario_1,
        ),
    ],
    judge_llm="judge-model",
    summary_prompt="…",  # instructions for the final round, see §10.2
)
```

- **`LLMUnderTest`** — one LUT identity: `llm` (a local registry name,
  mandatory) + `flavor` (defaults `"default"` — every registry entry ships an
  `id == "default"` flavor, so a suite's runs are deterministic unless a
  preset is named explicitly).
- **`SuiteEntry`** — one `(LLMUnderTest, Scenario)` pair to validate.
- **`ValidationSuite.entries`** is a **flat, explicit list** of entries — not
  an implicit cross product of every LUT against every scenario. In practice
  different scenarios target different LUTs on purpose (a fully-specified task
  for a weak LUT, a deliberately vague one to exercise `ask_user` on a strong
  one, a toolchain-triggering task for a mid-size one, …), so an implicit
  cross join would run nonsensical combinations. The common "these N scenarios
  against this one LUT" case is just a list comprehension in the suite file —
  see `kodo.validator.suites.full_regression`'s `_entries_for` helper.
- **`judge_llm`** (mandatory) / **`judge_llm_flavor`** (optional, default
  `None` = leave as-is) — the model that answers every entry's UPP, judges
  every entry's RVP, *and* produces the final cross-entry summary (§10.2).
  There is exactly one judge per suite, not one per entry.

### 10.1 Execution (`run_suite`)

`run_suite(suite, out_dir, *, template_home=None)` runs `out_dir/<name>-
<timestamp>/`:

1. **Each entry runs through `run_scenario` exactly as a standalone scenario
   would** — its own fresh isolated harness/home/server/workspace
   (`llm_under_test=entry.llm_under_test.llm`,
   `validation_llm=suite.judge_llm`, `flavor=entry.llm_under_test.flavor`,
   `validation_llm_flavor=suite.judge_llm_flavor`), landing under the suite's
   `run_dir`. A suite is "the same isolated runs, just batched and reported
   together" — every LUT gets its own separate workspace, exactly like §3a's
   ordinary per-scenario isolation, entry by entry, never shared.
2. **Only after every entry has finished**, the final summary round (§10.2)
   runs once.
3. `<run_dir>/suite-report.md` (human-readable: every entry's score + the
   judge's comparative summary) and `suite-summary.json` (machine-readable,
   mirroring §7's per-scenario `summary.json`) are written.

### 10.2 The final round: one comparative summary across every LLM

`ValidationSuite.summary_prompt` is **mandatory** — comparing LLMs is a
suite's whole purpose, so this round always runs; there is no opt-out the way
§9's UPP/RVP are opt-in on a plain scenario. `summary_timeout` (default 900s)
and `summary_thinking_level` (optional, a valid tier slug for `judge_llm`'s
thinking family) mirror the per-round timeout/thinking-level knobs §9 already
establishes.

Unlike the RVP judge (§9.2), the summary round is **not** a full agentic
session — no workspace, no tools, no `judge` workflow. Its input is every
entry's *already-generated* report text (score + the RVP judge's write-up),
which is compact and needs no tool-based exploration; a real session would be
unnecessary machinery. Instead it is one session-less `llm.complete` call —
the same primitive the UPP proxy uses (§9.1) — on a lightweight harness whose
only job is getting `judge_llm` serving:

1. A `ValidationHarness` is opened with `llm_under_test=validation_llm=
   suite.judge_llm` and `flavor=suite.judge_llm_flavor` (no workspace roots),
   under `<run_dir>/summary/`.
2. `llm.select(judge_llm)` confirms it is actually serving (mirroring the UPP
   proxy's own switch-then-call sequence, §9.1).
3. `llm.complete(system=summary_prompt, prompt=<every entry's LLM+flavor,
   scenario name, score, and full report text>)` — no `json_schema`; the
   summary is free-form markdown prose, not a structured verdict, so there is
   no tool-call channel to read it off (contrast §9.2's `submit_evaluation`).

The shipped `kodo.validator.prompts` `suite_summary/default` prompt asks for:
an overview, a per-LLM breakdown, a direct cross-LLM comparison on any
scenario more than one LLM ran, cross-cutting patterns, and a grounded
recommendation — see the file for the exact structure. Write your own and pass
it as `summary_prompt` for a different rubric; like the UPP/RVP, the mechanics
treat it as an opaque string.

### 10.3 The suite selector and `hatch run validate-suite`

Curated suites live under `kodo/validator/suites/` and resolve exactly like
`kodo/validator/scenarios/` (§8a) — same one-file-one-`SUITE`(-or-`SUITES`)
convention, same dotted/submodule/`all` selector grammar
(`kodo.validator.suites.resolve_selectors`), same file-path loading (not
import) so directory names need not be valid identifiers:

```bash
hatch run validate-suite full_regression   # one suite
hatch run validate-suite all               # every suite
hatch run validate-suite --list            # show available suites
```

Unlike `hatch run validate` (§8a), there are **no** `--llm-under-test`/
`--validation-llm` flags here — a suite already carries every LUT (+ flavor)
and judge it needs. The runner resolves every selector first, disk-checks the
template home for the union of every suite's LUTs + judge (logged, not fatal,
same as §8a), then runs each suite in turn. Artifacts land under
`~/.kodo-validation/runs` by default (`--out` overrides).

`kodo.validator.suites.full_regression` is the shipped suite: it re-pairs
every scenario under `kodo.validator.scenarios` with the LLM-under-test that
family was originally built for (via `kodo.validator.scenarios.
resolve_selectors`, since scenario content is loaded by suites the same way
`hatch run validate` loads it), judged throughout by
`unsloth-qwen36-27b-q8-k-xl`. Running it reproduces the pre-suite per-family
LUT pinning exactly, batched into one run with one final comparative summary.
