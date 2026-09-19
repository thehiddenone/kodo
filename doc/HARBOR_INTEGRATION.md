# Kōdo × Harbor — Integration Proposal & Plan

> Status: **proposal — nothing implemented**.
> Scope: **server-side only** (`py-kodo`). No `kodo-vsix` change is required by
> any option below, and none is proposed.
> Sources read for this document (2026-09-17):
> `github.com/harbor-framework/harbor` @ `main` — `src/harbor/agents/base.py`,
> `src/harbor/agents/installed/base.py`, `src/harbor/agents/capabilities.py`,
> `src/harbor/agents/options.py`, `src/harbor/agents/model_connection.py`,
> `src/harbor/environments/base.py`, `src/harbor/models/agent/context.py`,
> `src/harbor/models/agent/name.py`, `src/harbor/models/agent/rollout_detail.py`;
> plus `docs.harborframework.com` (`core-concepts/agents/custom-agents`,
> `core-concepts/sandboxes/custom-sandboxes`, `core-concepts/agents/acp`,
> `core-concepts/jobs/run-a-job`, `getting-started/installation`).
> Kōdo-side references: [VALIDATOR.md](VALIDATOR.md), [WS_PROTOCOL.md](WS_PROTOCOL.md),
> [SECURITY.md](SECURITY.md), [SETTINGS.md](SETTINGS.md),
> [LLM_REGISTRY.md](LLM_REGISTRY.md), [LLM_GATEWAY.md](LLM_GATEWAY.md),
> [SKILLS.md](SKILLS.md), [INTERNALS.md](INTERNALS.md).

---

## 1. Why do this at all

Kōdo currently has exactly one way to measure itself: `kodo.validator`, which
runs real sessions and scores them with an LLM judge. That instrument has two
known defects — **no control arm** (every scenario runs Kōdo, so a score is
never an effect size) and **a judge made of the thing being measured** (the RVP
judge is a Kōdo session, [VALIDATOR.md](VALIDATOR.md) §9.2).

Harbor fixes the second defect outright and makes the first cheap:

- **Program verifiers instead of a judge.** A Harbor task ships its own
  `verifier` (tests). Pass/fail is produced by the task, not by an LLM, and
  certainly not by a Kōdo LLM.
- **A control arm for free.** Harbor already ships ~45 agent integrations
  (`claude-code`, `codex`, `mini-swe-agent`, `terminus-2`, `openhands`, …). The
  same task, same model, same container, different agent *is* the ablation —
  no second harness to write.
- **Task corpora we do not have to author.** Terminal-Bench 2.0 and the Harbor
  Hub datasets are addressable by name (`-t org/task`, `-d org/dataset`).
- **Parallelism and isolation we do not have to build.** Docker/Modal/Daytona/GKE
  sandboxes, `-n` concurrency, `-k` attempts, `-r` retries.

The cost is one adapter class. The rest of this document is about **which**
adapter class, and what it has to be handed to work.

Non-goal: replacing `kodo.validator`. The validator exercises the *interactive*
product (gates, questions, approvals, multi-root workspaces) which Harbor tasks
deliberately have no notion of. Harbor is the outcome instrument; the validator
stays the behavior instrument.

---

## 2. Harbor, precisely

### 2.1 The three interfaces

Verbatim from `src/harbor/agents/base.py` (abstract surface only; ~20 further
concrete helpers and class attributes are omitted):

```python
class BaseAgent(ABC):
    capabilities: ClassVar[AgentCapabilities] = AgentCapabilities()
    options_model: ClassVar[type[AgentOptions] | None] = None
    MODEL_CONNECTION: ClassVar[ModelConnectionSpec | None] = None

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        mcp_servers: list[MCPServerConfig] | None = None,
        skills_dir: str | None = None,
        *,
        extra_env: dict[str, str] | None = None,
        load_trajectory: str | Path | None = None,
        environment_logs_dir: PurePosixPath | None = None,
        **kwargs,
    ): ...

    @staticmethod
    @abstractmethod
    def name() -> str: ...

    @abstractmethod
    def version(self) -> str | None: ...

    @abstractmethod
    async def setup(self, environment: BaseEnvironment) -> None: ...

    @abstractmethod
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None: ...

    # optional, default to NotImplementedError / no-op:
    async def resume(self, instruction, environment, context) -> None: ...
    async def load(self, instruction, environment, context) -> None: ...
    @classmethod
    def handoff(cls, trial_dir: Path, cwd: Path) -> list[str]: ...
    @classmethod
    def preflight(cls, kwargs=None, env=None) -> None: ...
    def populate_context_post_run(self, context: AgentContext) -> None: ...
```

`AgentContext` (`src/harbor/models/agent/context.py`) is a pydantic model — the
**only** channel through which an agent reports anything but files-on-disk:

```python
class AgentContext(BaseModel):
    n_input_tokens: int | None      # includes cache
    n_cache_tokens: int | None
    n_output_tokens: int | None
    cost_usd: float | None
    model_usage: dict[str, ModelUsage] | None   # per model, subagents included
    rollout_details: list[RolloutDetail] | None # token ids / logprobs / loss masks
    metadata: dict[str, Any] | None
```

`BaseEnvironment` (`src/harbor/environments/base.py`, 1422 lines) — the abstract
surface a sandbox provider must fill:

```python
class BaseEnvironment(ABC):
    @staticmethod
    @abstractmethod
    def type() -> str: ...
    @abstractmethod
    def _validate_definition(self) -> None: ...
    @abstractmethod
    async def start(self, force_build: bool) -> None: ...
    @abstractmethod
    async def stop(self, delete: bool): ...
    @abstractmethod
    async def exec(
        self, command: str, cwd: str | None = None, env: dict[str, str] | None = None,
        timeout_sec: int | None = None, user: str | int | None = None,
    ) -> ExecResult: ...            # ExecResult(stdout, stderr, return_code)
    @abstractmethod
    async def upload_file(self, source_path: Path | str, target_path: str): ...
    @abstractmethod
    async def upload_dir(self, source_dir: Path | str, target_dir: str): ...
    @abstractmethod
    async def download_file(self, source_path: str, target_path: Path | str): ...
    @abstractmethod
    async def download_dir(self, source_dir: str, target_dir: Path | str): ...
```

