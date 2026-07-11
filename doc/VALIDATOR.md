# Kodo — Automated Validation Harness (`kodo.validator`)

> Status: **phase 2 — evaluation mechanics**. The harness runs real kodo
> sessions end-to-end, can answer the agent's questions with the **validation
> LLM** (User Proxy Prompt), and can score the finished run with a judge
> session (Result Validation Prompt) into `ScenarioResult.score` +
> `report.md` — see §9. **Prompt content (phase 3) is not written yet**: the
> UPP/RVP texts are caller-supplied; without them, phase-1 behaviour
> (scripted answers, `score=None`) is unchanged.
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
  _scenario.py    Scenario / run_scenario / ScenarioResult
  __main__.py     python -m kodo.validator / kodo-validator CLI
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

## 3. Isolated home (`clone_kodo_home`)

The server is a machine-wide singleton rooted at `$HOME/.kodo`
(`kodo.project.kodo_user_dir`). The harness therefore spawns the server with
`HOME`/`USERPROFILE` pointed at `<run_dir>/home`, and populates
`<run_dir>/home/.kodo` from a **template** kodo home (typically the real
`~/.kodo`, `--template-home` on the CLI):

- **symlinked**: `bin/`, `llama.cpp/` — llama.cpp builds and GGUF models;
  local inference works without copying tens of GB. (A download during a run
  writes through the symlink into the template's `llama.cpp/models` — accepted
  by design.) Also `titler/` — the session-titler's cached summarization
  model (`kodo.titling`, doc/INTERNALS.md §10c), for the same reason: without
  this, every run would redownload it on first titling call.
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

Every `Scenario`/`ValidationHarness` requires two **local registry names**
(`kodo/doc/LLM_REGISTRY.md`), both mandatory — there is no default:

- **`llm_under_test`** — the model this run actually exercises. `start()`
  forces it onto the cloned `etc/settings.json` (`mode: "local"`,
  `models.local: llm_under_test`) *on top of* any `settings_overrides`, so a
  run always drives the model it claims to, regardless of what else a
  scenario pins.
- **`validation_llm`** — a fixed, capable model that answers the LUT's
  questions (User Proxy, §9.1) and judges the finished run (§9.2). Installed
  up-front either way, so runs that enable neither still leave it ready.

Both are always local (GGUF/llama.cpp) models — cloud models are API-based
and have no "download" step, so they're out of scope for this mandatory pair.

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
3. `bind_project(root_name)` — `project.set`, required before Guided runs.
4. `submit_prompt(text)` — `prompt.submit`, then block until the turn ends;
   returns a `TurnResult` (final phase, assistant text, tool calls with
   prep+detail merged, interactions, errors, raw entries).
5. `shutdown()` — close WS, SIGTERM the server, close the transcript.

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
| `prompt.question` (ask_user / escalate_blocker) | scripted per-batch answers if provided, else first option / free-text fallback |
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
  --root app=/path/to/seed-project --root lib \
  --workflow problem_solving --command-control permissive \
  --prompt "Find and fix the failing test" \
  --upp-file suites/prompts/upp.md \
  --rvp-file suites/prompts/rvp.md \
  --out kodo-validator-runs

