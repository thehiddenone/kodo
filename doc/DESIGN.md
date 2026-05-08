# Kodo MVP — Design

> Status: draft for review. Reference: [REQUIREMENTS.md](REQUIREMENTS.md). Each design section maps back to FR/NFR IDs.

---

## 1. Architecture overview

Two processes, one project:

```
+----------------------------------+        WebSocket        +-----------------------------+
|  VS Code (extension host)        |  ws://127.0.0.1:<p>     |  Kodo Server (Python)       |
|                                  | <---------------------> |                             |
|  - Activation / lifecycle        |   JSON envelopes        |  - WebSocket transport      |
|  - WebView (Preact + Vite)       |   token streaming       |  - Workflow engine          |
|  - SecretStorage (API token)     |                         |  - Agent registry           |
|  - Diff editor integration       |                         |  - LLM plugin (Anthropic)   |
|  - STOP control                  |                         |  - Toolchain plugins        |
|                                  |                         |  - Security layer           |
+----------------------------------+                         |  - Mirror (git)             |
                                                             |  - In-process MCP servers   |
                                                             |    * tools/fileio           |
                                                             |    * tools/shell            |
                                                             +-----------------------------+
                                                                          |
                                                                  ~/.kodo/transient/
                                                                  <project>/.kodo/
```

Single project per server (FR-SRV-02). One WebSocket connection per server, on a loopback port chosen by the extension at activation time (FR-SRV-03/04, FR-VSIX-03) — each VS Code window picks its own free port so multiple windows can run Kodo concurrently. Single async worker (FR-WF-02). MCP servers run in-process to avoid extra processes for MVP — they expose the MCP wire format inside Python coroutines, not stdio.

The Kodo panel (WebView) is a *view* onto state owned by the extension host: the WebSocket connection and the cached agent/conversation state persist for the lifetime of the VS Code window, independent of whether the panel is open. Closing the panel does not tear down the connection; reopening it rehydrates the WebView from the cached state and resumes live updates (FR-VSIX-06).

### 1.1 Server-side module layout

```
src/kodo/
├── server/                 # entry point, lifecycle, WS endpoint
│   ├── __main__.py
│   ├── _app.py             # asyncio app + WS handler
│   ├── _lifecycle.py       # PID file, graceful shutdown, signal handling
│   └── _config.py          # CLI args, settings loader, precedence
├── transport/              # wire protocol
│   ├── _envelope.py        # {kind, id, correlation_id, payload}
│   ├── _messages.py        # message catalog (typed payloads)
│   ├── _outbox.py          # disconnect-tolerant outbound queue
│   └── _ws.py              # aiohttp WebSocket binding
├── workflow/
│   ├── _engine.py          # async queue, single worker, stage machine
│   ├── _stages.py          # stage definitions + transitions
│   ├── _gates.py           # approval gate orchestration
│   ├── _scheduler.py       # component scheduling, integration DAG
│   └── _session.py         # per-session metadata, resume logic
├── agents/                 # markdown agent files; one file per (name, model)
│   ├── _loader.py          # parses frontmatter + body into Agent dataclass
│   ├── _registry.py        # (name, model) -> Agent
│   ├── narrative_author.claude-sonnet-4-6.md
│   ├── architect.claude-sonnet-4-6.md
│   ├── requirements_author.claude-sonnet-4-6.md
│   ├── requirements_reviewer.claude-sonnet-4-6.md
│   ├── functional_designer.claude-sonnet-4-6.md
│   ├── functional_design_critic.claude-sonnet-4-6.md
│   ├── test_designer.claude-sonnet-4-6.md
│   ├── test_design_critic.claude-sonnet-4-6.md
│   ├── test_coder.claude-sonnet-4-6.md
│   ├── coder.claude-sonnet-4-6.md
│   ├── code_reviewer.claude-sonnet-4-6.md
│   └── dev_proxy.claude-haiku-4-5-20251001.md
├── llms/
│   ├── _interface.py       # LLMPlugin ABC
│   └── anthropic/
│       ├── _claude.py      # Claude implementation
│       ├── _cache.py       # cache_control breakpoint logic
│       ├── _retry.py       # 2/8/32s backoff
│       └── _usage.py       # token + dollar accounting
├── toolchains/
│   ├── _interface.py       # ToolchainPlugin ABC
│   ├── python/
│   │   ├── _plugin.py      # init/build/test/format
│   │   └── _pytest.py      # TestResult parsing
│   └── node/
│       ├── _plugin.py
│       └── _vitest.py
├── mcp/
│   ├── _interface.py       # in-process MCP server contract
│   └── _registry.py
├── tools/
│   ├── fileio/             # in-process MCP server
│   └── shell/              # in-process MCP server
├── security/
│   ├── _layer.py           # gate every tool call
│   ├── _rules.py           # rule schema, regex matcher
│   ├── _store.py           # session + global stores
│   └── _defaults.py        # built-in ruleset
├── mirror/
│   ├── _repo.py            # git porcelain wrapper
│   └── _checkpoints.py     # checkpoint commit logic
├── state/
│   ├── _transient.py       # ~/.kodo/transient/...
│   └── _memory.py          # src/.memory/*.kd helpers
└── project/
    ├── _layout.py          # path conventions (kodo.md, src/, gen/, .kodo/)
    └── _manifest.py        # kodo.md parser/validator
```

