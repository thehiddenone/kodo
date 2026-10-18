# Benchmark sessions

> Status: **built 2026-10-18, not yet run for real.** Steps 1–4 of §8 landed:
> Part A (§3), the exclusive lease (§4.6), the server side of Part C (§4; §4.7
> lists what was built differently) and kodo-vsix (§5). Step 5, the manual
> runs with Docker (macOS local and cloud, Stop, and Stop on Windows), is
> still to do. Every decision in §1 was made by the user when asked. Supersedes [AGENT_BUILDER.md](AGENT_BUILDER.md) D19 (run
> in a terminal, no server-side job management) and D20 (GPU warning) once
> built. Related: [HARBOR.md](HARBOR.md) (`kodo-harbor`),
> [HEADLESS.md](HEADLESS.md) (`--llama-url`), [LLM_GATEWAY.md](LLM_GATEWAY.md)
> (the llama-server lease), [SESSION_LLM_BINDING.md](SESSION_LLM_BINDING.md) §3,
> [WS_PROTOCOL.md](WS_PROTOCOL.md) §7.4h (`agent.run`, the model for the lock).

Today the Kōdo Settings panel's **Basics test** and **Benchmark…** actions open
a VS Code integrated terminal and run `kodo-harbor run …` there
(`kodo-vsix/src/extension/benchmark.ts`). For a local model, `kodo-harbor`
starts its **own** `kodo-llama-server` on port 8090, so the GGUF can be loaded
twice: once by the Kōdo server for chat and once for the benchmark. The modal
can only warn about that (D20).

After this change, a run opens a **Kōdo session tab**. The server runs
`kodo-harbor` itself, on the server's **own** llama-server (the singleton on
port 8042). While the run holds that llama-server, nothing else uses it.

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Approach | **Attach mode in `kodo-harbor` (§3), then a server-run job shown in a session tab (§4).** |
| D2 | Local chat during a local run | **Waits for the run.** Every local request waits until the benchmark ends, even one for the same model and command line (§4.6). |
| D3 | Linux, where containers can't reach `127.0.0.1` | **Fall back to standalone.** The run still shows in the session tab, but `kodo-harbor` starts its own `kodo-llama-server` on the docker0 address, as it does today. The server's llama-server is never exposed on the bridge. |
| D4 | Linux memory clash | **Hold the lease, then unload.** The job takes the same exclusive lease and stops the server's llama-server before `kodo-harbor` starts its own. Local chat waits, as on macOS and Windows. |
| D5 | VS Code closes during a run | **Stop the job.** Idle shutdown stops it as Stop would (§4.5); the summary covers the trials that finished; then the server exits. |
| D6 | The terminal path | **Removed.** `benchmark.ts`'s terminal launcher and the D20 warning go away. `kodo-harbor` stays a CLI for manual use. |
| D7 | Cloud models | **Same session tab.** The server gets the key with `api_key.request` (WS_PROTOCOL §6.3) and puts it only in `kodo-harbor`'s environment. D37's rule (never on a command line, never in a file) moves to the server. |
| D8 | What the tab shows | **Progress, then the summary.** The server reads the job directory and shows a finished/total line and one row per finished trial (reward, time). At the end it shows `kodo-summary.md`. The raw output goes to a log file, which the tab links. |
| D9 | Persistence | **A real session.** It's in the session list and reopens like an `agent.run` session; reopening replays the progress and the summary. If the server restarts mid-run, the session is marked **interrupted**. It's never resumed. |
| D10 | Which llama-server configuration | **The entry's stored configuration**: the profile and knobs stored for that card in the registry. That's the command line an unbound session would bind to, and what `kodo-llama-server` uses today. |
| D11 | Concurrent runs | **One at a time, server-wide**, local or cloud. A second request is refused with an error that names the running session. |

## 2. Feasibility (what exists, what's missing)

**Already works:**