Everything else on it — `is_file`, `is_dir`, `reset_dirs`, network policy,
per-compose-service execution, healthchecks, `_merge_env`, `_resolve_user` — is
concrete and inherited.

### 2.2 One correction to the import paths

The brief named `from harbor.agents.context import AgentContext`. That module
does not exist on `main` (HTTP 404); the class lives at
**`harbor.models.agent.context`**, and every in-repo agent and the published
docs import it from there. The other two paths in the brief are exact:
`harbor.agents.base.BaseAgent`, `harbor.environments.base.BaseEnvironment`.

### 2.3 How Harbor loads code it does not ship

This is the single most important fact for us: **custom agents and custom
sandboxes are addressed by import path, not by registry name.**

```bash
harbor run -t hello-world/hello-world -a kodo.harbor:KodoAgent -m anthropic/claude-sonnet-5
harbor run -t hello-world/hello-world -a codex -e kodo.harbor:GatedEnvironment
```

Constructor options go through `--agent-kwarg/--ak` (validated against the
agent's `options_model`), environment variables through `--agent-env/--ae`,
sandbox options through `--environment-kwarg/--ek`; the same three exist as
`agents[].kwargs`, `agents[].env` and `environment.kwargs` in a job config JSON.

**Consequence: no Harbor pull request is needed, ever, for any option in this
document.** Kōdo ships the adapter; the module only has to be importable from
the Harbor process. Upstreaming (adding `KODO = "kodo"` to
`harbor.models.agent.name.AgentName`) becomes a marketing decision, not an
engineering dependency.

### 2.4 Installed vs external — Harbor's own framing

| | Installed (`BaseInstalledAgent`) | External (`BaseAgent`) |
|---|---|---|
| Where the agent loop runs | inside the task container | inside the Harbor host process |
| How it touches the task | natively, it *is* in the container | only through `environment.exec` / up/download |
| Examples in-repo | `claude-code`, `codex`, `aider`, `acp` | `terminus-2` |
| Harbor's guidance | "**prefer** `BaseInstalledAgent`" | "**only when** the agent loop must remain outside the task environment" |

`BaseInstalledAgent` adds `install(environment)`, `exec_as_root` /
`exec_as_agent`, a `SYSTEM_PACKAGES` table with per-package-manager names
(apt/dnf/yum/apk — including `python3`, `python_pip`, `python_venv`), an
`ERROR_PATTERNS` table that classifies provider failures out of agent stdout
into typed exceptions (`ApiRateLimitError`, `ContextWindowExceededError`,
`AgentAuthenticationError`, …) that Harbor's retry logic understands, and the
`@with_prompt_template` decorator for `prompt_template_path`.

---

## 3. What Kōdo already has (and what it is missing)

The adapter is small **because most of it already exists**. `kodo.validator` is
a headless client that drives a real Kōdo server end to end with no VS Code and
no human ([VALIDATOR.md](VALIDATOR.md) §2):

| Piece | Module | Reusable as-is? |
|---|---|---|
| Spawn `python -m kodo.server` on a free loopback port, `HOME` redirected | `validator/_server.py` (`ServerProcess`, `pick_free_port`) | yes |
| Isolated `~/.kodo` per run | `validator/_home.py` (`clone_kodo_home`) | host-side only; in a container the container *is* the isolation |
| WS pseudo-extension: `hello`, requests, turn-boundary detection | `validator/_client.py` (`ValidatorClient`, `wait_turn_end`) | yes |
| Answer every server→client gate (`prompt.question`, `prompt.approval`, `prompt.permission`, `api_key.request`) | `validator/_user.py` (`UserSimulator`, `ScriptedUser`) | yes |
| Bind workspace roots (`workspace.folders`) | `validator/_workspace.py` | yes |
| Full frame-level transcript | `validator/_transcript.py` | yes |
| One run, end to end | `validator/_harness.py` (`ValidationHarness`) | **not** as-is — carries judge/LUT/local-model concerns Harbor must not inherit |

Four further facts that make this work:

1. **Autonomous mode is already the non-interactive posture.** `mode.set
   {autonomous: true}` (`MSG_MODE_SET`) puts the security layer in permissive
   ([SECURITY.md](SECURITY.md) §"Autonomous mode ⇒ permissive"), withholds
   `ask_user`, and auto-accepts the document-review gate. A Harbor trial needs
   no human and must never block on one.
2. **Usage accounting already exists, with per-agent attribution.**
   `usage.update` ([WS_PROTOCOL.md](WS_PROTOCOL.md) §5.7) carries
   `cumulative_input_tokens`, `cumulative_input_tokens_uncached`,
   `cumulative_output_tokens`, `cumulative_usd`, per-call `usd_cost`, `model`,
   `stop_reason` and `agent` (main agent *or* sub-agent). That is a direct,
   lossless fill of `AgentContext` — including `model_usage` broken down by
   model "including subagents", which most agents cannot produce.
3. **The wheel is pure Python.** `py_kodo-0.4.26-py3-none-any.whl` contains no
   compiled extension (the stray `src/kodo/rust_native.abi3.so` in the worktree
   is imported by nothing). `pip install py-kodo` works in any container with
   CPython ≥ 3.12.
4. **Skills are already the open Agent Skill format** ([SKILLS.md](SKILLS.md)
   §1) — the same format Harbor hands agents via `skills_dir`.

What is missing, in all options:

- **A headless single-shot entry point.** There is no `kodo <prompt>` today.
  `python -m kodo` prints prompts and tool payloads; `kodo-validator` runs a
  *scenario* and drags in judge/LUT/local-model machinery. Harbor needs
  "one prompt, one workspace, one result document, an exit code".
- **A machine-readable run result.** `TurnResult` is an in-process dataclass;
  nothing writes a stable JSON result file.
- **A model-selection mapping** from Harbor's `provider/model` string onto
  Kōdo's `settings.json` shape.
- **No MCP support** (planned post-MVP) → `AgentCapabilities(mcp_servers=False)`.
- **No global kill switch for the web tools.** `web_search` / `read_webpage` /
  `query_search_engine` are granted purely by agent frontmatter; there is no
  setting that disables them. In a network-disabled Harbor task they fail at
  call time rather than being withheld (see §10, R4).

---

## 4. The options

### Option A — Installed agent: Kōdo runs **inside** the task container

`KodoAgent(BaseInstalledAgent)`. `install()` puts `py-kodo` in the container;
`run()` executes a new headless Kōdo runner there; `populate_context_post_run()`
parses the synced transcript into `AgentContext`.

```
Harbor host                        task container
┌───────────────┐                  ┌──────────────────────────────────────┐
│ harbor run    │  exec_as_agent   │ kodo-headless --prompt … --root /app │
│  KodoAgent    │ ───────────────▶ │   └─ spawns python -m kodo.server    │
│   .run()      │                  │      └─ WS 127.0.0.1:<free port>     │
│               │ ◀─ logs synced ─ │   writes result.json + transcript    │
└───────────────┘                  └──────────────────────────────────────┘
                                        ▲ model API egress from container
```

**For**
- Kōdo's tools run natively against the task filesystem. `run_command`,
  `find_text_in_files`, `filesystem`, the git shadow mirror
  (`kodo.mirror.ShadowMirror`), checkpoints, `session_temp_dir`, skills — all
  work unmodified. **Zero semantic drift** between what Harbor measures and
  what a Kōdo user gets.
- No change whatsoever to `kodo.tools`, `kodo.runtime`, `kodo.security`.
- Harbor's recommended path; the one its retry/error taxonomy is built around.
- Isolation, network policy, resource limits, concurrency: Harbor's problem.

**Against**
- **Install footprint per trial.** CPython ≥ 3.12 (many task images ship 3.9–3.11
  → install a standalone interpreter via `uv`), plus ten runtime dependencies,
  several heavy (`anthropic`, `openai`, `boto3`, `playwright`, `huggingface_hub`,
  `curl_cffi`, `selectolax`, `nvidia-ml-py`). Cold install is minutes;
  amortizable with a pre-baked image or a wheel cache, never free.
- **Egress from inside the container.** Kōdo calls the model API from the
  container, so tasks with `network_policy` disabled or allowlisted need the
  provider host allowlisted. Harbor supports this, but it is per-task
  configuration we do not control on Hub datasets.
- **Local inference is effectively out.** llama.cpp + a multi-GB GGUF inside a
  per-trial container is impractical. Mitigated by Option C, not by A alone.
- Container ↔ host clock/timeout coordination: Kōdo's turn timeout must be set
  under Harbor's agent timeout or Harbor kills the process mid-write.

### Option B — External agent: Kōdo runs **on the host**, the container is its filesystem

`KodoAgent(BaseAgent)` plus a Kōdo-side execution backend so every tool that
touches a file or spawns a process routes through the injected
`BaseEnvironment`.

```
Harbor host                                        task container
┌────────────────────────────────────────┐         ┌──────────────┐
│ harbor run → KodoAgent.run()           │         │              │
│   └─ kodo.server (host, own HOME/port) │  exec   │   /app       │
│        └─ ToolDispatcher               │ ──────▶ │   files      │
│             └─ RemoteBackend ──────────┼────────▶│   processes  │
│        └─ LLMGateway → local llama /   │ up/down │              │
│           cloud API (host credentials) │ ◀─────▶ │              │
└────────────────────────────────────────┘         └──────────────┘
```

**For**
- **Nothing is installed in the task container.** Any image, any Python version,
  any language. Works against images we could never modify.
- **Credentials and local inference stay on the host.** This is the arm the
  benchmarking work actually wants: a local GGUF driving a containerized task,
  with no model weights or API keys anywhere near task code.
- Kōdo gains a genuinely reusable capability — remote/sandboxed execution —
  that is worth having independent of Harbor (SSH targets, devcontainers,
  "run this agent against my staging box").

**Against**
- **This is the expensive one.** Today every tool does direct local I/O. The
  seam spans, at minimum:
  `tools/_run_command.py` (`asyncio.create_subprocess_shell`),
  `tools/_filesystem.py` (12 direct path/FS sites — the densest),
  `tools/_edit_file.py`, `tools/_create_file.py`, `tools/_create_directory.py`,
  `tools/_read_file.py`, `tools/_find_files.py`, `tools/_find_text_in_files.py`,
  `tools/_search.py`, `tools/_toolchain_build.py`, `tools/_toolchain_deps.py`,
  `tools/_paths.py` (`LogicalPathResolver` resolves against host paths),
  plus `kodo.mirror.ShadowMirror` (git in the work tree → git must exist *in the
  container*, and checkpoints/rollback become remote operations) and the
  workspace-containment checks in `kodo.security`.
- **Two code paths forever.** Local and remote backends must stay behaviorally
  identical or Harbor stops measuring the shipped product. That is a standing
  test burden, not a one-time cost.
- **Latency.** Every `read_file`/`edit_file`/`find_text_in_files` becomes a
  `docker exec` round trip (~50–150 ms). A Guide-mode run makes thousands of
  tool calls; at 100 ms that is minutes of pure overhead per trial. Mitigable
  (batched transfers, a persistent exec channel) but never zero.
- Concurrency ceiling with local inference: N host-side Kōdo processes each run
  their own `LLMGateway`, whose local feed is `max_slots = 1`
  ([LLM_GATEWAY.md](LLM_GATEWAY.md)) — but they all share one physical
  llama-server. Effective parallelism for a local-model arm is 1 regardless of
  Harbor's `-n`.

### Option C — Hybrid: Kōdo in the container, models on the host

Option A plus `--agent-env KODO_MODEL_URL=http://<host-gateway>:<port>/v1`,
mapped onto a Kōdo local-registry entry of kind `custom_server_url`
([LLM_REGISTRY.md](LLM_REGISTRY.md) §"`custom_server_url` is not managed by kodo
at all"). Kōdo in the container does inference against a host llama-server (or
a host proxy for cloud traffic).

**For** — recovers the local-model arm without touching `kodo.tools`; keeps all
of A's fidelity; one shared llama-server is honest about the concurrency limit
instead of hiding it.
**Against** — requires a task network policy that permits the host gateway; the
single llama-server still serializes trials; a shared endpoint is a
cross-trial contamination surface (one wedged trial stalls the queue).

### Option D — ACP bridge: teach Kōdo the Agent Client Protocol

Harbor ships a generic `acp` installed agent that runs any agent from the ACP
registry, and accepts a local entry via
`--agent-kwarg registry_entry_path=/path/to/agent.json`. If Kōdo exposed an ACP
stdio server, Harbor would need **no Kōdo-specific code at all**.

**For** — one protocol buys Harbor *and* Zed *and* every other ACP client;
Harbor's ACP integration already emits `acp-events.jsonl`, `acp-summary.json`
and an ATIF `trajectory.json` we would otherwise have to write ourselves.
**Against** — a second front door into the engine, permanently. Kōdo's WS
protocol is a near-superset of ACP's surface (sessions, streamed output,
permission requests) with different shapes for all of them; an ACP façade is
a real translation layer, not a shim, and it buys nothing Option A does not
already give us for Harbor specifically. ACP agents also do **not** receive
provider credentials automatically. Park it; revisit if ACP clients become a
product goal in their own right.