### 1.2 Extension layout

```
kodo-vsix/
├── src/
│   ├── extension.ts        # activate, commands, server lifecycle
│   ├── server-launcher.ts  # download/launch/PID-cleanup
│   ├── ws-client.ts        # wire-protocol client mirror
│   ├── webview/            # Preact app
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── Conversation.tsx
│   │   │   ├── ApprovalGate.tsx
│   │   │   ├── FileEvent.tsx
│   │   │   ├── ShellEvent.tsx
│   │   │   ├── UsagePanel.tsx
│   │   │   └── StopButton.tsx
│   │   └── lib/
│   │       ├── shiki.ts
│   │       └── jsdiff.ts
│   ├── secret-storage.ts   # API token retrieval
│   └── diff-bridge.ts      # WebView -> vscode.diff host command
└── package.json            # vscode contribution points
```

---

## 2. Process model & startup (FR-SRV, FR-VSIX)

1. VS Code window starts → extension activates on `onStartupFinished` (no command needed). Extension resolves the Anthropic token: reads `KODO_ANTHROPIC_API_KEY` from the process environment — if non-empty, writes it to VS Code SecretStorage and uses it; otherwise falls back to the value already in SecretStorage; if neither source yields a key, shows a warning ("set `KODO_ANTHROPIC_API_KEY` and restart") and continues with an empty key (server starts but LLM calls will fail with a clear error).
2. Extension checks `~/.kodo/bin/kodo-server-<os>-<arch>` against expected version. Downloads from GitHub release if mismatched. Verifies SHA-256 against the release manifest.
3. Extension reads `<workspace>/.kodo/server.pid`. If a process is alive, attempts a clean handshake; if it's a stale or foreign PID, kills it and removes the file.
4. Extension picks a free loopback TCP port (binds `127.0.0.1:0`, reads the OS-assigned port, releases it) and launches the server with: `kodo-server --project <root> --port <picked>` and `ANTHROPIC_API_KEY` in env.
5. Server: validates `git` on PATH; ensures project layout (`kodo.md` exists OR errors with init hint); writes PID file; opens WS listener on the supplied port (loopback only).
6. Extension opens WebSocket, sends `request{kind:"hello", payload:{client:"vsix", version:...}}`. Server responds with `response{payload:{server_version, project_root, last_session?}}`. The WS connection persists for the lifetime of the VS Code window; the Kodo panel may open and close many times against the same connection.
7. If `last_session` exists and is not in a clean terminal state, server emits `event{kind:"resume_offer"}` so the WebView can prompt Dev to resume.

