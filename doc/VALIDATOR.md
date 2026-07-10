# Kodo — Automated Validation Harness (`kodo.validator`)

> Status: **phase 1 — capabilities**. The harness runs real kodo sessions
> end-to-end and records everything; the **evaluation/scoring layer (phase 2)
> is not built yet** — `ScenarioResult.score` is always `None` today.
> Reference: [WS_PROTOCOL.md](WS_PROTOCOL.md), [SESSIONS.md](SESSIONS.md),
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
  _transcript.py  Transcript          — every frame + interaction, JSONL
  _harness.py     ValidationHarness   — one run, end to end; Modes; TurnResult
  _scenario.py    Scenario / run_scenario / ScenarioResult (score=None)
  __main__.py     python -m kodo.validator / kodo-validator CLI
```

Every run owns one directory:

```
<run_dir>/home/.kodo                isolated kodo home (see §3)
<run_dir>/home/server-console.log   server subprocess stdout+stderr
<run_dir>/workspace/<root>/…        simulated workspace folders
<run_dir>/transcript.jsonl          every frame + interaction, in order
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
- **`validation_llm`** — a fixed, capable model reserved for the phase-2
  evaluator (§9). Phase 1 only guarantees it's installed; nothing calls it
  yet.

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
  --out kodo-validator-runs

# scenario file: defines SCENARIO or SCENARIOS (Scenario carries
# llm_under_test/validation_llm itself; can also carry ScriptedUser scripts)
uv run kodo-validator --scenario suites/smoke.py
```

`--llm-under-test`/`--validation-llm` are mandatory for ad-hoc runs (§3a); a
scenario file sets them directly on its `Scenario` instead.

`python -m kodo.validator` is equivalent. Exit code 0 iff every scenario
completed with no `error`-phase turn. Each scenario gets a fresh home + server.

## 9. Phase 2 (planned)

The evaluator plugs into `ScenarioResult.score`: read `transcript.jsonl` /
`summary.json` (plus the workspace tree the run left behind) and produce a
0–100 score per scenario — LLM-judged and/or programmatic assertions. Nothing
in phase 1 needs to change shape for that: scoring is a pure function of the
run directory.
