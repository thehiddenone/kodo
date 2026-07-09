# Kodo — Automated Validation Harness (`kodo.validator`)

> Status: **phase 1 — capabilities**. The harness runs real kodo sessions
> end-to-end and records everything; the **evaluation/scoring layer (phase 2)
> is not built yet** — `ScenarioResult.score` is always `None` today.
> Reference: [WS_PROTOCOL.md](WS_PROTOCOL.md), [SESSIONS.md](SESSIONS.md),
> [SECURITY.md](SECURITY.md), [SETTINGS.md](SETTINGS.md).

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
  by design.)
- **skipped**: `sessions/`, `logs/`, the `kodo-server` discovery file —
  per-run state starts fresh, and a stale discovery PID can't confuse the
  spawned server.
- **copied**: everything else — `etc/settings.json`, the local-LLM registry, …
  so a run can mutate its settings freely.

`settings_overrides` (harness/scenario arg) deep-merges into the cloned
`etc/settings.json` before the server starts — e.g. pin `mode`/`models` for a
particular validation matrix. Cloud API keys are *not* in the home (the real
extension keeps them in VS Code SecretStorage); see §6.

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
frame kind), plus `note` entries: `lifecycle` (connect/modes/shutdown),
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
  --root app=/path/to/seed-project --root lib \
  --workflow problem_solving --command-control permissive \
  --prompt "Find and fix the failing test" \
  --out kodo-validator-runs

# scenario file: defines SCENARIO or SCENARIOS (can carry ScriptedUser scripts)
uv run kodo-validator --scenario suites/smoke.py
```

`python -m kodo.validator` is equivalent. Exit code 0 iff every scenario
completed with no `error`-phase turn. Each scenario gets a fresh home + server.

## 9. Phase 2 (planned)

The evaluator plugs into `ScenarioResult.score`: read `transcript.jsonl` /
`summary.json` (plus the workspace tree the run left behind) and produce a
0–100 score per scenario — LLM-judged and/or programmatic assertions. Nothing
in phase 1 needs to change shape for that: scoring is a pure function of the
run directory.