### Comparison

| | A — Installed | B — External | C — Hybrid | D — ACP |
|---|---|---|---|---|
| Kōdo core changes | none | large (11 tool modules + resolver + mirror) | none | medium (ACP façade) |
| New Kōdo code | headless runner + adapter | headless runner + adapter + backend tier | A + model-URL mapping | ACP server + registry entry |
| Harbor changes | none | none | none | none (local registry entry) |
| Fidelity to shipped Kōdo | exact | two code paths to keep equal | exact | exact |
| Works on unmodifiable images | no | **yes** | no | no |
| Local GGUF arm | no | **yes** | yes (shared server) | no |
| Per-trial install cost | high (cacheable) | **none** | high (cacheable) | high |
| Tool-call latency overhead | none | ~50–150 ms × thousands | none | none |
| Credentials in task container | yes | **no** | no (URL only) | yes |
| Time to first green trial | **days** | weeks | days + network plumbing | weeks |

### Recommendation

**Ship A. Design for B. Use C for the local-model arm. Park D.**

A is days of work, needs no engine change, and is the only option that is
provably measuring the shipped product. It also produces the two artifacts every
other option needs anyway — the headless runner and the JSON result document —
so no work is thrown away if B follows.

B is the strategically interesting one, and the only one that makes the
local-model ablation ladder practical, but it is a refactor of the T2 tool tier
and should be justified by a decision to support remote execution *as a Kōdo
feature*, not by Harbor alone. Phase it behind A.