Graceful shutdown is triggered by VS Code window close, an explicit `shutdown` request, or SIGTERM. The server flushes transient state, closes the WS, terminates child processes started under tools/shell, removes PID file, exits.

---

## 3. Wire protocol (FR-WS)

### 3.1 Envelope

Every WebSocket frame is JSON:

```json
{
  "kind": "request" | "response" | "event" | "stream_chunk" | "stream_end",
  "id": "<ulid>",
  "correlation_id": "<id of the request this responds to>",
  "payload": { ... message-specific ... }
}
```

- `kind=request`: client→server or server→client invocation.
- `kind=response`: terminates a request; carries `correlation_id`.
- `kind=event`: unsolicited push (state changes, file events, usage updates).
- `kind=stream_chunk`: a fragment of a streamed response. Multiple chunks share a `correlation_id` with `kind=stream_end` closing the stream.

Errors are `kind=response` with `payload.error = { code, message, details? }`.

### 3.2 Message catalog (initial)

| Direction | kind | payload type | purpose |
|---|---|---|---|
| C→S | request | `prompt.submit { text }` | Dev typed a free-form prompt |
| C→S | request | `approval.respond { gate_id, action: "agree"\|"feedback", feedback?: string }` | response to an approval gate |
| C→S | request | `stop {}` | global STOP |
| C→S | request | `session.resume { session_id }` | continue a prior session |
| C→S | request | `checkpoint.list {}` | list mirror commits |
| C→S | request | `checkpoint.rollback { commit_sha }` | restore to a checkpoint |
| C→S | request | `security.add_rule { scope, rule }` | session/global rule add |
| C→S | request | `mode.set { autonomous: bool }` | toggle autonomous |
| S→C | event | `agent.started { agent, component? }` | stage transitions |
| S→C | event | `agent.finished { agent, component?, status }` |  |
| S→C | stream_chunk + stream_end | `agent.tokens { text }` | streamed LLM output |
| S→C | event | `file.change { kind: "add"\|"modify"\|"delete", path, diff_uri? }` | file change happened |
| S→C | event | `shell.run { command, cwd, exit_code, stdout, stderr }` | shell tool result |
| S→C | event | `approval.request { gate_id, gate_type, artifact_path?, summary }` | needs Dev decision |
| S→C | event | `security.prompt { rule_match, command, decision_id }` | needs Dev decision for a tool call |
| S→C | event | `usage.update { cumulative_usd, last_call_tokens, breakdown }` | cost update |
| S→C | event | `error { code, message, recoverable }` | surface a server error |
| S→C | event | `state { stage, agent?, component?, autonomous }` | full state snapshot |

### 3.3 Streaming & backpressure

LLM token streams use `stream_chunk` frames. The extension is the only consumer; if it disconnects, the server's outbound queue (`transport/_outbox.py`) buffers events. On reconnect, the server replays the queued frames in order. No flow-control protocol is implemented in MVP (FR-WS-04 / out-of-scope buffer caps); the queue is bounded at a generous default (50 MB) and overflows are logged.

### 3.4 Request/response correlation

`id` is generated by the sender (ULID). `correlation_id` is the `id` of the originating request. The receiver maintains a pending-request map and times out after 60s with an error response.

---

## 4. Plugin model

Three plugin kinds, all loaded by dotted import path at startup. None are dynamic in MVP (no install/uninstall UX).

### 4.1 LLMPlugin (FR-LLM)

```python
class LLMPlugin(ABC):
    name: str
    supported_models: list[str]

    async def stream_query(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],   # indices into messages to mark cache_control
    ) -> AsyncIterator[StreamEvent]: ...

    async def cancel(self, stream_id: str) -> None: ...

    def report_usage(self, response: AnyResponse) -> Usage: ...
```

