# Benchmarking Kōdo with Harbor — `kodo-harbor` and `kodo.harbor.agent`

> Status: implemented (2026-09-25); **verified end to end with real Docker on
> 2026-09-27** (§8.1). Packages: [kodo.harbor](../src/kodo/harbor/)
> (the `kodo-harbor` command) and [kodo.harbor.agent](../src/kodo/harbor/agent/)
> (the Harbor agent). Tests: `test/test_harbor_orchestrator.py`,
> `test/test_harbor_agent.py`, `test/test_headless_cloud.py`. Built on
> [HEADLESS.md](HEADLESS.md) (option A′ of [HARBOR_INTEGRATION.md](HARBOR_INTEGRATION.md)).
> Harbor pin: **0.23.0** (`kodo.harbor.HARBOR_VERSION`, and the `harbor==` dev
> dependency in `pyproject.toml` — keep them equal).

`kodo-harbor` runs a benchmark on a Kōdo agent with a chosen model and reports
the result. You pick three things:

- **a Kōdo agent** — any top-level agent: a built-in `kodo_*` one or one you
  installed under `~/.kodo/agents`;
- **a model** — a cloud model (`VENDOR/MODEL_ID`) or a local one (a
  local-registry entry, served on the host by `kodo-llama-server`);
- **a benchmark** — Harbor datasets and tasks, local task directories, or a
  named suite.