- **Container side.** `kodo-headless --llama-url` attaches to an llama-server
  without an alias. `RemoteLlamaEndpoint` matches the GGUF by file name (an
  unaliased server's `/v1/models` id is its full path) and checks the context
  per slot against the registry window.
- **The same flags either way.** The server's managed llama-server and
  `kodo-llama-server` both get their flags from `resolve_llama_launch`. A trial
  on the singleton runs the same model configuration as on a standalone server.
- **A lock on the singleton.** `LLMGateway.acquire_local(holder, argv)` /
  `release_local(holder)` (`llms/_gateway.py`, `LlamaLease`). The sidebar's
  `llama.start` already uses it, and `llama.stop` is refused while it's held.
- **A locked session tab.** `agent.run` sessions take no input and show a
  status strip with Stop and Delete (kodo-vsix `App.tsx`, `state.interactive`).
- **Keys from VS Code.** `api_key.request` already brings a stored key to the
  server over the session channel, held in memory only.
- **Reading results.** `JobSummary.load(job_dir)` (`harbor/_summary.py`)
  reads `*/result.json`, the same data a progress view needs.

**Missing**, each covered below:

1. `kodo-harbor` can't attach. `HostLlama` (`harbor/_llama.py`) only starts or
   reuses a standalone server (§3).
2. The managed server listens only on `127.0.0.1:8042`, so Linux containers
   can't reach it (D3).
3. Idle shutdown: the server exits 5 s after its last client
   (`server/__main__.py`, `_IDLE_SHUTDOWN_SECONDS`) and stops llama-server
   with it (D5, §4.5).
4. The lease lets holders share a command line, and llama-server runs one
   slot (`--parallel 1` unless a profile sets it, LLM_REGISTRY §4.6). Without
   an exclusive hold, chat on the same model would queue between trial
   requests, evict their prompt cache and skew their timings (D2, §4.6).
5. A session tab only shows agent conversations. There's no job session, and
   no event that streams a subprocess (§4).

## 3. Part A — `kodo-harbor run --llama-url`

A new flag in the `local` group of `run`:

```bash
kodo-harbor run --model ENTRY --llama-url http://127.0.0.1:8042 …
```

- **Attaches; never starts or stops.** It replaces `HostLlama`'s
  start/reuse/stop and can't be combined with `--llama-port`, `--llama-bind`
  or `--keep-llama` (argparse error). It requires a local `--model`.
- **Checks what it's attaching to** before the job directory exists, through
  the same check `kodo-headless` makes (`RemoteLlamaEndpoint`'s GGUF basename
  match). A server serving another model is an error naming both models.
- **`LlamaAccess` stops assuming kodo-harbor started the server.** It takes
  the host URL as given (for the `terminus-2` control arm), builds the
  container URL `http://host.docker.internal:<port>` from it, and reads the
  context per slot from `/props`, as `HostLlama` does now.
- **Refuses a loopback URL on Linux**, because containers can't reach it:
  `"--llama-url … is a loopback address, which containers cannot reach on
  Linux; omit --llama-url to start a standalone server"`. The server never
  passes one there (D3), so this only guards a manual CLI run.
- **The control arm needs no alias** (checked 2026-10-17 against the managed
  server, llama.cpp b11552). `terminus-2` sends the model name
  `openai/<entry>`, and a standalone server has `--alias <entry>` from
  `kodo-llama-server`, but the managed server has none. That doesn't matter:
  with one model loaded, llama-server answers 200 from that model for the
  entry name, for `openai/<entry>` and for a name that doesn't exist. So the
  managed server needs no `--alias`.

Part A is usable on its own from the CLI, and §4 depends on it.

**Built 2026-10-18.** `LlamaAccess(entry, host_url, context_per_slot)` takes
the URL; `LlamaAccess.attach(entry, url, kodo_dir)` makes the check by running
`RemoteLlamaEndpoint.verify` once (restoring the process's previous endpoint
afterwards). `kodo-harbor` also installs a `SIGBREAK` handler on Windows that
raises `KeyboardInterrupt` (§4.4), so Stop has the Ctrl+C path to use.
`--llama-port` lost its argparse default (8090 is applied in `_run`) so the
exclusion check can tell a given flag from a default.

## 4. Part C — the benchmark session

### 4.1 Starting one

The Benchmark modal keeps its fields (model, suite, attempts, thinking level,
control) and loses the GPU warning (D6). **Run** opens a new session tab with
`hello greet:false`, as `agent.run` does, and sends a new session-connection
message:

```json
{ "type": "harbor.run", "agent": "kodo_problem_solver", "model": "unsloth-qwen38-27b-ud-q4-k-xl",
  "suite": "agent-basics", "attempts": 1, "control": false, "thinking_level": null }
```

It works only on a **brand-new session**. The response is
`harbor.run.accepted`, preceded by a `state` with `interactive: false` and a
new `job` field (§4.3). Refusals change nothing and answer `error`
`harbor_run_refused` with a message:

- the session already has a conversation, a queued prompt or a job
- another benchmark is running (D11); the message names that session's title
- the model, suite or thinking level fails the checks `kodo-harbor` makes
  before creating a job directory (the same `BenchModel.resolve` /
  `check_thinking_level` code)

The session's title is set at once, with no titler involved: `Basics test:
<agent> · <model>` or `Benchmark: <agent> · <model>`.

### 4.2 The job

A new server-side class (`HarborJob`, in `kodo.server`, which already imports
`kodo.harbor`) runs one job per benchmark session:

1. **Local, macOS/Windows:** take the **exclusive** lease (§4.6) for holder
   `harbor:<session_id>` on the entry's planned command line (D10, the stored
   profile and knobs). Then `ensure_llama_running` starts or reuses the
   managed server. The URL passed on is `http://127.0.0.1:<port>`.
   **Local, Linux:** take the same exclusive lease and stop the managed
   llama-server (D4). No `--llama-url` is passed.
   **Cloud:** no lease. Fetch the key with `api_key.request` and put it in
   the child's environment under the vendor's `credential_env[0]` (Bedrock
   also gets `AWS_REGION`), unless the server's environment already has one.
2. **Spawn** `sys.executable -m kodo.harbor run --agent … --model … --suite …
   --attempts … [--control terminus-2] [--thinking-level …] --jobs-dir
   ~/.kodo/harbor/jobs --job-name <agent>-<timestamp> [--llama-url URL]` in
   its own process group (POSIX `start_new_session`; Windows
   `CREATE_NEW_PROCESS_GROUP`). stdout and stderr go to
   `<session dir>/harbor-output.log`, never to the wire. They go in the session
   directory, not the job directory, because `kodo-harbor` creates the job
   directory and refuses one that already holds a `config.json`.
3. **Watch** the job directory every 2 s with `JobSummary.load`. Each newly
   finished trial is pushed as `harbor.trial` and persisted.
4. **On exit**, read `<job>/kodo-summary.md`, push `harbor.finished`, persist
   it, and release the lease **in a `finally`**, so a crash or a cancel can't
   leave the lease held.

If `kodo-harbor` exits before writing a job directory (a preflight failure:
no Docker, no `uv`, a missing agents directory), `harbor.finished` carries
`outcome: "failed"` and the last 40 lines of the log as `detail`, so the tab
shows the reason without opening the log.

### 4.3 Wire and persistence

| Name | Kind | Payload |
|---|---|---|
| `harbor.run` / `harbor.run.accepted` | session command (§7.4j) | §4.1 |
| `state.job` | field on `state` (§5.1) | `null`, or `{status, kind:"basics"\|"benchmark", agent, model, suite, attempts, control, job_dir, log_path, started_at}` with `status` one of `waiting` · `running` · `stopping` · `completed` · `failed` · `stopped` · `interrupted` |
| `harbor.trial` | event | `{task, arm, reward, seconds, exceptions, finished, total}`; `total` is `null` while it's unknown |
| `harbor.finished` | event | `{outcome, exit_code, summary_markdown, detail, job_dir, log_path}` |
| `llm.waiting` | existing event | new `reason: "benchmark"` with the job session's title (§4.6) |

`transient.json` gains `harbor_job` (the request, the job directory, the
child's pid, the status). The lock is derived from it, not from `top_agent`,
so a reload or a restart keeps the session locked. `session.jsonl` gets the
markers `harbor_job`, `harbor_trial` and `harbor_finished`, and
`session.history` replays them (D9).

The **finished/total** count comes from Harbor's job-level `result.json`.
Harbor 0.23 writes it, with `n_total_trials`, before the first trial starts
(`Job.run` → `_write_job_result`, checked in its source and in the 2026-10-16
`agent-basics` job). Until that file exists, the line reads "starting".

### 4.4 Stop

`stop` on a benchmark session interrupts the run the way Ctrl+C in a terminal
does: Harbor winds its trials down and the summary covers what finished.

**As built (2026-10-18):** the server **closes `kodo-harbor`'s stdin**. The
server starts `kodo-harbor` with the hidden flag `--stop-on-stdin-close`, and a
thread in `kodo-harbor` waits for stdin to close, then interrupts its own
process group:

- **POSIX:** `kodo-harbor` runs in its own session (`start_new_session`), and
  sends `SIGINT` to its own group (`os.killpg(os.getpgrp(), SIGINT)`).
- **Windows:** `kodo-harbor` runs on its own **hidden console**
  (`CREATE_NEW_CONSOLE`, `SW_HIDE`), and calls
  `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)`, which reaches every
  process on that console and never the server. `kodo-harbor`'s `SIGBREAK`
  handler raises `KeyboardInterrupt`.

The first design, in which the server sends the signal itself, doesn't work on
Windows: a server VS Code started has no console, and a process without a
console can't send `CTRL_BREAK_EVENT`. Closing stdin also means a server that
crashes stops its run cleanly instead of orphaning it.

**Harbor on Windows (decided by the user 2026-10-18).** Python installs no
`SIGBREAK` handler, so Harbor would exit at once on `CTRL_BREAK_EVENT`. Moving
it to its own process group, the first option, wouldn't help: it would still
get only `CTRL_BREAK_EVENT`. So on Windows `kodo-harbor` starts Harbor
through a shim: `uv tool run --from harbor==0.23.0 --with … python -c
"<shim>" run -c …`, where the shim maps `SIGBREAK` to `KeyboardInterrupt` and
then runs `harbor.cli.main:app` (checked on macOS that `--from harbor python`
runs Harbor's CLI). Harbor then takes its Ctrl+C path. That `uv` itself
ignores `CTRL_BREAK_EVENT` while its child runs is **unverified**; it's part of
the Windows manual check (§8 step 5).

**`kodo-harbor` now waits for Harbor after an interrupt.** It used
`subprocess.run`, which kills its child 0.25 s after Ctrl+C, so `kodo-harbor`
killed `uv` and summarized while Harbor was still winding down, orphaned.
`HarborInvocation.run` now waits for Harbor after the first interrupt, then
re-raises it; a second interrupt kills it. This also fixes the terminal path.

The status becomes `stopping`. After a 120 s grace period the whole process
tree is killed (`psutil`, children first), and the outcome is `stopped` either
way. Containers Harbor didn't get to remove after a kill are a known limit (§6).

### 4.5 Idle shutdown, `server.shutdown`, restart

- **Idle shutdown (D5)** and `server.shutdown` stop a running job as §4.4
  does, wait for it to exit (bounded by the same grace period), and only then
  stop llama-server and exit. A window reload reconnects within the 5 s grace
  period and doesn't stop the job.
- **Server restart.** If a server starts and finds a session whose
  `harbor_job` was never finished, it marks that session `interrupted`. If the
  recorded pid is still alive, it kills that process group first. Liveness
  goes through the existing public pid helpers, never `os.kill(pid, 0)` on
  Windows. Nothing is resumed.
  **As built:** the record keeps `process: {pid, created}` (psutil's start
  time), and the tree is killed only when both match, so a reused pid is never
  killed. `psutil` does the liveness check on every platform.

### 4.6 The exclusive lease (D2)

`LlamaLease.acquire` gains `exclusive: bool = False`, and
`LLMGateway.acquire_local` passes it through:

- An exclusive request waits in the same FIFO until **every** holder has
  released. Running turns finish; none is broken (SESSION_LLM_BINDING D4).
- While an exclusive holder holds the lease, **no one** is admitted, not even
  on the same command line. Requests that arrive in the meantime queue behind
  it in order and are admitted when it releases.
- The gateway reports the wait as `llm.waiting {reason: "benchmark"}`, which
  the webview shows as `Waiting for the benchmark in <title> to finish`. It
  replaces `model_swap`, which would be misleading here.
- The sidebar's `llama.start` waits the same way. `llama.stop` is already
  refused while the lease is held.

**Built 2026-10-18** (`LlamaLease.acquire(..., exclusive=, label=)`,
`LLMGateway.acquire_local(..., exclusive=, label=)`, `exclusive_label`). The
`llm.waiting` payload carries the title as `title`. The exclusive request's own
wait reports `model_swap`, never `benchmark`. A cancelled waiter now lets the
same-command waiters at the front of the queue join the current holders.

**Other paths that stop the managed llama-server** and ignore the lease
(found 2026-10-18; §4.2 must guard each while a job holds it):

- `llamacpp.update` and `llamacpp.uninstall` (`server/_app.py`) stop it
  unconditionally. They must be refused while a job runs.
- The GPU-release hook (`_release_llama_gpu`, armed in
  `ConnectionRegistry`) stops it as soon as the last client disconnects,
  with no grace period, when `SessionManager.any_running()` is false. A
  window reload during a run would kill the trials' server. The hook, and
  the idle self-reap's deferral, must count a running job as running.
- A `llama.start` for a `custom_server_url` entry stops the managed server
  when `local_holders` is empty. It already sees the job's hold.

The housekeeper LLM (titles, greetings) runs on its own llama-server
(`titling/_server.py`) and isn't affected. Self-hosted cards and cloud models
never touch the lease, so they keep working during a run.

### 4.7 As built (2026-10-18)

The server side is `kodo.server._harbor_job` (`HarborRequest`, `HarborJob`,
`HarborJobs`, `ManagedLlama`) and these changes elsewhere:

- **Engine:** `WorkflowEngine.handle_job_start` / `update_job` /
  `record_job_event`; `SessionState.job`; `TransientStore.harbor_job` (opaque,
  like `llm_selection`). `state.job` leaves out the `process` entry. On
  resume, a stored `harbor_job` sets `interactive: false`.
- **History:** `harbor_*` markers replay verbatim, with the live event's own
  snake_case fields, not camelCase, so the client can use one code path for
  live and replayed entries.
- **Outcome rule:** `stopped` if a Stop or delete was requested; `completed` if
  `kodo-summary.md` exists, the exit code isn't `2` and nothing failed before
  `kodo-harbor` started; else `failed` with `detail`.
- **Progress:** `JobSummary.trial_rows()` and `JobSummary.total_trials()`.
  Harbor writes a trial's `result.json` only when the trial ends
  (`Trial._finalize`, checked in Harbor 0.23's source), so a present file is a
  finished trial; a half-written one fails to parse and is read on the next
  poll.
- **Departure: `llama.start` is refused during a run, not queued** (§4.6 said
  it waits). Handlers run one at a time per connection, so a `llama.start`
  waiting for the whole run would stall every request on the sidebar's
  connection for hours.
- **Guards (the hazards in §4.6):** `llamacpp.update` and `llamacpp.uninstall`
  are refused while a run is active, on every platform (on Linux the run's own
  `kodo-llama-server` uses the same binaries). The GPU-release hook now does
  nothing while anyone holds the llama-server lease. The idle self-reap needs
  no change: with no window for 5 s it shuts down, and `_stop_background`
  stops the run first (D5).
- **Bug found while testing:** a daemon thread blocked in
  `sys.stdin.buffer.read()` holds the buffered reader's lock, and Python then
  aborts at exit (`SIGABRT`, "could not acquire lock for <stdin> at
  interpreter shutdown"). The stdin watcher reads the raw descriptor
  (`os.read`) instead.
- **Tests:** `test/test_harbor_jobs.py` (a stand-in `kodo-harbor` script),
  WebSocket tests in `test/test_server_integration.py`, and an end-to-end
  POSIX test of the real `kodo-harbor --stop-on-stdin-close` with a fake `uv`
  that traps `SIGINT` (`test/test_harbor_host.py`).

## 5. Client (kodo-vsix)

- **Modal.** `BenchmarkModal.tsx` drops the GPU warning. Run posts the
  request, and the host opens a new session with it (`newSession` gains a
  `benchmark` request beside `AgentRunRequest`).
- **Removed** (D6): the terminal launcher in `src/extension/benchmark.ts`
  (`runBenchmark`, `credentialEnv`, `quoteCommandLine`, the shell-integration
  wait). `fetchHarborOptionsForPanel` stays. Its D37 widening of `credential`
  stays too, because the server's key still comes from the same stored keys.
- **Tab.** A locked tab, like `agent.run`, whose strip reads "A benchmark is
  running in this session — it takes no input", with Stop and Delete. The body
  is a job view in place of the transcript:
  - a header (agent, model, suite, attempts, control)
  - the status and the finished/total line
  - a trials table that grows with each `harbor.trial`
  - `summary_markdown` rendered when the run finishes
  - links that open the job directory and `harbor-output.log`
- **Waiting text** for `reason: "benchmark"` in every other session tab.

**As built (2026-10-18):**

- **Launch.** `newSession(launch?: SessionLaunch)`, where a `SessionLaunch` is
  `{kind: 'agent_run', run}` or `{kind: 'benchmark', run: BenchmarkRunRequest}`.
  The controller sends `hello` `greet: false`, locks the composer at once
  (`ModeToggleController.lockForJob`), then sends `harbor.run`. There is no
  llama.cpp launch confirmation, since the user picked the model in the modal.
  A `harbor_run_refused` reply shows an error and turns the tab back into an
  ordinary blank session (`applyNewSessionDefaults` now also resets
  `interactive`).
- **State.** `src/benchmark-view.ts` is shared by the host and the webview, with
  no `vscode` import. `BenchmarkProgress` folds `state.job`, `harbor.trial`
  (deduplicated by trial name), `harbor.finished` and `session.history`.
  History is authoritative, but trials pushed after it are kept. The controller
  posts one `{type: 'benchmark', view}` snapshot whenever it changes, and again
  on rehydrate. The webview reducer just stores it, and the `harbor_*` history
  entries never reach the chat feed.
- **Tab.** `BenchmarkPanel.tsx` shows the header, the status line
  (`benchmarkStatusLine`), the trials table (task, arm, reward, agent time,
  errors), the failure `detail`, the summary rendered as Markdown, and
  **Open the run's log** (`open_path`) and **Show the job directory**
  (`reveal_path` → `revealFileInOS`). Stop is enabled while the status is
  `waiting` or `running`.
- **Not in the design:** when a run ends and its tab isn't in front, a VS Code
  notification says so, with **Show**. The terminal path used to open the
  summary at the end, and a run can take hours. A local run also pushes
  `llama.state` from the server as it loads the model, so the sidebar's
  llama indicator stays right.
- **Removed:** `runBenchmark`, `benchmarkArgs`, `benchmarkJobName`,
  `credentialEnv`, `quoteCommandLine` and their tests. The Actions… menu's
  Benchmark entry now says "in a new session tab", and the modal's button is
  **Run**.

## 6. Known limits

- A local run blocks every local chat for its whole duration, possibly hours
  (D2). Cloud and self-hosted chat keep working.
- Killing the process group after the grace period can leave task
  containers behind. `docker ps` shows them; Harbor's names are prefixed by
  the job name.
- Harbor's own live progress display is lost. Its output is only in
  `harbor-output.log`.
- On Linux the model is still loaded into a second process (D3), but never
  alongside the server's copy (D4).
- Docker and Harbor already work on the development Mac (Docker 29.8,
  `~/.docker/bin/docker`). The `agent-basics` suite ran there on 2026-10-16:
  `kodo_problem_solver` on `bartowski-ornith15-35b-a3b-bf16`, one attempt, 7/7
  steps, each step with its own `kodo.jsonl` and `trajectory.json`. What has
  never run is this design, so the manual runs in §8 step 5 are its first
  real test.

## 7. Docs to update when built

AGENT_BUILDER.md §8 (D19/D20 superseded, pointer here) · HARBOR.md
(`--llama-url`) · WS_PROTOCOL.md (§5.1 `state.job`, new events, §7.4j,
`llm.waiting` reason) · LLM_GATEWAY.md (exclusive lease, waiting indicator) ·
SESSION_LLM_BINDING.md §3 (pointer) · SESSIONS.md (job sessions, markers) ·
INTERNALS.md (new server module).

## 8. Build order and tests

1. **A — attach mode.** CLI flag, mutual exclusion, attach check, Linux
   loopback refusal, `LlamaAccess` from a URL; tests with a stand-in HTTP
   server for `/v1/models` and `/props`. No `--alias` change
   needed (§3).
2. **Exclusive lease.** `LlamaLease` unit tests: an exclusive request waits
   for shared holders; a same-command request waits behind and during an
   exclusive hold; FIFO order after release; cancellation while waiting.
3. **`HarborJob` + `harbor.run`.** Tests with a stand-in `kodo-harbor` (a
   Python script that writes `result.json` files and a summary, honours
   SIGINT/SIGBREAK, and exits with chosen codes): refusals, progress events,
   persistence and replay, Stop and the kill fallback, idle-shutdown stop,
   restart → interrupted, lease released on every exit path, key only in the
   child's environment (never in its argv or the log).
4. **kodo-vsix.** Modal → `newSession`, the job view's reducer driven by the
   new events, the locked strip, the waiting text; remove `benchmark.ts`'s
   terminal tests along with the code.
5. **Manual, with Docker:** a Basics test on a local model on macOS, a cloud
   one, and Stop mid-run.