`StreamEvent` covers token deltas, tool-use requests, and end-of-turn. The Anthropic implementation uses the SDK's streaming interface and translates `tool_use` content blocks into `StreamEvent.tool_call`.

### 4.2 Agents (FR-AGT)

Agents are not Python classes. They are markdown files at `kodo/agents/<name>.<model>.md`, parsed into a small data type at startup:

```python
@dataclass(frozen=True)
class Agent:
    name: str
    model: str
    tools: frozenset[str]            # MCP tool names this agent may invoke
    system_prompt: str               # body of the markdown
    source_path: Path
```

Frontmatter schema:

```yaml
---
name: requirements_author
tools:
  - tools/fileio.read_file
  - tools/fileio.write_file
---
```

The body is the full system prompt for the model encoded in the filename. There is no inheritance, no shared common section: each (name, model) file is self-contained and independently editable. Looking up a name with no variant for the active model is a hard error — adding a model means authoring a new variant file.

Agents are invoked by the workflow, not by each other. Per agent invocation the workflow function: collects inputs (reads `.kd` files, gathers prior turns), constructs the user message, calls the LLM plugin with the agent's `system_prompt` plus the constructed messages plus the `tools` filter (a hard pre-filter the security layer reads before its own rule evaluation), and interprets the response (writing files via `tools/fileio`, parsing accept/feedback output for reviewer agents, etc.).

### 4.3 ToolchainPlugin (FR-TC)

```python
class ToolchainPlugin(ABC):
    name: str
    languages: list[str]

    async def init(self, project_root: Path) -> None: ...
    async def add_dependency(self, name: str, version: str | None = None) -> None: ...
    async def build(self, component_dir: Path) -> BuildResult: ...
    async def test(self, scope: TestScope) -> TestResult: ...
    async def format(self, paths: list[Path]) -> None: ...
```

`TestScope` selects unit / integration / e2e and an optional component filter. `TestResult` carries pass/fail counts, per-test status and failure messages, plus an optional coverage report path.

---

## 5. Workflow engine (FR-WF)

### 5.1 Stage machine

```
PROJECT_INIT
    ↓
NARRATIVE         → gate("narrative")
    ↓
ARCHITECTURE      → gate("responsibilities")        # also fixes component list
    ↓
REQUIREMENTS_*    → gate per component
    ↓
DESIGN_*          → gate per component
    ↓
TEST_PLAN_*       → gate per component
    ↓
TEST_CODING_*     → produces failing tests
    ↓
IMPLEMENTATION_*  → gate per component
    ↓
INTEGRATION_TEST  → DAG-driven from architect's component graph
    ↓
E2E_TEST          → must pass
    ↓
FINAL             → gate("final")
    ↓
DONE
```

`*` denotes per-component fan-out. With one worker, fan-out is a serial loop in alphabetical order (FR-WF-03). Integration test scheduling honors the dependency DAG (FR-WF-04): an integration test for components `{A, B}` runs only after both components are in `IMPLEMENTATION` complete.

### 5.2 Engine internals

- One `asyncio.Queue[Task]`. Task = `(stage, component?, agent_chain)`.
- One worker coroutine consumes tasks and runs the relevant agent(s).
- Author/Reviewer pairs live as a sub-state inside a single task: the worker loops `Author.run() → Reviewer.run()` up to 5 iterations until accept (FR-AGT-03).
- An approval gate enqueues an `approval.request` event and `await`s a `Future` resolved by the matching `approval.respond`.
- STOP cancels the worker coroutine, which propagates `CancelledError` into all in-flight `await`s including LLM streams (FR-LLM-07).
- Resume: the engine replays the stage machine to the most recent gate, plus the most recent transient state for the in-flight agent (FR-STA-02).

### 5.3 Approval gate semantics