Harbor ([harborframework.com](https://harborframework.com)) does the rest:
Docker sandboxes, the tasks' own verifiers (tests, not an LLM judge),
attempts, retries and concurrency. Kōdo runs inside each task container,
autonomously, with no user.

---

## 1. Quick start

Prerequisites on the host: **Docker** (running), **uv**, and `py-kodo`.
Harbor itself is *not* installed: `kodo-harbor` runs the pinned release in a
throwaway `uv` environment.

```bash
# The one-task smoke test first: install, run and verify, end to end.
export ANTHROPIC_API_KEY=…
kodo-harbor run --model anthropic/claude-sonnet-5 --suite smoke

# Terminal-Bench 2.0, three attempts per task, against Harbor's reference agent.
kodo-harbor run --model anthropic/claude-sonnet-5 --suite terminal-bench \
  --attempts 3 --control terminus-2

# A local model (downloaded in the Kōdo extension), a 10-task sample, the Guide.
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl --agent kodo_guide \
  --suite terminal-bench-sample --control terminus-2

# One SWE-bench Verified task on an Apple Silicon Mac: its images are amd64-only (§2.3).
kodo-harbor run --model anthropic/claude-sonnet-5 --dataset swebench-verified@1.0 \
  --include django__django-15098 --platform linux/amd64

# See the plan without running anything.
kodo-harbor run --model anthropic/claude-sonnet-5 --dataset terminal-bench@2.0 \
  --include 'fix-*' --n-tasks 5 --dry-run
```

Each run prints a summary and writes it next to Harbor's results
(`jobs/<job>/kodo-summary.md`, `kodo-summary.json`). `harbor view jobs` shows
every trial, including Kōdo's trajectory.

### 1.1 One task, or a few, from a dataset

`--include` picks tasks out of a dataset by Harbor task name, exact or as a
shell-style glob. Repeat it to pick more than one.

```bash
# One Terminal-Bench 2.0 task, by name.
kodo-harbor run --model anthropic/claude-sonnet-5 \
  --dataset terminal-bench@2.0 --include regex-log

# The same task on a local model, five attempts.
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl \
  --dataset terminal-bench@2.0 --include regex-log --attempts 5

# Two named tasks plus every task matching a glob (quote the glob).
kodo-harbor run --model anthropic/claude-sonnet-5 --dataset terminal-bench@2.0 \
  --include regex-log --include sqlite-with-gcov --include 'qemu-*'

# One task, Kōdo against Harbor's reference agent on the same model.
kodo-harbor run --model anthropic/claude-sonnet-5 --dataset terminal-bench@2.0 \
  --include chess-best-move --control terminus-2

# One Harbor package task (ORG/NAME[@REF]).
kodo-harbor run --model anthropic/claude-sonnet-5 --task acme/one-task@latest

# A task downloaded to disk, so you can read (or edit) its instruction and tests first.
uvx --from harbor==0.23.0 harbor datasets download terminal-bench@2.0 -o tasks
kodo-harbor run --model anthropic/claude-sonnet-5 --path tasks/terminal-bench/regex-log
```

- **Finding task names:** `harbor datasets download` writes one directory per
  task, named after the task (`<dir>/<dataset>/<task>/`).
- **A misspelled name** isn't caught by the preflight checks or by
  `--dry-run`. Harbor rejects it when it expands the dataset, with
  `No tasks matched the filter(s) …` and five example task names.
- `--include` applies to **every** dataset in the run (§2.2). To filter
  datasets differently, use separate runs or a suite with per-dataset
  `task_names` (§3).

### 1.2 A whole dataset on one machine, one container at a time

Harbor gives every task its own container: each task ships its own image
(Terminal-Bench 2.0: `ghcr.io/laude-institute/terminal-bench/<task>:2.0`) and
its own verifier, so tasks can't share a container. To run a whole dataset
on a laptop, run **one trial at a time**. Only one task container exists at
any moment, and Harbor removes it after its verifier finishes and before the
next trial starts.

`--concurrency 1` does this. A local model already defaults to it, because
one llama-server serves one request at a time. A cloud model defaults to 4,
so pass the flag yourself.

```bash
# Every Terminal-Bench 2.0 task (89), local model, one container at a time.
# kodo-harbor starts the host llama-server first and stops it at the end.
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl --suite terminal-bench

# The same run, naming the dataset instead of the suite, with the Guide.
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl --agent kodo_guide \
  --dataset terminal-bench@2.0

# Every task, cloud model, one container at a time.
export ANTHROPIC_API_KEY=…
kodo-harbor run --model anthropic/claude-sonnet-5 --dataset terminal-bench@2.0 \
  --concurrency 1

# The 10-task sample first, to check the model before committing to a full run.
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl --suite terminal-bench-sample

# A whole dataset downloaded to disk (the directory holds one directory per task).
kodo-harbor run --model unsloth-qwen36-27b-q4-k-xl --path tasks/terminal-bench
```

- **Duration:** a Terminal-Bench 2.0 task gives the agent up to 15 minutes
  (`[agent] timeout_sec = 900` in its `task.toml`). Each trial also spends
  about 30 s installing Kōdo (§8), and each task's image is pulled the first
  time it runs. A sequential full run takes hours. `--timeout SEC` caps
  Kōdo's own turn below the task's limit.
- **Disk:** Harbor removes each task container after its trial, but the
  pulled task images stay in Docker's image cache. `-- --no-delete` keeps
  every container for inspection, and those containers add up over a full run.
- **Stopping:** Ctrl+C winds down the trial in progress, stops the
  llama-server, and reports the trials that finished. `kodo-harbor` won't reuse
  an existing job directory, so the next run needs a new `--job-name` (the
  default name is timestamped, so leaving `--job-name` off also works).
- **Several local runs in a row:** `--keep-llama` leaves the llama-server
  running, and the next run on the same model and port reuses it instead of
  loading the model again (§5, Local).

## 2. `kodo-harbor`

```
kodo-harbor run --model (VENDOR/MODEL_ID | ENTRY) [--agent kodo_problem_solver]
    (--dataset NAME[@VER] | --task ORG/NAME[@REF] | --path DIR | --suite NAME|FILE)...
    [--include GLOB]... [--exclude GLOB]... [--n-tasks N]
    [--control AGENT]... [--attempts K] [--concurrency N] [--max-retries N]
    [--thinking-level TIER] [--timeout SEC] [--platform OS/ARCH]
    [--kodo-wheel PATH | --kodo-version VERSION]
    [--jobs-dir jobs] [--job-name NAME]
    [--llama-port 8090] [--llama-bind ADDR] [--keep-llama]
    [--dry-run] [-- HARBOR_RUN_ARGS...]
kodo-harbor summarize JOB_DIR [--json]
kodo-harbor suites [--json]
kodo-harbor list-models [local|cloud] [--json]
```

Exit code: Harbor's own, or `2` when the run could not start (the message says
why).

### 2.1 What `run` checks before anything starts

A mistake fails in seconds, not on trial 400:

| Check | Failure |
|---|---|
| `--model` spelling ([HEADLESS.md](HEADLESS.md) §2) | malformed |
| cloud: the vendor is one Kōdo supports | lists the supported vendors |
| cloud: the model id is in the vendor's catalog (OpenRouter and Bedrock catalogs are fetched at runtime, so any id passes) | lists the known ids |
| cloud: a credential for the vendor is set in this shell | names the variables |
| local: the entry is in the host's local registry | suggests similar names |
| a non-built-in `--agent`: `~/.kodo/agents` exists | |
| something to run was given; suite files are valid | |
| a `--control` other than `terminus-2` with a local model | refused (see §5) |
| `uv` is found; Docker answers `docker info` (skipped by `--dry-run`) | |

### 2.2 Choosing tasks

| Flag | Harbor entry |
|---|---|
| `--dataset terminal-bench@2.0` | a registry dataset (`name` + `version`) |
| `--dataset acme/private-bench@3` | a Harbor package dataset (`name` + `ref`) |
| `--task acme/one-task@latest` | one package task |
| `--path DIR` | a local task (the directory holds `task.toml`) or a local dataset |
| `--suite NAME\|FILE` | a suite (§3) |

All of them combine. `--include` / `--exclude` (task-name globs) and
`--n-tasks` apply to every dataset: `--include` replaces a suite's own
`task_names`, `--exclude` adds to its `exclude_task_names`, `--n-tasks`
replaces its cap.

### 2.3 Everything else

- **`--control AGENT`** (repeatable) adds a Harbor agent that runs the same
  tasks, attempts and sandbox with the same model — the control arm that turns
  a score into an effect size. `terminus-2`, Harbor's reference agent, is the
  one to use; any other Harbor agent works with a cloud model (§5).
- **`--attempts K`**: `n_attempts`. **`--concurrency N`**: trials at once;
  default 4 for a cloud model and **1 for a local one**, because one
  llama-server serves one request at a time.
- **`--max-retries N`**: Harbor retries a trial that errored. Harbor's default
  exclusions still apply (timeouts, authentication, usage limits, safety
  refusals are never retried).
- **`--timeout SEC`** bounds Kōdo's own turn. Without it, Harbor's per-task
  agent timeout governs, as it does for every other agent (§4.3).
- **`--thinking-level TIER`**: Kōdo's thinking tier for the model's family.
- **`--platform OS/ARCH`** (e.g. `linux/amd64`): the platform Docker builds
  or pulls every task image for. The default is Docker's own platform. Use it
  on an Apple Silicon or other arm64 host for a dataset whose images are
  amd64-only. SWE-bench's are (`swebench/sweb.eval.x86_64.*`), and without
  the flag their build fails with `no match for platform in manifest`. The
  container then runs under emulation, so turn on Docker Desktop's Rosetta
  setting and expect slower trials. The model is unaffected: a local model
  still runs natively on the host's llama-server. The flag is written to the
  job as a compose overlay (`kodo-platform.yaml`, §2.5) that sets
  `services.main.platform`, so `harbor run -c` reproduces it. A malformed
  value fails before the job directory is created.
- **`-- ARGS`**: anything after `--` goes to `harbor run` verbatim (e.g.
  `-- --debug`, `-- -y`).
- **`--dry-run`** writes the job config, prints the Harbor command, and stops:
  no Docker, no llama-server, no trials.

### 2.4 Which Kōdo the containers run

Every trial installs `py-kodo` in its container. By default that is **the
same Kōdo `kodo-harbor` was started from**:

| You run `kodo-harbor` from | Containers get |
|---|---|
| an installed release | `py-kodo==<that version>` from PyPI |
| an **editable source checkout** | a wheel built from the checkout (`uv build`) into the job directory — PyPI's release with the same number is not the code being measured |
| `--kodo-wheel PATH` | that wheel, uploaded into each container |
| `--kodo-version V` | `py-kodo==V` from PyPI |

Harbor's own process gets the same wheel or release (`uv tool run --with …`),
so the host, the adapter and the containers always run identical Kōdo.

### 2.5 What a job directory holds

Harbor's usual layout (`config.json`, `result.json`, one directory per trial)
plus `kodo-harbor`'s own **files** (Harbor removes unknown *directories* when
resuming a job, never files):