---

## 5. Where `BaseEnvironment` fits

There are three distinct things "implement `BaseEnvironment`" can mean. They are
not alternatives to each other; they answer different questions.

**Reading 1 — consume it.** Option B: Kōdo's tools act on a Harbor-supplied
sandbox. Kōdo implements nothing; it *calls* `exec`/`upload_*`/`download_*`.
This is the reading the brief most likely intends, and §7 sketches it.

**Reading 2 — implement a Kōdo sandbox provider.**
`KodoLocalEnvironment(BaseEnvironment)`: no container at all. `start()` creates
a workspace directory and takes a `ShadowMirror` checkpoint; `exec()` runs
locally; `stop(delete=True)` rolls the mirror back to the checkpoint instead of
destroying a container.

*Value*: fast local iteration on tasks (no image build), and — because the
rollback is Kōdo's own checkpoint machinery — a reset that is exactly as strong
as Kōdo's undo. *Caveat, to state loudly*: **no isolation**. Untrusted task code
runs as the invoking user. It is a development convenience, never a substitute
for Docker on a Hub dataset. Declare
`EnvironmentCapabilities()` with nothing enabled and reject compose tasks in
`_validate_definition()`.

**Reading 3 — implement a gating decorator environment (the interesting one).**
`GatedEnvironment(BaseEnvironment)` wraps another environment (Docker by
default) and routes every `exec()` through `kodo.security` before delegating:

```python
decision = self.__layer.evaluate(tool_name="run_command", tool_input={"command": command}, ...)
```

`kodo.security` is a near-leaf package (`common`, `toolspecs`, `shellparser`,
`project`), performs no I/O, consults no LLM, and is fully deterministic — so it
can judge commands with no engine present. In `audit` mode the decision is
recorded and the command runs anyway; in `enforce` mode `ask`/`deny` becomes a
non-zero `ExecResult`.

*Value*: it answers a question nothing else can — **"how often does a frontier
agent issue a command Kōdo's security layer would have stopped?"** Run
`-a claude-code -e kodo.harbor:GatedEnvironment --ek mode=audit` over a
Terminal-Bench dataset and the output is a distribution of real agent traffic
against our rule table, with task success as the control for false positives.
That is a defensible, publishable measurement of a Kōdo component, produced
with ~200 lines of adapter and no Kōdo core change.

**Recommendation:** implement Reading 3 as an optional, clearly-scoped extra
(§8 phase 4). Implement Reading 2 only if task-authoring iteration speed becomes
a real complaint. Reading 1 is Option B and is governed by that decision.

---

## 6. Design — the recommended path (Option A)

### 6.1 Package layout and layering

