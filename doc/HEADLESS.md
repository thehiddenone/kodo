# Headless Kōdo — `kodo-headless` and `kodo-llama-server`

> Status: implemented (2026-09-23). Packages: [kodo.headless](../src/kodo/headless/),
> [kodo.llamaserver](../src/kodo/llamaserver/). Server side:
> [security/_sandbox.py](../src/kodo/security/_sandbox.py),
> [llms/llamacpp/_remote.py](../src/kodo/llms/llamacpp/_remote.py), the
> `--headless-sandbox` / `--llama-url` flags in
> [server/_config.py](../src/kodo/server/_config.py). Tests:
> `test/test_headless.py`, `test/test_server_headless.py`,
> `test/test_sandbox_security.py`, `test/test_llamaserver_standalone.py`.
> Built for the Harbor "option A′" integration ([HARBOR_INTEGRATION.md](HARBOR_INTEGRATION.md) §4, "Option A′").
> Cloud models, credentials from the environment and `--transcript-dir` were added
> 2026-09-25 for `kodo-harbor` ([HARBOR.md](HARBOR.md)), which drives this command
> inside Harbor task containers.

Two console scripts:

- **`kodo-llama-server`** serves one local-registry model on a chosen port,
  outside any kodo server, and can start, stop and report on it.
- **`kodo-headless`** runs one prompt through one top-level agent, on a local
  or a cloud model. There is no user and no VS Code. Every change is confined
  to the working directory, and everything the run does is written to stdout.

They are meant to be split across a boundary: the model runs on the host under
`kodo-llama-server`, and Kōdo runs wherever the task lives (for Harbor, a task
container) under `kodo-headless --llama-url`. Run on one machine, `kodo-headless`
can also start its own llama-server.

---

## 1. `kodo-llama-server`

```bash
kodo-llama-server start  --model ENTRY --port N [--host 127.0.0.1] [--foreground] [--replace]
kodo-llama-server stop   --port N
kodo-llama-server status [--port N] [--json]
```

- **`ENTRY`** is a local-registry entry name, which already names both the LLM
  and the quant (for example `unsloth-qwen36-27b-q4-k-xl`). The launch flags
  come from `resolve_llama_launch`, the same function the kodo server itself
  uses. That function applies the entry's **active profile** in
  `~/.kodo/etc/local-llm-registry.json`, so a model launched either way gets
  identical flags. There is no `--profile` flag on purpose: the registry file
  is the only source, and it is the file the container side reads (§3). This
  is what keeps the two sides agreeing on context size and thinking family.
- **`start`** detaches a supervisor (a `--foreground` copy of itself, in a new
  session or process group) and returns once llama-server answers `/health`.
  - It needs a separate supervisor process because asyncio kills a child
    process when its subprocess transport closes, so a plain spawn-and-exit
    would take llama-server down with it.
  - Starting the same model on the same port again succeeds and does nothing.
  - A different model already on that port is refused unless you pass
    `--replace`.
- **`stop`** terminates llama-server first and then the supervisor, using
  Windows-safe helpers (`is_pid_alive` / `terminate_pid` / `kill_pid`, never
  `os.kill(pid, 0)`). The order matters on Windows, where terminating the
  supervisor is `TerminateProcess` and would orphan its child.
- **State** is kept in `~/.kodo/llama.cpp/standalone/<port>.json`, recording
  `{supervisor_pid, llama_pid, host, port, model, profile_id, kodo_version}`.
  Logs go to `~/.kodo/logs/llama-server-<port>.log`.
  - This is deliberately **never** the kodo server's own runtime file,
    `llama.cpp/llama-server.json`. The kodo server adopts whatever that file
    names at startup, and stops it at shutdown or on a model switch.
  - The redirect is `LlamaServerConfig.runtime_file` / `log_file`. Both default
    to `None`, which keeps today's paths.
- **`--alias ENTRY`** is always passed, so `/v1/models` reports the registry
  name. The attach check (§3) relies on it.
- **The GGUF path is looked up read-only** with
  `LocalModelManager.peek_model_path`. Constructing a `LocalModelManager`
  rewrites every `DOWNLOADING` record to `PAUSED`, which is right in the
  process that owns the downloads but would pause a download that a running
  VS Code session has in progress.