| File | What |
|---|---|
| `kodo-job.json` | the Harbor `JobConfig` — `harbor run -c` reproduces the run from it alone |
| `kodo-compose.yaml` | Linux, local model: the compose overlay that maps `host.docker.internal` |
| `kodo-platform.yaml` | `--platform`: the compose overlay that pins the task image's platform |
| `py_kodo-*.whl` | the wheel built from an editable checkout |
| `kodo-summary.md` / `.json` | the report (§6) |

Each trial's `agent/` directory holds Kōdo's outputs (§4.2).

### 2.6 Which models `--model` accepts — `list-models`

`kodo-harbor list-models` prints every `--model` value `run` can run, local
models first. `local` or `cloud` narrows it to one kind; `--json` prints
`{model, kind, description, credential}` rows.

```
$ kodo-harbor list-models
bartowski-ornith15-35b-a3b-bf16  installed  Ornith 1.5 35B A3B BF16 by bartowski
anthropic/claude-sonnet-5        key set    Claude Sonnet 5
openai/gpt-5.6-sol               no key     GPT-5.6 Sol
openrouter/<MODEL_ID>            no key     any model id it serves
```

- **Local:** only the entries `kodo-llama-server` can serve: an installed
  GGUF (a catalog download or a `custom_file`). A `custom_server_url` entry is
  left out, because it is not a model Kōdo launches. When nothing is
  installed, a hint goes to stderr. `credential` is `null`.