Two new packages, deliberately split so the one that ships into task containers
never imports Harbor:

```
src/kodo/headless/          # NEW — runs anywhere: container, host, CI
  __init__.py               #   HeadlessRun, RunOutcome, RunResult
  py.typed
  _run.py                   #   HeadlessRun: server + client + one prompt
  _result.py                #   RunResult / RunOutcome — the JSON contract
  _user.py                  #   NonInteractiveUser (UserSimulator impl)
  _settings.py              #   ModelSelection → settings.json overrides
  __main__.py               #   `kodo-headless` console script

src/kodo/harbor/            # NEW — imports harbor; never installed in a task
  __init__.py               #   KodoAgent, KodoAgentOptions
  py.typed
  _agent.py                 #   KodoAgent(BaseInstalledAgent)
  _options.py               #   KodoAgentOptions(InstalledAgentOptions)
  _context.py               #   RunResult → AgentContext
  _environment.py           #   phase 4: GatedEnvironment(BaseEnvironment)
```

Layering (extends the table in [INTERNALS.md](INTERNALS.md) §2.1):

- `kodo.headless` imports **only** `kodo.common` + `kodo.transport` — the same
  client-side discipline `kodo.validator` holds ([VALIDATOR.md](VALIDATOR.md)
  §2): it must never import `runtime`, `llms`, `agents` or `server` internals,
  so protocol drift breaks it loudly. Enforced by
  `grep -rE "^\s*(from|import) kodo\.(runtime|llms|agents|server|tools)" src/kodo/headless`
  being empty (the `python -m kodo.server` subprocess is spawned by name, not
  imported).
- `kodo.harbor` imports `harbor` + `kodo.headless` (+ `kodo.security` in phase 4)
  and **nothing else from Kōdo**. Nothing in Kōdo imports `kodo.harbor`.
- `kodo.validator` should later be refactored onto `kodo.headless` rather than
  duplicating it (§8 phase 5) — but not before Harbor trials are green, so the
  validator is never destabilized by this work.

Dependency on Harbor is an **extra**, not a runtime dependency:

```toml
[project.optional-dependencies]
harbor = ["harbor>=0.1"]   # pin at implementation time; PyPI name is `harbor`
```

`pip install py-kodo[harbor]` in the Harbor host environment;
plain `pip install py-kodo` in the task container. (Alternative considered and
rejected: a separate `kodo-harbor` distribution. It decouples release cadence
but splits the adapter from the protocol client it must stay in lockstep with —
exactly the drift `kodo.validator`'s no-internals rule exists to prevent.)

### 6.2 `kodo.headless` — the single-shot runner

The deliverable is a CLI with a stable contract, usable by Harbor, by CI, and by
hand:

```bash
kodo-headless \
  --prompt-file /harbor/agent/instruction.txt \
  --root /app \
  --agent problem_solver \
  --autonomous \
  --turn-timeout 3000 \
  --result /harbor/agent/kodo-result.json \
  --transcript /harbor/agent/transcript.jsonl
```

Sequence, all of it protocol calls that already exist:

1. `ServerProcess`-equivalent: spawn `python -m kodo.server --port <free>`,
   wait for the port, stream its console log to the log dir.
2. `hello` (`MSG_HELLO`) with `client: "kodo-headless"` → `hello.ack` gives
   `session_id` and the served agent catalog.
3. `agent.set` (`MSG_AGENT_SET`) to the requested top-level agent — default
   `problem_solver`; `guide` is the multi-sub-agent pipeline and should be a
   separate Harbor agent row, not a hidden default.
4. `workspace.folders` (`MSG_WORKSPACE_FOLDERS`) binding `--root` (repeatable;
   multi-root is supported and is a Kōdo differentiator worth measuring).
5. `mode.set` (`MSG_MODE_SET`) `{autonomous: true}`.
6. `prompt.submit` (`MSG_PROMPT_SUBMIT`) with the instruction; then wait for the
   turn boundary exactly as `ValidatorClient.wait_turn_end` does (resting phase
   + settle window), bounded by `--turn-timeout`.
7. Answer every server→client request from a `NonInteractiveUser`:
   `SREQ_API_KEY_REQUEST` → the key from the process environment for that
   vendor; `SREQ_PROMPT_PERMISSION` → allow (autonomous is already permissive —
   this is belt and braces); `SREQ_PROMPT_APPROVAL` → accept;
   `SREQ_PROMPT_QUESTION` → a fixed "proceed with your best judgement, do not
   ask again" answer, **counted** in the result (an autonomous agent that still
   asks is a finding, not an error); `SREQ_PROMPT_EDIT_REVIEW`,
   `SREQ_PROMPT_STUCK_ALERT`, `SREQ_PROMPT_CHOOSE_PROJECT_FOLDER`,
   `SREQ_WORKSPACE_CONFIRM_FOLDER`, `SREQ_HF_TOKEN_REQUEST` → deterministic
   non-blocking defaults.
8. `session.release` + `server.shutdown`, then write `RunResult` and exit with a
   code from `RunOutcome`.

`RunResult` (the JSON contract — versioned, and the *only* thing the adapter
parses):

```python
@dataclass(frozen=True)
class RunResult:
    schema_version: int          # 1
    outcome: str                 # RunOutcome: completed | timeout | stopped |
                                 #   server_error | llm_error | crashed
    session_id: str
    agent: str                   # top-level agent that ran
    final_phase: str
    assistant_text: str
    cumulative_input_tokens: int
    cumulative_input_tokens_uncached: int
    cumulative_output_tokens: int
    cumulative_usd: float
    per_model: dict[str, ModelUsageRecord]   # from each usage.update
    tool_calls: int
    tool_call_errors: int
    questions_asked: int
    autonomous_disabled: bool    # engine dropped autonomous mid-run
    nudges: int                  # stuck-watchdog course corrections
    wall_seconds: float
    error: str | None
```