- **Bind warnings.** Only a wildcard bind (`0.0.0.0` / `::`) prints a warning,
  because llama-server has no authentication. A specific address, such as the
  docker bridge `172.17.0.1` on Linux, gets an informational line. Loopback
  prints nothing. Docker Desktop on macOS reaches host loopback through
  `host.docker.internal`, so the default already works there.

## 2. `kodo-headless`

```bash
kodo-headless (--prompt TEXT | --prompt-file PATH) --model (ENTRY | VENDOR/MODEL_ID)
  [--agent kodo_problem_solver] [--llama-url URL | --llama-port N] [--cwd DIR]
  [--registry-file PATH] [--thinking-level TIER] [--timeout SEC]
  [--format jsonl|text] [--stream-deltas] [--result PATH] [--transcript-dir DIR]
  [--home DIR] [--keep-home] [--keep-kodo-dir] [--log-level INFO]
```

**`--model`** has three spellings (`kodo.headless.ModelSpec`):

| Spelling | Meaning |
|---|---|
| `ENTRY` | a local-registry entry (LLM + quant), e.g. `unsloth-qwen36-27b-q4-k-xl` |
| `local/ENTRY` | the same, explicitly — the only way to name an entry whose name contains `/` |
| `VENDOR/MODEL_ID` | a cloud model, e.g. `anthropic/claude-sonnet-5`, `openrouter/qwen/qwen3-coder` (only the first `/` splits) |

A cloud model is pinned for every effort tier (`models.cloud_uniform.<vendor>`,
[SETTINGS.md](SETTINGS.md) §2.2d), so a sub-agent's declared `capability`
cannot switch models mid-run. Whether the vendor and model id exist is the
server's knowledge: an unknown one fails at the first LLM call as
`runtime_error` (`kodo-harbor` checks both before queueing anything). A cloud
model takes no llama-server at all; `--llama-url` / `--llama-port` with one is
a `startup_error`.

**A local `--model` is required even with `--llama-url`.** The URL only says *where*
inference runs. The registry entry is *which model Kōdo thinks it is using*,
and three things depend on it:

- thinking tiers (`base_llm`, including the qwen reasoning-budget request
  fields),
- the compaction budget (the active profile's context window),
- sampling defaults.

A bare URL, as with a `custom_server_url` entry, would lose all three.

What a run does, in order:

1. **Builds an isolated home** (`build_headless_home`) from an **allowlist**,
   so the user's real `~/.kodo` is never written and nothing large is copied:
   - *copied:* `etc/settings.json` (with the model merged in — `mode: local`
     + `models.local: ENTRY`, or `mode: cloud` + `active_cloud_vendor` + an
     enabled `models.cloud_uniform.<vendor>`; OpenRouter also gets
     `openrouter_auto_mode: false`, Bedrock gets `bedrock_region` from
     `AWS_REGION` / `AWS_DEFAULT_REGION`) and `etc/local-llm-registry.json`,
     or the `--registry-file` in its place. It is copied, never linked, so no
     write can reach the original.
   - *symlinked:* `bin`, `agents`, `skills`.
   - *omitted:* everything else, including `llama.cpp`, `checkpoints`, `venv`,
     `sessions`, `logs`, credential files, and any entry a later release adds.

   The home is a temporary directory that is deleted afterwards. With
   `--home DIR` it is built in that directory and kept; `--keep-home` keeps
   the temporary one.
2. **Local model, spawn mode (no `--llama-url`):** runs
   `kodo-llama-server start --foreground` on a free port (or `--llama-port`)
   with the *real* home, where the models are, and stops it through
   `kodo-llama-server stop` when the run ends. **Cloud model:** no
   llama-server; the run stops here with `startup_error` when no environment
   variable carries the vendor's key (the message names the variables).
3. **Spawns** `kodo-server --headless-sandbox <cwd> [--llama-url <url>]` with the
   isolated `HOME`, through `kodo.headless.ServerProcess(extra_args=…)` (moved
   here from the now-removed `kodo.validator`).
4. **Drives one session.** It sends `hello`, checks the agent against
   `top_agents.list` (an unknown agent exits with code 4), then sends
   `agent.set`, `workspace.folders` (one root: the cwd), `mode.set
   {autonomous: true}`, optionally `thinking_level.set`, and `prompt.submit`.
   It then waits for the turn to end, bounded by `--timeout`.
5. **Answers every server→client request** deterministically:

   | Request | Answer |
   |---|---|
   | `prompt.permission` | deny (only a recovered malformed call can reach it; the sandbox never asks) |
   | `prompt.question` | first option plus "no user is available…", and **counted** |
   | `prompt.approval` / `prompt.edit_review` | accept / approve |
   | `prompt.stuck_alert` | unstick |
   | `prompt.choose_project_folder` / `workspace.confirm_folder` | refused (the workspace is fixed) |
   | `api_key.request` / `hf_token.request` | the vendor's credential (below) / `HF_TOKEN` from the environment |

   `test_every_server_request_type_has_a_deterministic_answer` fails as soon as
   a new `SREQ_*` constant appears without an answer here.

   Credentials (`kodo.headless.resolve_vendor_api_key`): the first non-empty of

   | Vendor | Variables |
   |---|---|
   | every vendor | `<VENDOR>_API_KEY` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, …) |
   | `google` | + `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` |
   | `alibaba` | + `DASHSCOPE_API_KEY` |
   | `kimi` | + `MOONSHOT_API_KEY` |
   | `meta` | + `LLAMA_API_KEY` |
   | `bedrock` | `BEDROCK_API_KEY` (kodo's JSON pair), else built from `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (no session token: kodo's Bedrock credential is a long-term key pair) |