- A gate is identified by `gate_id = sha1(stage|component|artifact_path)`.
- `approval.request` carries: `gate_type`, `artifact_path` (so the WebView can render or link), and a one-paragraph `summary` produced by the responsible Author.
- Two affirmative actions: `agree`, `feedback`. There is no `reject` (FR-WF-06).
- On `feedback`: the engine re-runs the responsible Author/Reviewer pair with the feedback text injected as a new user message in their conversation. The gate fires again with the regenerated artifact.
- On `agree`: a checkpoint is committed in the mirror (FR-MIR-03), and the next stage starts.
- In autonomous mode, Dev Proxy intercepts the gate and answers `agree` (FR-AUT-02).

### 5.4 Component DAG

The Architect emits, alongside `responsibilities.kd`, a `responsibilities.dag.json` listing `{component, depends_on: [...]}`. The scheduler topologically sorts only for the integration-test phase. Component-internal stages (Requirements/Design/Test Plan/Test Coding/Implementation) do not consult the DAG since components are independent at that level.

---

## 6. Agent design

### 6.1 Common prompt structure

Every agent's LLM call is structured as:

```
system:
  [agent role + responsibilities + the behavior-testing principle (FR-TST) if test-related]
  [global conventions from src/.memory/*.kd if any]

user (cached block):
  ## Project narrative
  {{src/narrative.kd}}

  ## Responsibilities
  {{src/responsibilities.kd}}

  ## Component context (only for component-scoped agents)
  - requirements: {{src/<component>/requirements.kd}}
  - design: {{src/<component>/design.kd}}
  - test_plan: {{src/<component>/test_plan.kd}}

user (uncached):
  ## Task
  {{stage-specific instructions}}

  ## Prior turn (if iterating with Reviewer feedback)
  {{feedback}}
```

`cache_control` breakpoints are placed after the system prompt and after the cached user block, so the next call in the same agent's turn (typical Author→Reviewer iteration) reads the cache.

### 6.2 Per-agent specifics

Each agent is a single markdown file (see §4.2). The body of the file is the system prompt; constraints below are encoded directly in that prompt. Detailed prompts are authored during the M3 milestone (see [PLAN.md](PLAN.md)).

Notable agent constraints:

- **Test Designer & Test Design Critic**: prompts include explicit guard-rails against call-count assertions, internal mocks, and tautological tests. The Critic is required to reject any test plan containing those, with a feedback template that cites FR-TST-01..03.
- **Coder**: receives only the failing tests + design + requirements. Has access to `tools/shell` (to run tests) and `tools/fileio`. Loops "edit → run tests" until all green or iteration limit. Behavior-testing principle keeps the tests stable under refactoring.
- **Code Reviewer**: receives diff + tests + design. May request behavior changes only via feedback that maps to a requirement.

### 6.3 Dev Proxy (LLM agent with rules)

The Dev Proxy is an ordinary agent (per §4.2) — a markdown file at `kodo/agents/dev_proxy.<model>.md` whose system prompt establishes the role of "autonomous-mode proxy." User-defined rules from project settings are interpolated into the prompt at invocation time.

Configuration in `<project>/.kodo/settings.json`:

```json
{
  "dev_proxy": {
    "model": "claude-haiku-4-5-20251001",
    "default_action": "agree",
    "rules": [
      "If a shell command starts with 'rm -rf', return feedback asking for confirmation instead of agreeing.",
      "Approve narrative drafts that include both a 'risk' and a 'success criteria' section; otherwise return feedback.",
      "Allow curl/wget against *.etrade.com; deny against any other host."
    ]
  }
}
```

Prompt structure:

```
system:
  You are the autonomous-mode proxy for the Dev. Your job is to answer prompts the
  Dev would otherwise be interrupted by, applying the rules below with judgement.
  Return JSON: {"action": "agree" | "feedback" | "deny", "feedback"?: string, "reasoning": string}.
  When no rule clearly applies, default to "agree" (FR-AGT-DP-02).

  Rules:
  - {{rule 1}}
  - {{rule 2}}
  - ...

user:
  Event:
  {{kind, payload, agent, stage, component, recent context}}
```