Exit codes: `0` completed, `2` timeout, `3` LLM/provider error, `4` server
failed to start, `5` crashed. Harbor's `ERROR_PATTERNS` matches on stdout text,
so the runner also prints a single machine-greppable line
(`KODO-RESULT outcome=… error=…`) before exiting — that is what lets
`ApiRateLimitError` / `ContextWindowExceededError` classification work without
Kōdo-specific Harbor code.

### 6.3 `KodoAgent(BaseInstalledAgent)`

```python
class KodoAgent(BaseInstalledAgent):
    """Runs Kōdo inside the task environment as a single autonomous turn."""

    capabilities = AgentCapabilities(skills=True, mcp_servers=False, windows=False)
    options_model = KodoAgentOptions

    @staticmethod
    def name() -> str:
        return "kodo"

    def version(self) -> str | None:
        return self.__installed_version        # captured during install()

    async def install(self, environment: BaseEnvironment) -> None:
        # 1. ensure_system_dependencies(): python3, python_pip, python_venv, git, curl
        # 2. `uv python install 3.12` when the image's python3 is older
        # 3. pip install "py-kodo==<version>" into /opt/kodo-venv
        # 4. seed ~/.kodo/etc/settings.json from the model selection (§6.4)
        # 5. copy self.skills_dir → ~/.kodo/skills (§6.6)

    @with_prompt_template
    async def run(self, instruction, environment, context) -> None:
        # write the instruction to a file (never interpolate it into a shell line),
        # exec_as_agent: /opt/kodo-venv/bin/kodo-headless --prompt-file … --root <cwd>
        #   --result <environment_logs_dir>/kodo-result.json
        #   --transcript <environment_logs_dir>/transcript.jsonl
        #   --turn-timeout <self.options.turn_timeout_sec>

    def populate_context_post_run(self, context: AgentContext) -> None:
        # parse self.logs_dir/kodo-result.json (synced by Harbor) → §6.5
```

`KodoAgentOptions(InstalledAgentOptions)` — every field with a `Field(description=…)`
so `harbor agent schema kodo.harbor:KodoAgent` documents itself:

| kwarg | default | meaning |
|---|---|---|
| `top_agent` | `problem_solver` | which top-level agent (`agent.set`) |
| `turn_timeout_sec` | `3000` | Kōdo-side turn bound; **must** be < Harbor's agent timeout |
| `autonomous` | `true` | leaving it settable makes the interactive-gate cost measurable |
| `model_url` | `None` | OpenAI-compatible base URL → `custom_server_url` local entry (Option C) |
| `thinking_level` | `None` | tier slug for the active local model |
| `settings_json` | `None` | inline JSON deep-merged into `etc/settings.json` — escape hatch |
| `install_ref` | `None` | `py-kodo` version/spec; default = the adapter's own version |

The instruction goes to the container **as a file**, never as a shell argument —
Harbor instructions routinely contain quotes, newlines and backticks, and
`exec()` takes a single command string.

### 6.4 Model and credential mapping

Harbor gives `model_name` as `provider/model` (e.g. `anthropic/claude-sonnet-5`)
and supplies credentials via `--agent-env`, which arrive in `self.extra_env`.
Kōdo's model selection lives in `~/.kodo/etc/settings.json`
([SETTINGS.md](SETTINGS.md)):

| Harbor `-m` | Kōdo settings |
|---|---|
| `anthropic/claude-sonnet-5` | `mode: "cloud"`, `active_cloud_vendor: "anthropic"`, `models.cloud_uniform.anthropic: {enabled: true, model_id: "claude-sonnet-5"}` |
| `openai/gpt-5.6-sol` | same shape, vendor `openai` |
| `openrouter/<any id>` | `models.cloud.openrouter.<tier>` — the catalog is fetched at runtime, so any of OpenRouter's ids works with no Kōdo code change |
| `amazon-bedrock/us.anthropic.…` | vendor `bedrock`; AWS credentials from `extra_env` |
| *(with `--ak model_url=…`)* | `mode: "local"` + a `custom_server_url` registry entry pointing at that base URL (Option C) |

`models.cloud_uniform.<vendor>` is the right key, not `models.cloud.<vendor>.<effort>`:
it pins **one** model across all four effort tiers, so a sub-agent that declares
a different `capability` cannot silently change which model Harbor thinks it is
measuring. A vendor Kōdo does not know is a `preflight()` failure with the list
of supported vendors — caught before trials are queued, not on trial 400.

API keys reach Kōdo through `api_key.request` (§6.2 step 7), resolved from the
process environment by vendor (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, the AWS triple, …) — the same names Harbor's
`PROVIDERS` table uses, so `--agent-env` values land where Kōdo looks with no
translation. Keys are never written to `settings.json` and never appear in a
command line (Harbor redacts `Cli(sensitive=True)` values; environment variables
are not printed at all).

`MODEL_CONNECTION` should be declared so `to_agent_info()` reports the resolved
provider in the trial result — free metadata, one class attribute.

### 6.5 Filling `AgentContext`

```python
context.n_input_tokens  = result.cumulative_input_tokens          # includes cache, per Harbor's semantics
context.n_cache_tokens  = result.cumulative_input_tokens - result.cumulative_input_tokens_uncached
context.n_output_tokens = result.cumulative_output_tokens
context.cost_usd        = result.cumulative_usd
context.model_usage     = {name: ModelUsage(**rec) for name, rec in result.per_model.items()}
context.metadata        = {
    "kodo_version": …, "session_id": …, "top_agent": …, "final_phase": …,
    "outcome": …, "tool_calls": …, "tool_call_errors": …,
    "questions_asked": …, "autonomous_disabled": …, "nudges": …,
    "wall_seconds": …,
}
```

Two deliberate omissions:

- **`rollout_details` is left `None`.** It wants prompt/completion token ids,
  logprobs and loss masks — for RL training pipelines. Cloud providers do not
  return them; a local llama-server could, but Kōdo does not currently surface
  token ids through `usage.update`. If a local-inference RL story ever matters,
  this is the hook, and it is a `kodo.llms` change, not an adapter change.