6. **Cleans up in every case**, including a timeout or SIGINT/SIGTERM (it sends
   `stop` first). It shuts down the server and any llama-server it started,
   and removes `<cwd>/.kodo` (the checkpoint mirror) if this run created it,
   so verifiers see a clean task directory; `--keep-kodo-dir` keeps it. The
   project's own `.git` is never touched.
7. **`--transcript-dir DIR`** copies the session's own log out of the isolated
   home before it is deleted: `session.jsonl` plus `subsessions/<id>.jsonl`
   ([SESSIONS.md](SESSIONS.md)) — the exact LLM-visible conversation, with every
   tool input and result as the model saw it (the stdout `tool.call` document
   is rendered Markdown and not reversible). `kodo.harbor.agent` converts it
   into Harbor's ATIF `trajectory.json`. A `transcript.exported` event reports
   the copy.

### 2.1 stdout

JSONL by default: one object per line, each with `ts` and `type`, and most with
`agent` and `subsession_id` (`null` for the top-level agent). `--format text`
renders the same events for people.

| `type` | Carries |
|---|---|
| `run.start` | agent, model, cwd, llama_url, home |
| `llama.spawn` / `llama.ready` | spawn mode only |
| `phase` | every session phase change |
| `llm.turn` | the agent and model starting an LLM call |
| `thinking` / `text` | one event per segment. The server uses one stream id for a whole agent turn ([WS_PROTOCOL.md](WS_PROTOCOL.md) §2.3), so the client cuts it wherever the chunk kind flips and at every `llm.turn` / `tool.start` / sub-session / `error` boundary. `--stream-deltas` switches to raw `thinking.delta` / `text.delta` |
| `tool.start` | tool name and call id, when the call is announced |
| `tool.call` | the tool's full input and output: the per-call document the server writes (`agent.tool_call_detail.file`), since the wire `rows` are visibility-filtered |
| `tool.denied` | a call the sandbox refused, with its reason |
| `subsession.start` / `subsession.end` | a sub-agent run (with its task brief) |
| `agent.start` / `agent.finish` | agent invocation boundaries |
| `usage` | per LLM call: model, `input_tokens` (cache included), `cache_read_tokens`, `output_tokens`, USD, stop reason, duration |
| `question`, `nudge`, `autonomous.changed`, `error`, `warning`, `transcript.exported` | as named |
| `run.result` | the result record (§2.2), always last |

After `run.result`, one plain line follows:
`KODO-RESULT outcome=<outcome> error=<message>`. It exists for Harbor's
`ERROR_PATTERNS`, which match stdout text. JSONL consumers skip any line that
doesn't start with `{`. Server and llama logs go to files, never to stdout.