The proxy intercepts both `approval.request` events (workflow gates) and `security.prompt` events (tool-call prompts) when autonomous mode is on. Each invocation is one LLM call; cost is reported through the standard usage stream so autonomous runs remain visible (FR-COS, FR-AGT-DP-04). The proxy is single-turn — no Author/Reviewer iteration.

---

## 7. Security layer (FR-SEC)

### 7.1 Rule schema

```json
{
  "scope": "session" | "global",
  "match": {
    "tool": "tools/fileio.write_file" | "tools/shell.run" | "*",
    "args_regex": "^pytest( |$)"
  },
  "action": "allow" | "deny" | "prompt",
  "reason": "free-form, displayed to Dev"
}
```

Rules are evaluated in this order: built-in defaults → global → session. First match wins. A rule with `tool = "*"` is allowed only at deny-side; allow rules must specify a tool to avoid foot-guns.

### 7.2 Built-in defaults (initial set)

- `deny` shell commands matching `\brm\s+-rf\s+/` or `:(){:|:&};:` (fork-bomb).
- `deny` any path outside the project root, evaluated post-canonicalisation.
- `prompt` shell commands matching `^(curl|wget|nc|ssh)\b`.
- `prompt` shell commands matching anything not in the safe-list.
- `allow` exact safe-list entries: `pytest`, `pytest -q`, `npm test`, `npm run lint`, `git status`, `git diff`, `git add`, `ruff`, `mypy`, `vitest run`.
- `allow` any `tools/fileio.*` call confined to project root, except when "review all writes" mode is on (then `prompt`).

### 7.3 Evaluation flow

```
agent.tool_call
   ↓
security.layer.evaluate(call)
   ↓
[allow] → execute → return result to agent
[deny ] → synthesise error result, log → return to agent (no Dev interruption)
[prompt] →
    emit event security.prompt{decision_id, ...}
    await Dev decision (or Dev Proxy if autonomous)
    on "agree" → execute and optionally add a session rule for "remember this answer"
    on "feedback" → treat as deny + pass feedback string back to agent as the tool result
```

---

## 8. Mirror & checkpoints (FR-MIR)

The mirror at `<project>/.kodo/checkpoints/` is initialised by `Kodo: Init Project` with a single empty commit and a fixed branch `kodo`. The mirror is *not* a git worktree of the main repo; it is a separate repository whose working tree contains a copy of `src/` and `gen/`.

Checkpoint flow at every approval gate:

1. Gate fires; Dev clicks Agree.
2. Server `rsync`-style copies `<project>/src` and `<project>/gen` into the mirror's working tree (excluding `.kodo/`, `.git/`, and node/python build artefacts that the toolchain plugin declares ignorable).
3. `git add -A`, `git commit -m "[<gate_type>] <component_or_artifact>"`.
4. Returns the new SHA in the `approval.respond` response so the WebView can show "checkpoint <sha>".

Rollback (`checkpoint.rollback`) does the inverse copy from a prior commit's tree back to `src/` and `gen/`, after creating a safety checkpoint of the current state.

The mirror never contains uncommitted changes between gates: gates are the only commit moment.

---

## 9. State & memory (FR-STA)

### 9.1 Transient state

`~/.kodo/transient/<project-hash>/<session-id>/`:

```
session.json                  # session metadata: started_at, last_stage, autonomous_mode, ...
agents/<agent-name>.jsonl     # one record per LLM call
mcp/<tool>.jsonl              # one record per MCP call
ws-outbox.jsonl               # disconnect-tolerant outbound queue
```

- `<project-hash>` is `sha1(absolute_project_root)[:12]`.
- Records are append-only; rotation/compaction is post-MVP.
- Resume reads the latest session.json + the agent JSONL to reconstruct the active agent's conversation.

### 9.2 Memory