- **ATIF (`capabilities.atif=False`) initially.** A `transcript.jsonl` → ATIF
  `trajectory.json` converter unlocks Harbor's trajectory viewer, `--load-trajectory`
  and handoff. Worth doing (phase 4) and cheap, but it needs the ATIF schema read
  properly first (`/core-concepts/agents/atif`), which this document has not done.

`metadata` is where Kōdo's differentiators become measurable across a whole
dataset: `questions_asked > 0` in autonomous mode, `autonomous_disabled`,
`nudges`, and the per-sub-agent `model_usage` split that answers "did the
Planner sub-agent pay for itself" — a question the existing transcripts can
already answer but nothing currently aggregates.

### 6.6 Skills, MCP, and capability declarations

- **Skills**: Harbor passes `skills_dir` (a host path) and expects the agent to
  make them discoverable. Kōdo reads `~/.kodo/skills` in the open Agent Skill
  format, so `install()` copies the directory in and every skill is surfaced by
  the existing progressive-disclosure path ([SKILLS.md](SKILLS.md)) with no
  conversion. Declare `AgentCapabilities(skills=True)` — a capability most
  integrations cannot claim.
- **MCP**: Kōdo has no MCP client. `mcp_servers=False`, and `setup()` logs a
  warning when `self.mcp_servers` is non-empty rather than silently ignoring it.
- **Windows**: `windows=False` (the WS server is cross-platform, but Kōdo's
  Windows-specific process handling has never been exercised in a Windows task
  container; claiming it untested would be false).
- **`resume` / `load` / `handoff`**: `False` initially. `resume` is plausible
  later — Kōdo sessions *are* resumable (`hello` with `session_id`) — and would
  unlock Harbor's multi-step tasks. Note it as future work, do not half-ship it.

---

## 7. Option B, sketched (the seam, if and when it is funded)

The refactor has one shape that fits the existing architecture. `kodo.tools`
already expresses every higher-tier collaborator as a structural `Protocol`
injected by `runtime` (`GateLike`, `SessionLike`, `EngineServices` in
`tools/_context.py`). Execution is simply one more:

```python
# src/kodo/tools/_backend.py  (T2, no new imports)
class ExecutionBackend(Protocol):
    """Where a tool's file and process operations actually happen."""

    async def exec(self, command: str, cwd: str, timeout: float) -> CommandResult: ...
    async def read_bytes(self, path: str) -> bytes: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> tuple[DirEntry, ...]: ...
    async def stat(self, path: str) -> FileStat | None: ...
    async def remove(self, path: str, *, recursive: bool) -> None: ...
    async def move(self, source: str, target: str) -> None: ...
```

- `LocalBackend` reproduces today's behavior exactly and stays the default —
  no behavior change for the product.
- `ToolContext` gains a `backend` field; the eleven tool modules in §4/Option B
  switch from `Path`/`open`/`create_subprocess_shell` to backend calls.
- `LogicalPathResolver` keeps doing containment logic on *strings*; only the
  final I/O moves.
- `kodo.mirror.ShadowMirror` takes the backend too — checkpoints then work in
  the container (requiring `git` there) or degrade explicitly when it is absent.
- `kodo.harbor` supplies `EnvironmentBackend`, a ~150-line adapter over
  `BaseEnvironment.exec` / `upload_file` / `download_file`.

**The rule that keeps this honest**: one shared behavioral test suite,
parametrized over both backends, with the remote one running against a real
container in CI. If the suites ever diverge, Harbor has stopped measuring the
shipped product and the numbers are worthless.

Realistic first estimate: 2–3 weeks for the seam plus the equalization suite,
which is why it is phase 6 and not phase 1.

---

## 8. Implementation plan

Each phase ends in something runnable. No phase depends on a Harbor change.

**Phase 1 — `kodo.headless` (no Harbor at all).**
`HeadlessRun`, `RunResult`/`RunOutcome`, `NonInteractiveUser`, `kodo-headless`
console script, `[project.scripts]` entry, module docstrings, `py.typed`,
`__all__`. Done when `kodo-headless --prompt "create hello.py that prints hi"
--root /tmp/x` exits 0, the file exists, and `kodo-result.json` validates.
*Ships value on its own*: Kōdo becomes scriptable from CI with no VS Code.

**Phase 2 — `KodoAgent` against `hello-world`.**
`_options.py`, `_agent.py`, `_context.py`, the `harbor` extra.
Done when
`harbor run -t hello-world/hello-world -a kodo.harbor:KodoAgent -m anthropic/claude-sonnet-5`
produces a passing trial whose result carries non-zero tokens and a cost.

**Phase 3 — a real dataset + the control arm.**
Terminal-Bench 2.0 (or a ~20-task slice), run twice: `-a kodo.harbor:KodoAgent`
and `-a claude-code`, same model, same sandbox, same `-k`. This is the first
number Kōdo has ever produced with a control arm and a non-Kōdo judge. Record
install-time overhead per trial and decide on a pre-baked image.
Also: a `KODO_*` settings switch to withhold the web tools when the task network
policy is not `public` (§10, R4).

**Phase 4 — instrumentation extras (independent, parallelizable).**
(a) `transcript.jsonl` → ATIF converter, `capabilities.atif=True`.
(b) `GatedEnvironment` (§5, Reading 3) with `mode=audit|enforce`, plus a small
aggregator that turns a job's audit records into a security-rule report.
(c) `resume` support for Harbor multi-step tasks, via `hello {session_id}`.

**Phase 5 — fold `kodo.validator` onto `kodo.headless`.**
Remove the duplicated client/server/user plumbing; the validator keeps scenario,
suite, judge and local-model concerns. Strictly after phase 3 is green.

**Phase 6 — Option B, if funded.** §7, gated on a product decision about remote
execution, not on Harbor.