### 2.2 Result and exit codes

`RunResult` (`schema_version` 1) is the `run.result` event, and is also written
to `--result` when given. Its fields:

- `outcome`, `session_id`, `agent`, `model` (the canonical `ENTRY` or
  `VENDOR/MODEL_ID`), `final_phase`, `assistant_text`
- the token totals and `cumulative_usd`
- `per_model` and `per_agent` usage, each row `{calls, input_tokens,
  cache_read_tokens, output_tokens, usd}` (`input_tokens` includes cache
  reads; answers "did that sub-agent pay for itself")
- `tool_calls`, `tool_denials`, `questions_asked`, `nudges`
- `wall_seconds`, `error`

| outcome | exit | when |
|---|---|---|
| `completed` | 0 | the turn ended normally |
| `timeout` | 2 | `--timeout` expired (the turn was stopped) |
| `runtime_error` | 3 | an `error` event ended the turn (a provider or LLM failure, or a llama endpoint that fails the attach check) |
| `startup_error` | 4 | bad arguments, an unknown agent or model, or a server or llama-server that would not start |
| `crashed` | 5 | the connection dropped, or an unexpected exception |
| `stopped` | 130 | SIGINT/SIGTERM |

## 3. The headless server: `--headless-sandbox` and `--llama-url`

Two optional `kodo-server` flags, both off by default. `--llama-url` requires
`--headless-sandbox`.

- **`--headless-sandbox DIR`** has three effects:
  - `SessionManager(sandbox_root=…)` makes every engine build a
    `SandboxSecurityLayer(DIR)` instead of the interactive `SecurityLayer`. The
    server passes only the path; `runtime` stays the only importer of
    `kodo.security` ([INTERNALS.md](INTERNALS.md) §2).
  - Startup runs `_start_background_headless`, which skips the steps that
    touch state shared with the user's own kodo:
    - llama adoption,
    - `purge_unknown_local_models` (a version-skewed purge over linked
      directories could delete real downloads),
    - the titler (a second llama-server competing for the GPU),
    - the OpenRouter catalog refresh.

    Only `ensure_all_utils` still runs.
  - Tool calls get allow or deny verdicts, never a prompt (§4).
- **`--llama-url URL`** sets `RemoteLlamaEndpoint` for the whole process.
  `LlamaPlugin.__ensure_running` then never launches or touches a managed
  llama-server: it points its client at the URL. On first use per
  `(url, model)`, `RemoteLlamaEndpoint.verify` checks two things:
  - **Identity:** `/v1/models` lists the entry name (the alias), or `/props`
    reports the entry's GGUF file name (a plain llama-server started by hand).
  - **Context:** `/props` `n_ctx` per slot is at least the context window the
    registry expects, divided by the profile's `--parallel`. A smaller context
    would otherwise surface much later as a mid-run context overflow.

  A mismatch raises, which becomes `runtime_error` with a message naming the
  fix. This is the check that catches a registry, profile or `py-kodo` version
  mismatch between host and container.

## 4. The sandbox posture (`SandboxSecurityLayer`)

**Mutation is confined to the sandbox root. Reads are not. Git may only be
read.** The verdict is final, because there is no user to ask: the dispatcher
turns `deny` into `{"error": "Blocked by the headless sandbox: <reason>"}`
without prompting and without running the tool (doc/SECURITY.md §2b).
Command Control and Autonomous mode are ignored.

- **File tools** (`create_file`, `create_directory`, `edit_file`, and the
  mutated ends of `filesystem`) must resolve inside the root and outside every
  `.git` directory.
  - Symlinks are followed, so a link inside the root that points out is caught.
  - A copy only reads its source; a move mutates both ends.
  - `temporary: true` calls go to the session's own scratch directory and are
    allowed.
  - `toolchain_build` / `toolchain_deps` project paths must be inside the root,
    and `toolchain_deps` keeps the suspicious-dependency check.