Memory lives in the main repo as `.kd` files under `<project>/src/.memory/`. Naming is by topic: `architecture-decisions.kd`, `conventions.kd`, `external-systems.kd`, etc. Agents may write to memory through the standard fileio MCP path. Writes appear in the WebView as ordinary `file.change` events; security rules apply.

Memory is loaded into the cached user block of every agent's LLM call (Section 6.1), keeping it inexpensive on subsequent calls.

### 9.3 Settings precedence

```
project   <project>/.kodo/settings.json
   ↑ overrides
user      ~/.kodo/settings.json
   ↑ overrides
defaults  baked into the binary
```

Schema is documented in `src/kodo/server/_config.py` as a `pydantic` model. VS Code workspace settings are *only* used by the extension for VSIX-side concerns (server binary path override, log level).

---

## 10. WebView (FR-VSIX-06..08)

### 10.1 Stack

- **Preact** + **Vite** for the SPA. Smaller and faster to build than React; API-compatible enough for typical components.
- **Shiki** for syntax highlighting (server-side themed tokens, ships with TextMate grammars).
- **jsdiff** for inline diff previews inside event cards.
- The WebView opens VS Code's native diff editor for the full diff via the `vscode.diff` host command, bridged from the WebView through `acquireVsCodeApi().postMessage`.

### 10.2 Components

- **Conversation**: vertical timeline of `agent.tokens`, `file.change`, `shell.run`, `approval.request` cards in arrival order.
- **ApprovalGate card**: shows `gate_type`, `summary`, link to artifact, two buttons (Agree, Feedback) and a textarea.
- **FileEvent card**: filename, change kind, link "Open diff" → host command.
- **ShellEvent card**: command, cwd, collapsed stdout/stderr (expandable), exit code colour.
- **UsagePanel**: cumulative cost; drawer for per-agent breakdown.
- **StopButton**: pinned top-right; sends `stop {}` request.
- **AutonomousToggle**: pinned top-left; sends `mode.set`.
- **ResumeBanner**: shown when server reports `resume_offer`.

### 10.3 State

- The **extension host** owns persistent state (connection status, current stage, conversation buffer, usage totals, autonomous flag, etc.) for the lifetime of the VS Code window. The WS client maintains it in memory; closing the panel does not affect it.
- The WebView is a stateless view onto that state. On mount the Preact app posts `{type:"ready"}` to the host; the host replies with the current cached state, and live envelopes flow into both the cache and the WebView from then on.
- WebView-side state is managed with `@preact/signals`; one signal per top-level slice (conversation, usage, stage, autonomous). It is purely UI-mirror state — the source of truth is the extension host's cache, which in turn mirrors the server.
- WebView local-storage is used only for ephemeral draft text in the prompt input across panel close/open.

---

## 11. Project layout & `kodo.md` schema (FR-PRJ)

### 11.1 Filesystem

```
<project>/
├── kodo.md
├── src/
│   ├── narrative.kd
│   ├── responsibilities.kd
│   ├── responsibilities.dag.json     # emitted alongside, machine-readable
│   ├── .memory/
│   │   └── *.kd
│   └── <component>/
│       ├── requirements.kd
│       ├── design.kd
│       └── test_plan.kd
├── gen/
│   ├── <component>/
│   │   └── ... (toolchain-shaped: pyproject.toml or package.json + sources + unit tests)
│   └── tests/
│       ├── integration/
│       └── e2e/
└── .kodo/
    ├── checkpoints/                  # mirror git repo
    ├── settings.json
    ├── security.json
    ├── server.pid
    ├── logs/
    │   └── server.log
    └── sessions/
        └── <session-id>.json
```

### 11.2 `kodo.md` minimal template

```markdown
# Kodo Project

> Project marker. Required.

## Toolchain

- python                       # one of: python, node

## Components

(empty until Architect runs; agents append entries)

## Settings overrides

(optional inline overrides; structured-but-prose)
```