**Documentation and memory obligations** (same task as the code, per the repo's
working rules): a new `doc/HARBOR.md` (operator-facing: install, run, kwargs,
result schema), a `kodo.headless` section in [INTERNALS.md](INTERNALS.md) §2.1's
import matrix, a [DEPENDENCIES.md](../DEPENDENCIES.md) note for the extra, and a
project-memory entry recording the wire contract (`RunResult.schema_version`,
exit codes, `KODO-RESULT` line) because none of it is derivable from a diff.

---

## 9. Testing

| What | How |
|---|---|
| `RunResult` schema | round-trip + a golden JSON fixture; bump `schema_version` deliberately |
| Gate answering | a fake WS server emits every `SREQ_*` in turn; assert the runner never blocks and every answer is well-formed |
| Turn-boundary detection | recorded frame fixtures (including a mid-turn `error` and a `stuck_critical`) replayed through the client |
| Model mapping | table-driven: `provider/model` + kwargs → expected `settings.json` deep-merge; unknown vendor → `preflight()` raises |
| `AgentContext` fill | `RunResult` fixtures → assert each field, including `n_cache_tokens` subtraction and `model_usage` keys |
| Adapter without Harbor installed | `kodo.harbor` import is guarded in tests with `pytest.importorskip("harbor")`; the rest of the suite must not require the extra |
| End-to-end | `hello-world` trial in CI, nightly, against one cheap cloud model |
| Layering | `grep -rE "^\s*(from\|import) kodo\.(runtime\|llms\|agents\|server\|tools)" src/kodo/headless` is empty; nothing in `src/kodo` outside `kodo/harbor` imports `harbor` |

House rules apply throughout: behavior-level tests only, no private members, no
hardcoded assumptions about which tool has which spec property.

---

## 10. Risks and open questions

**R1 — Harbor is a moving target.** The repo carries dated changelog entries and
deprecated-alias shims (`SUPPORTS_*` → `capabilities`), i.e. a live API. Pin the
`harbor` extra to a tested minor version and treat an upgrade as a task.

**R2 — Install cost dominates short tasks.** On a task whose solution is a
two-line patch, minutes of `pip install` per trial is most of the wall time.
Mitigation order: wheel cache → pre-baked image published alongside releases →
`uv` instead of `pip`. Measure in phase 3 before optimizing.

**R3 — Timeout coordination.** If Kōdo's turn timeout ≥ Harbor's agent timeout,
Harbor kills the process before `kodo-result.json` is written and the trial
reports nothing. `preflight()` cannot see Harbor's timeout, so document the
constraint and default `turn_timeout_sec` conservatively.

**R4 — Web tools in a network-restricted task.** `web_search`, `read_webpage`
and `query_search_engine` are granted by agent frontmatter with no settings-level
kill switch today, and `read_webpage` additionally wants a Playwright browser
the container will not have. In a `network_policy: disabled` task they fail at
call time and burn turns. Phase 3 adds the switch; until then, run with a
top-level agent whose frontmatter excludes them.

**R5 — Local inference concurrency.** One llama-server, `max_slots = 1` on the
gateway's local feed. A local-model arm runs effectively serially whatever `-n`
says. Plan wall-clock accordingly, and never compare a local arm's throughput
with a cloud arm's.

**R6 — What does "success" mean for Guide mode?** Harbor tasks are verified by
tests. Kōdo's Guide pipeline produces specs, reviews and plans as real files
under `specs/` on the way to code. A task whose verifier only runs `pytest`
scores that work at zero. Two Harbor agent rows (`problem_solver`, `guide`) and
an explicit acknowledgement that Guide mode is being measured on an axis it was
not designed for — or a Kōdo-authored dataset that verifies the artifacts too.

**Open questions**
1. Which corpus first — Terminal-Bench 2.0 (external credibility, hard) or a
   Kōdo-authored dataset that reuses the existing `toolchain_*` scenario content
   with real program verifiers (cheap, already calibrated to small models)? The
   benchmarking analysis argued the latter is the highest-value first move; this
   plan is agnostic and works for both.
2. Publish results to the Harbor Hub leaderboard, or keep runs private? Affects
   nothing technically; decide before phase 3 reports numbers.
3. Do we want `resume` (multi-step tasks) in the first release, or is single-turn
   enough to get a number?

---

## 11. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Ship Option A (installed agent) first | days, not weeks; no engine change; measures the shipped product exactly |
| D2 | Build `kodo.headless` as its own package, no Harbor import | needed by every option; ships value standalone; keeps the container payload Harbor-free |
| D3 | `kodo.harbor` imports only `harbor` + `kodo.headless` | one direction, one tier; nothing in Kōdo depends on Harbor |
| D4 | `harbor` is an optional extra, not a runtime dependency | task containers must not carry it |
| D5 | Never upstream-gated | custom agents load by `module:Class`; a Harbor PR is optional forever |
| D6 | The instruction is passed as a file, never a shell argument | task instructions contain quotes, newlines, backticks |
| D7 | `RunResult` JSON is the only adapter↔runner contract, versioned | lets both sides evolve; keeps parsing out of shell text |
| D8 | Model selection maps to `models.cloud_uniform.<vendor>` | pins one model across effort tiers so sub-agents cannot change what is being measured |
| D9 | `rollout_details` left unpopulated; ATIF deferred to phase 4 | needs token ids Kōdo does not surface / a schema not yet read |
| D10 | `BaseEnvironment` implemented as `GatedEnvironment` (audit/enforce), not as a sandbox provider | measures Kōdo's security layer against real agent traffic; a Kōdo sandbox adds no isolation Docker does not already give |
| D11 | Option B is phase 6, gated on a remote-execution product decision | ~11 tool modules + mirror + resolver, two permanent code paths, per-call latency |
| D12 | ACP (Option D) parked | a second permanent front door; buys nothing Option A does not, for Harbor specifically |
| D13 | `kodo.validator` is refactored onto `kodo.headless` only after Harbor trials are green | never destabilize the existing instrument to build the new one |