- **`run_command` is allow-unless-evidence.** A command is denied only when
  one of these holds:
  - a redirection, a known file-mutating program's target (`rm`, `mv`, `cp`'s
    destination, `sed -i`, `tee`, `dd of=`, `curl -o`, `tar -C`, …) or a
    `cd` resolves outside the root; the OS temp directory is carved out, as
    in `_analysis`;
  - a non-read-only program is given a path into `.git`;
  - git is invoked outside the read-only set, including behind `env`, `sh -c`,
    `xargs`, `find -exec`, `sudo`, `$(…)`, or inline code that mentions git;
  - the program itself is a substitution, or a `cd` cannot be followed (a
    dynamic target, or one inside a loop or conditional that leaves the root).

  Opaque programs (`pytest`, `make`, `pip install`, …) are allowed.
- **Git read-only set:**
  - Always allowed: `status log diff show blame rev-parse ls-files ls-tree
    cat-file describe shortlog grep merge-base …`.
  - Listing forms only: `branch`, `tag`, `remote -v|show|get-url`,
    `stash list|show`, `config --get*|--list|get|list`, `worktree list`,
    `reflog`.
  - Global options `-C`, `--no-pager` and `-P` are allowed. `-c`,
    `--git-dir`, `--work-tree` and `--exec-path` are refused, as are
    `--output` and `--ext-diff` anywhere.
- **Denied outright:** `scaffold_new_project` (the root is fixed) and
  `disable_autonomous_mode` (no user to hand control to). Any tool without an
  explicit policy is also denied: `test_every_dispatchable_tool_has_an_explicit_sandbox_policy`
  forces a decision whenever a tool is added.

## 5. Harbor recipe (option A′)

`kodo-harbor run` ([HARBOR.md](HARBOR.md)) does all of this — starts the host
llama-server, mounts the registry, opens the host to the container, installs
Kōdo and runs `kodo-headless` in every trial. The manual recipe below is what
it automates.

On the host:

```bash
kodo-llama-server start --model unsloth-qwen36-27b-q4-k-xl --port 8090   # macOS (Docker Desktop)
kodo-llama-server start --model unsloth-qwen36-27b-q4-k-xl --port 8090 --host 172.17.0.1   # Linux
```

In the task container (installed by the future `kodo.harbor` adapter, or by hand):

```bash
kodo-headless --prompt-file /harbor/agent/instruction.txt --cwd /app \
  --model unsloth-qwen36-27b-q4-k-xl --llama-url http://host.docker.internal:8090 \
  --registry-file /kodo-host/local-llm-registry.json \
  --result /harbor/agent/kodo-result.json --timeout 3000
```

Harbor configuration (no Harbor change is needed):

- `--mounts '[{"type":"bind","source":"<host>/.kodo/etc/local-llm-registry.json","target":"/kodo-host/local-llm-registry.json","read_only":true}]'`
  gives the container the host's registry. `--registry-file` copies it into
  the isolated home.
- `agents[].extra_allowed_hosts: ["host.docker.internal"]` (or the bridge IP)
  opens the egress allowlist to the host during `agent.run()` only, even for
  `deny-all` tasks.
- On Linux, an `extra_docker_compose` overlay adds
  `extra_hosts: ["host.docker.internal:host-gateway"]` to the `main` service.

## 6. Limits (stated, not built)

- **The sandbox is best-effort.** Static analysis cannot see what a program
  does at run time: `python build.py` may write anywhere. The container is the
  jail. An OS sandbox (bwrap / sandbox-exec) around `run_command` would be the
  real guarantee and is future work.
- **One llama-server serializes concurrent trials.** Run a local arm with
  Harbor `-n 1`, or give the profile `--parallel N`. The attach check already
  divides the context by N.
- **`network_mode: none` tasks** cannot reach the host at all.
- **Host and container should run the same `py-kodo` version.** The hardcoded
  catalog is code; the alias and `n_ctx` check catches most mismatches.
- **The web tools still fail at call time** in network-restricted tasks
  (HARBOR_INTEGRATION.md R4).
- **The full `kodo-headless` → server → llama path has no automated test**
  (it was verified by hand on 2026-09-23 against a real local model: sandbox
  denial of `git commit`, file creation, final answer, exit 0).
  The server's startup runs `ensure_all_utils`, which downloads rg/fd/uv into
  a fresh home. The pieces are tested separately (the sandbox end to end
  in-process, the client against a scripted WS server, the llama side against
  a fake llama-server); the whole path is verified by hand with a real model.