The `# Kodo Project` heading is the unique identifier; `Kodo: Init Project` refuses to run if it already exists. The `## Toolchain` heading specifies the active toolchain plugin. `## Components` is overwritten by the Architect.

---

## 12. Error handling, retries, autonomous mode

### 12.1 LLM call retries

Implemented in `llms/anthropic/_retry.py`. Wraps `stream_query` with a generator that detects retryable errors (HTTP 5xx, timeouts, transient connection errors). Backoff: 2s, 8s, 32s. After exhaustion, emits an `error{recoverable: true}` event and pauses the worker.

Quota / 401 / 403 errors are non-retryable: emit `error{recoverable: false}` and abort the workflow until Dev intervenes.

### 12.2 MCP tool errors

Tool errors are returned as the tool result content with an error flag. The agent sees the error and decides whether to retry (Coder typically does on shell test failures, since that's the whole loop). The security layer's `deny` is rendered as a tool error, not a workflow halt.

### 12.3 Autonomous mode behaviour

- `mode.set { autonomous: true }` causes the Dev Proxy to intercept `approval.request` and `security.prompt` events.
- LLM rate-limit pauses become silent: the worker waits, no Dev notification, STOP still works.
- Hard errors (auth, billing, NFR-04 violations) page the Dev regardless.

---

## 13. Concurrency model

- Server is single-process, asyncio-based.
- One worker coroutine drives the workflow.
- LLM streams, MCP tools, mirror commits run as awaited coroutines on the same loop.
- Tool-call IO that would block (subprocess for `tools/shell`) uses `asyncio.create_subprocess_exec` so it doesn't block the loop.
- The WS handler runs concurrently with the worker; messages are exchanged via `asyncio.Queue` and `Future` (for pending approvals).
- STOP cancels the worker task; cleanup is in the worker's `finally`.

---

## 14. Sequence diagrams

### 14.1 Happy-path approval

```
Dev (WebView)         Server (workflow worker)         LLM plugin
       │                       │                            │
       │  prompt.submit ──────►│                            │
       │                       │  start NarrativeAuthor ──► │
       │                       │                            │── stream tokens ─┐
       │ ◄─── stream_chunk × N (tokens)                                        │
       │ ◄─── stream_end                                                       │
       │                       │ ◄─ artifact written ───────────────────────── │
       │ ◄─── file.change                                                      │
       │                       │  start NarrativeReviewer ──► (accepts)        │
       │ ◄─── agent.finished                                                   │
       │                       │  commit checkpoint                            │
       │ ◄─── approval.request (gate=narrative)                                │
       │  approval.respond {agree} ►│                                          │
       │                       │  next stage: ARCHITECTURE                     │
```

### 14.2 STOP

```
Dev: stop {} ─► Server: cancel worker task
                       cancel current LLM stream (LLM plugin .cancel)
                       finalise transient state
                       emit event state{stage: STOPPED}
```

### 14.3 Reconnect

```
Extension disconnects (e.g. VS Code reload)
       Server keeps worker running, queues outbound events into ws-outbox.jsonl
Extension reconnects → request hello
       Server replays outbox in order, then resumes live event stream
```

---

## 15. Mapping back to requirements

| Section | Covers |
|---|---|
| 1, 2 | FR-VSIX-01..09, FR-SRV-01..08 |
| 3 | FR-WS-01..04 |
| 4 | FR-LLM-01..07, FR-AGT-01..03, FR-TC-01..05 |
| 5 | FR-WF-01..08 |
| 6 | FR-AGT-NA..CR, FR-AGT-DP-01..03, FR-TST-01..04 |
| 7 | FR-SEC-01..07, FR-MCP-01..05 |
| 8 | FR-MIR-01..05 |
| 9 | FR-STA-01..06 |
| 10 | FR-VSIX-06..08, FR-COS-01..03 |
| 11 | FR-PRJ-01..06 |
| 12 | FR-LLM-05..07, FR-AUT-01..03 |
| 13 | NFR-02 |
| 14 | (illustrative) |