# scenario file: defines SCENARIO or SCENARIOS (Scenario carries
# llm_under_test/validation_llm itself; can also carry ScriptedUser scripts)
uv run kodo-validator --scenario suites/smoke.py
```

`--llm-under-test`/`--validation-llm` are mandatory for ad-hoc runs (§3a); a
scenario file sets them directly on its `Scenario` instead.
`--upp-file`/`--rvp-file` (optional, independent) enable the §9 machinery for
ad-hoc runs; a scenario file sets `user_proxy_prompt`/`result_validation_prompt`
directly.

`python -m kodo.validator` is equivalent. Exit code 0 iff every scenario
completed with no `error`-phase turn. Each scenario gets a fresh home + server.

## 8a. The scenario suite and `hatch run validate`

For a curated suite (as opposed to ad-hoc `--scenario FILE` runs), scenarios
live **in the package** under `kodo/validator/scenarios/` and run through a
build recipe:

```bash
hatch run validate qwen35-9b.tictactoe_console   # one scenario
hatch run validate qwen35-9b                      # every scenario under qwen35-9b/
hatch run validate all                            # the whole suite
hatch run validate --list                         # show available scenarios
```

**One scenario == one `.py` file** that defines a module-level `SCENARIO`
(a `Scenario`), with its PUT / UPP / RVP inlined as triple-quoted strings so the
file is self-contained (no sidecar prompt files). A file may define `SCENARIOS`
(a list) instead. Files are grouped in sub-directories — typically by the model
under test — and a directory name need not be a valid Python identifier
(`qwen35-9b/`), because scenarios are loaded by **file path**, not imported.

A command-line **selector** (`kodo.validator.scenarios.resolve_selectors`) is a
dotted path under the package: `qwen35-9b.tictactoe_console` →
`qwen35-9b/tictactoe_console.py`; a bare `qwen35-9b` → every scenario under that
directory; `all` → the whole tree. Selectors are resolved and de-duplicated
into the full batch **before anything runs**.

**Pre-flight model note (no fail-fast).** Once the batch is known, the runner
computes the union of every scenario's `llm_under_test` / `validation_llm` and
disk-checks the template home (`~/.kodo`) with `missing_local_llms(kodo_dir,
names)` (reads `manager-state.json`, no server). Any not-yet-installed model is
**logged, not fatal** — the per-run harness (`ensure_local_llms_installed`)
downloads it into the global `~/.kodo` (through the clone's `llama.cpp` symlink)
before that scenario prompts. Artifacts land under `~/.kodo-validation/runs` by
default (`--out` overrides). Entry point: `python -m kodo.validator.scenarios`.

**HuggingFace cache across runs.** Every run's server child has `HOME`
redirected to its throwaway home, which would otherwise push HuggingFace's
default cache under that home. `ServerProcess` (`build_child_env`) instead pins
`HF_HOME` to the **real, global** cache and sets `KODO_TITLER_LOCAL_FILES_ONLY=1`
so the session titler loads its already-cached summarization model (shared
globally via the symlinked `~/.kodo/titler`) **without** HEAD-ing the Hub each
run. That flag is a titler-only, per-call `local_files_only` — *not* a global
offline switch — so GGUF downloads, which resolve metadata through
`huggingface_hub`, are unaffected.

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
- **`llm.complete {prompt, system?, json_schema?}`** — session-less one-shot
  completion on the active local model, scheduled through the shared
  LLMGateway feed; `json_schema` grammar-constrains the output so the reply
  is parseable **by construction**.

### 9.1 User Proxy: questions answered by the VLLM (`_vllm.py`)

When the LUT calls `ask_user`, the engine's `fire_questions` awaits the
client's answer **with no timeout** (`runtime/_gates.py`) — the dangling tool
call is what makes the swap safe. `VLLMUserProxy.answer_questions` then runs:

1. `llm.select(validation_llm)` — wait until the VLLM is serving;
2. `llm.complete(system=UPP, prompt=PUT + question batch + wire contract,
   json_schema=answers_json_schema(n))` — the schema pins exactly *n*
   entries of `{selected: [str], free_text: str}`;
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
   `window_id: kodo-validator-judge`, own `judge-transcript.jsonl`), push the
   **same** `workspace.folders` payload, and pin friction-free modes
   (autonomous, problem_solving, allow_all/permissive — its gates are
   answered by a plain `ScriptedUser`);
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
   impact tool the `problem_solver` agent declares, `kodo.toolspecs.
   SUBMIT_EVALUATION`), and `_verdict_from_tool_calls` reads the values off
   that call's `agent.tool_call_detail` rows — structured, no text parsing.
   `_parse_score` (fenced / bare / embedded JSON) stays as a **fallback** for
   a judge that answers in prose anyway; a turn that yields neither gets a
   follow-up turn asking for the verdict again (default 3 attempts), then
   `EvaluationError`.

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
is *mediated by kodo's own agent stack* (the judge's Problem-Solver run is
part of the measurement chain). That is what buys tool access to the
workspace ("path to generated code"), and the judge stack is held constant
across scenarios, so comparisons between LUTs stay apples-to-apples.

### 9.3 What phase 3 owns

The UPP and RVP *texts* (persona, answering policy, scoring rubric), shipped
as suite files next to scenario definitions — the mechanics deliberately
treat both as opaque strings. The only text phase 2 injects around them is
the wire contract (the JSON shapes) and the run context (roots, prompts,
interaction log).