- **Cloud:** the whole catalog, each row marked by whether this shell holds
  the vendor's credential (`key set` / `no key`). That is the same check `run`
  makes (§2.1). OpenRouter and Bedrock fetch their catalogs at runtime, so
  each gets one `VENDOR/<MODEL_ID>` placeholder row.

## 3. Suites

A suite is a named, reusable list of Harbor datasets and tasks in one JSON
file:

```json
{
  "name": "my-slice",
  "description": "Twelve Terminal-Bench tasks sized for a 27B local model.",
  "datasets": [
    {"name": "terminal-bench", "version": "2.0",
     "task_names": ["fix-*", "sqlite-*"], "exclude_task_names": ["*-gpu"], "n_tasks": 12}
  ],
  "tasks": [{"name": "acme/one-task", "ref": "latest"}, {"path": "tasks/mine"}]
}
```

- `datasets` entries take `name`, `version`, `ref`, `path`, `task_names`,
  `exclude_task_names`, `n_tasks`; `tasks` entries take `name`, `ref`, `path`
  (Harbor's own `DatasetConfig` / `TaskConfig` fields). Each needs exactly one
  of `name` or `path`; a relative `path` is relative to the suite file.
- Built-in suites ship in `kodo/harbor/suites/`: `smoke` (hello-world, 1 task),
  `terminal-bench-sample` (10 tasks), `terminal-bench` (Terminal-Bench 2.0, 89
  tasks). Your own go in `~/.kodo/harbor/suites/*.json`; one with a built-in's
  name shadows it. `--suite` also takes a path to any suite file.

## 4. The Harbor agent — `kodo.harbor.agent:KodoAgent`

`kodo-harbor` is a convenience; the agent works in any Harbor job:

```bash
harbor run -d terminal-bench@2.0 --agent-import-path kodo.harbor.agent:KodoAgent \
  -m anthropic/claude-sonnet-5 --ak agent=kodo_guide --ae ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

(Harbor's environment must have `py-kodo` importable: `uv tool run --from
harbor==0.23.0 --with py-kodo harbor run …`.)

### 4.1 Options (`--agent-kwarg` / `agents[].kwargs`)

`harbor agent schema kodo.harbor.agent:KodoAgent` prints these.

| kwarg | Default | Meaning |
|---|---|---|
| `agent` | `kodo_problem_solver` | the Kōdo top-level agent |
| `llama_url` | — | local model: the host llama-server as the container reaches it |
| `registry_file` | — | local model: container path of the mounted host registry |
| `thinking_level` | — | Kōdo thinking tier |
| `turn_timeout_sec` | — | Kōdo-side turn bound; omitted, Harbor's timeout governs |
| `kodo_wheel` | — | host path of a py-kodo wheel to install instead of PyPI |
| `version` | the adapter's own | py-kodo version from PyPI |
| `agents_dir` | — | host path of user agents (`~/.kodo/agents`) to install |
| `python_version` | `3.12` | the Python uv installs for Kōdo |
| `workdir` | the task's working directory | the sandbox root |

`-m` is the model: `VENDOR/MODEL_ID`, or `local/ENTRY` with `llama_url`.

### 4.2 One trial

1. **Install** (Harbor's setup phase, network open): `curl`/`git`/`tar` from the
   image's package manager if missing; uv; a standalone Python; `py-kodo` as a
   uv tool (PyPI or the uploaded wheel); Kōdo's bundled rg/fd/uv utilities, so
   the agent phase never downloads them; user agents when `agents_dir` is set.
2. **Run**: the instruction is uploaded as a file (never a shell argument), the
   task's skills are copied into `~/.kodo/skills`, and one `kodo-headless`
   turn runs in the task's working directory — autonomous, sandboxed to that
   directory, git read-only ([HEADLESS.md](HEADLESS.md) §4). A working
   directory of `/` is refused (`--ak workdir=DIR`).
3. **Outputs**, in the trial's `agent/` directory:

   | File | What |
   |---|---|
   | `instruction.md` | the instruction as given |
   | `kodo.jsonl` | every event of the run ([HEADLESS.md](HEADLESS.md) §2.1) |
   | `kodo-result.json` | the `RunResult` |
   | `kodo-session/` | the exported session log (`session.jsonl`, `subsessions/`) |
   | `trajectory.json` | ATIF-v1.7, converted from the session log |
   | `kodo-stderr.log` | the run's stderr |

4. **Context** (`populate_context_post_run`): input tokens (cache included),
   cache *reads*, output tokens and cost; `model_usage` per model; and
   `metadata.kodo` — outcome, questions asked, tool calls, sandbox denials,
   nudges, wall time, error, per-sub-agent usage, py-kodo version.

### 4.3 Failures, timeouts and classification

- **Exit codes pass through.** A non-zero `kodo-headless` exit
  (`timeout`/`runtime_error`/`startup_error`/`crashed`) becomes the trial's
  exception, and Harbor **still runs the verifier** — the run is graded on
  whatever it left, the same treatment every agent gets.
- **Only the `KODO-RESULT` line reaches Harbor's error classifier.** The event
  stream goes to `kodo.jsonl`; the exec output is the one summary line. A tool
  that printed "rate limit" or "Connection refused" can therefore never be
  mistaken for a provider failure. A missing key classifies as
  `AgentAuthenticationError` (never retried); provider errors in the final
  line use Harbor's standard patterns (so `ApiRateLimitError` is retryable).
- **Harbor's agent timeout** cancels the exec, but a process inside a container
  outlives its `docker exec`. The adapter then sends `kodo-headless` SIGTERM
  (it stops the turn, writes its result and cleans up) and waits up to 25 s
  before SIGKILL, so the verifier never runs while Kōdo is still editing.
  Tokens are recovered from the `usage` events already in `kodo.jsonl` when
  no result was written (`metadata.kodo.result_source: "partial_log"`).

### 4.4 The trajectory (ATIF)

`kodo-headless --transcript-dir` exports the session's own log — the exact
LLM-visible conversation. Each assistant message becomes one `agent` step
(`text` → message, `thinking` → reasoning, `tool_use` → tool calls, the
preceding `usage` marker → metrics); each `tool_result` becomes an
observation on the step that made the call. Every sub-agent run is embedded as
a `subagent_trajectories` entry (`trajectory_id` = the subsession id) and
referenced from the observation of the `run_subagent_*` call that spawned it.
`final_metrics` totals include the sub-agents. Compaction and error markers
become `system` steps.

## 5. Models

### Cloud

The credential variables that are set in your shell are forwarded to Kōdo's
trials **as `${NAME}` templates**; Harbor resolves them from your environment
when each trial starts, so the key is never written to the job file or the
job directory (Harbor also scrubs trial output). Only the vendor's own
variables are forwarded ([HEADLESS.md](HEADLESS.md) §2, "Credentials"). The
vendor's API host is added to the trial's agent-phase allowlist, so tasks with
a restricted network still reach the model.

A `terminus-2` control gets the same model under LiteLLM's name
(`google/…` → `gemini/…`, `kimi/…` → `moonshot/…`, `alibaba/…` →
`dashscope/…`) and key variable. Meta and Bedrock have no control mapping.
Other control agents receive `VENDOR/MODEL_ID` and the same variables.

### Local

The model runs on the host under `kodo-llama-server`, and trials attach to it
by URL ([HEADLESS.md](HEADLESS.md) §5):

- `kodo-harbor` starts it on `--llama-port` (default 8090), or reuses one
  already serving that model there, and stops only one it started
  (`--keep-llama` leaves it). A port serving another model is an error.
- **macOS / Windows (Docker Desktop):** it binds `127.0.0.1`;
  `host.docker.internal` already reaches the host's loopback.
  **Linux:** it binds the `docker0` address (usually `172.17.0.1`,
  `--llama-bind` overrides) and a compose overlay maps
  `host.docker.internal:host-gateway`.
- The host's `local-llm-registry.json` is mounted read-only, so the container
  uses the same entry, profile and context window; the attach check refuses a
  mismatched server ([HEADLESS.md](HEADLESS.md) §3).
- `host.docker.internal` is on the agent-phase allowlist. Tasks with
  `network_mode: none` cannot reach the host at all. On a task whose network
  is already public (hello-world, most of Terminal-Bench) Harbor ignores the
  extra host and prints a harmless `UserWarning: Run-specific allowlist
  host(s) ['host.docker.internal'] are ignored because the effective network
  policy is public` — once per phase.
- A `terminus-2` control runs on the host and talks to the same llama-server's
  OpenAI endpoint; its context limit is read from the server's `/props`.
  Agents that run *inside* the container (claude-code, codex, …) are refused
  as controls for a local model.

## 6. The report

`kodo-harbor summarize JOB_DIR` (and every `run`) reads each trial's
`result.json`. An **arm** is one agent on one model, e.g.
`kodo[kodo_guide] @ anthropic/claude-sonnet-5`.

- **Per arm:** trials; scored (the verifier ran); solved (reward ≥ 1); mean
  reward (an unscored trial counts 0); errors by exception type; input
  (cached) / output tokens; cost; mean agent time.
- **Kōdo arms also:** outcomes, questions asked with no user present, tool
  calls, sandbox denials, stuck-watchdog nudges, and cost per sub-agent.
- **Paired comparison:** each Kōdo arm against every other arm, over the tasks
  both ran, by mean reward per task: Kōdo better / worse / tied, with a
  two-sided exact sign-test p-value over the untied tasks. The task is the
  unit, not the attempt: attempts of one task are not independent samples.

## 7. Design

- **Two packages, one direction.** `kodo.harbor` (the command) never imports
  `harbor`: it writes a `JobConfig` and runs Harbor through `uv tool run`.
  `kodo.harbor.agent` (the adapter) imports only `harbor` and `kodo.headless`,
  and only Harbor's process imports it. Nothing in `kodo` imports it
  ([INTERNALS.md](INTERNALS.md) §2). Harbor is a pinned **dev** dependency
  (mypy analyzes its annotated sources through `follow_untyped_imports`; the
  adapter tests build agents with Harbor's own factory).
- **The job file is the contract.** Everything `kodo-harbor` decides is in
  `kodo-job.json`; the tests validate it with Harbor's `JobConfig` and build
  every agent row with `AgentFactory`, so a Harbor upgrade that breaks the
  contract fails in the test suite.
- **Kōdo in the container, model outside** (option A′): Kōdo's tools run
  natively on the task files, exactly as a user gets them, and a local model
  keeps its full identity.

## 8. Limits

- **Docker is required.** Other Harbor environments (Modal, Daytona, …) are
  reachable with `-- --env …`, but a local model needs the host reachable from
  the sandbox, which only a local Docker gives.
- **Install cost per trial:** uv, Python and `py-kodo` (with Playwright's Python
  package) are installed in every container. It is the bulk of a short task's
  wall time (§8.1: 53 s total, 24 s of it the agent) and grows on a cold
  package cache.
- **No MCP client:** a task's MCP servers are ignored (with a warning).
- **Single-step tasks only:** no `resume` / multi-step tasks, no `load`/handoff.
- **Linux containers only** (`capabilities.windows` is false).
- **Web tools** need the network in the agent phase; `read_webpage` also needs a
  Playwright browser the image lacks.
- **Bedrock** needs a long-term access-key pair; session tokens are not
  supported by Kōdo's Bedrock credential.
- **Security-rule measurement (the `GatedEnvironment` idea) was dropped:** an
  exec-level gate never sees the commands of installed agents (they run inside
  one `exec`), sees terminus-2's commands only as `tmux send-keys`, and cannot
  tell agent commands from Harbor's setup and verifier commands — Harbor
  marks no phases on the environment.

### 8.1 Verified runs

**2026-09-27 — first real Docker run (macOS, Docker Desktop), local model:**

```bash
kodo-harbor run --model bartowski-ornith15-35b-a3b-bf16 --suite smoke
```

- Run from the editable checkout, so `kodo-harbor` built a py-kodo wheel and
  installed it in the container (§2.4); Harbor 0.23.0 ran under `uv tool run`.
- Defaults throughout: the host llama-server on `--llama-port` 8090, bound to
  loopback, reached from the container as `host.docker.internal` (§5, Local).
- hello-world: **1 trial, 0 exceptions, reward 1.000**; total runtime 53 s,
  agent time 24 s.
- Kōdo report: outcome `completed`, 2 tool calls, 0 sandbox denials, 0
  questions, 0 nudges; 3 LLM calls, 69,685 input / 274 output tokens, $0.00.
- The only noise was Harbor's allowlist `UserWarning` for a public-network task
  (§5, Local).

Before that (2026-09-25, no Docker available), the pieces were verified
separately: the adapter's command, exit codes, error classification,
SIGTERM-on-timeout and context/ATIF fill against a stand-in container; Harbor
0.23.0 loading the adapter from the built wheel (`harbor agent schema`); the job
file against Harbor's `JobConfig` and `AgentFactory`; the built-in suites
against the live registry; and a real local-model `kodo-headless` run with
`--transcript-dir` converted to ATIF.

Not yet run for real: a cloud model, a `terminus-2` control arm, a Linux host
(the `docker0` bind + compose overlay), and anything larger than `smoke`.
