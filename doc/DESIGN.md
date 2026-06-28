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
                                                                  ~/.kodo/
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
├── transport/              # wire protocol (WS_PROTOCOL.md)
│   ├── _envelope.py        # {kind, id, correlation_id, payload}
│   ├── _messages.py        # typed payload-type constants per WS_PROTOCOL.md
│   ├── _outbox.py          # disconnect-tolerant outbound queue
│   └── _ws.py              # aiohttp WebSocket binding
├── runtime/                # thin substrate that hosts the Guide session
│   ├── _engine.py          # single worker, dispatches Guide tool calls
│   ├── _tool_surface.py    # Guide's tools (FR-ORCH-03) wired to engine
│   ├── _gates.py           # ask_user / request_user_review_artifact blocking machinery
│   └── _session.py         # per-session metadata, resume logic
│                           # (context compaction lives in _engine.py, FR-ORCH-05)
├── subagents/              # markdown subagent files; one file per (name, model)
│   ├── _loader.py          # parses frontmatter + body into Agent dataclass
│   ├── _registry.py        # (name, model) -> Agent
│   ├── guide.claude-sonnet-4-6.md
│   ├── narrative_author.claude-sonnet-4-6.md
│   ├── architect.claude-sonnet-4-6.md
│   ├── requirements_author.claude-sonnet-4-6.md
│   ├── requirements_critic.claude-sonnet-4-6.md
│   ├── planner.claude-sonnet-4-6.md
│   ├── functional_designer.claude-sonnet-4-6.md
│   ├── functional_design_critic.claude-sonnet-4-6.md
│   ├── test_designer.claude-sonnet-4-6.md
│   ├── test_design_critic.claude-sonnet-4-6.md
│   ├── test_coder.claude-sonnet-4-6.md
│   ├── coder.claude-sonnet-4-6.md
│   └── code_reviewer.claude-sonnet-4-6.md
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
│   ├── _transient.py       # .kodo/sessions/<posix-ts>/ per-session store
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
│   │   │   ├── PromptCard.tsx        # question / approval / permission
│   │   │   ├── ArtifactCard.tsx      # artifact.published / removed
│   │   │   ├── ToolCallCard.tsx      # agent.tool_call (incl. shell)
│   │   │   ├── ReviewStatus.tsx      # review.started / verdict
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
6. Extension opens WebSocket, sends `hello` request per WS_PROTOCOL.md §4.1. Server responds with `hello.ack` embedding the current `state` snapshot. The WS connection persists for the lifetime of the VS Code window; the Kodo panel may open and close many times against the same connection.
7. Bootstrap runs ([STATE_AND_LIFECYCLE.md §3](STATE_AND_LIFECYCLE.md)): scans mirror, scans workspace, locates the Guide session and any sub-agent sessions it had in flight, queues them for resume. No user prompt is required — resume is automatic. The state snapshot embedded in `hello.ack` reflects the post-bootstrap world.

Graceful shutdown is triggered by VS Code window close, an explicit `shutdown` request, or SIGTERM. The server flushes transient state, closes the WS, terminates child processes started under tools/shell, removes PID file, exits.

---

## 3. Wire protocol (FR-WS)

The wire protocol — envelope shape, message catalogue, request/response correlation, server-initiated user prompts, reconnect semantics, non-goals — is specified in [WS_PROTOCOL.md](WS_PROTOCOL.md). This section describes only the server-side implementation choices that are not protocol-level.

### 3.1 Implementation

- **Envelope and message types.** Defined in `src/kodo/transport/_envelope.py` and `_messages.py`. Constants are grouped by frame role (`MSG_*` for client requests, `SREQ_*` for server-initiated user prompts, `EVT_*` for events) per WS_PROTOCOL.md §5–§7.
- **Outbound queue.** `src/kodo/transport/_outbox.py` buffers envelopes while the client is disconnected. Cap: 50 MB. On reconnect: replay in arrival order, then push a fresh `state` event (WS_PROTOCOL.md §8). Overflow drops the oldest frames and logs the discard.
- **Request timeout.** The receiver maintains a pending-request map keyed on `id`. Client-initiated requests time out at 60s with an `error` response. Server-initiated user prompts (WS_PROTOCOL.md §6) do not time out — they block the Guide's tool call until the user responds or the connection drops.
- **Streaming.** `agent.tokens` chunks carry `correlation_id` equal to a server-generated per-LLM-call stream id. `stream_end` closes the stream. One agent invocation may produce multiple consecutive streams (multiple LLM calls within one sub-agent run); each gets its own stream id.

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

Agents are not Python classes. They are markdown files at `kodo/subagents/<name>.<model>.md`, parsed into a small data type at startup:

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
  - workspace.publish_artifact
  - workspace.read_artifact
---
```

The body is the full system prompt for the model encoded in the filename. There is no inheritance, no shared common section: each (name, model) file is self-contained and independently editable. Looking up a name with no variant for the active model is a hard error — adding a model means authoring a new variant file.

Two role groups within the `Agent` shape:

- **Guide.** A single sub-agent (`kodo/subagents/guide.<model>.md`) whose tool list is the Guide tool surface (FR-ORCH-03): `query_frontier`, `list_artifacts`, `run_subagent`, `run_author_critic_iteration`, `ask_user`, `rollback`, `finalize_project`. The Guide is the sole entity authorized to invoke other sub-agents (FR-ORCH-02). It does not hold the user sign-off or completion tools — those are leaf tools.
- **Leaf sub-agents.** The remaining markdown files (Narrative Author, Architect, Requirements Author / Critic, Planner, Functional Designer / Critic, Test Designer / Critic, Test Coder, Coder, Code Reviewer). Their tool lists are the workspace tools plus, for the agents that need them, the common user tools: `ask_user` (elicit/validate user input, used by Narrative Author and critics), `request_user_review_artifact` and `report_artifact_completed` (the user review gate and completion signal, held by critics and solo agents), and `escalate_blocker` (authors/coders relinquishing a decision to the Guide). Sub-agents do not invoke each other; they reach the user only through these tools and produce their contributions only through the workspace. The agent registry renders each agent's `## Tools` section and tool set per mode, withholding `ask_user` in autonomous mode (FR-AUT-02).

Per leaf sub-agent invocation, the runtime: assembles the task message from input artifact IDs the Guide passed in (resolving them through the workspace MCP server), calls the LLM plugin with the agent's `system_prompt` plus the task message plus the `tools` filter, and persists the resulting session JSONL per [STATE_AND_LIFECYCLE.md §4](STATE_AND_LIFECYCLE.md). The Guide's own invocation is identical in shape; its tool surface is just larger.

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

## 5. Guide runtime (FR-ORCH, FR-WF)

The runtime is a thin substrate. It does not contain a stage machine, a scheduler, or a workflow DAG. It hosts the Guide's LLM session, dispatches the Guide's tool calls, and runs the leaf sub-agent sessions the Guide spawns. Every decision about *what* runs *when* is the Guide's, encoded in its system prompt (FR-ORCH-06) and carried out via its tool surface (FR-ORCH-03).

### 5.1 Engine internals

- One `asyncio.Queue[Task]`. Task is `GuideTurn` or `SubAgentInvocation`.
- One worker coroutine per FR-WF-02. The worker runs whichever task is on the queue; the Guide's blocking tool calls (every tool in FR-ORCH-03 is `async` and can `await`) cooperate with the worker's serial nature.
- The Guide session is a single long-lived `Session` object. Its LLM call is the same shape as any sub-agent's, but its tool list is the FR-ORCH-03 surface.
- STOP cancels the worker coroutine. `CancelledError` propagates into every awaited LLM stream, every pending tool call, every blocking user prompt (FR-LLM-07, FR-WF-07). On STOP, all sessions are flushed and the engine transitions wire `state.phase` to `stopped`.

### 5.2 Tool surface implementation

Each tool in FR-ORCH-03 is implemented in `src/kodo/runtime/_tool_surface.py` as an async function with a JSON Schema declared as a `ToolSpec`. Dispatch happens through the same MCP path leaf sub-agents use for workspace tools, so the Guide's tool calls are persisted in its session log identically to any other tool call.

- `query_frontier()` / `list_artifacts(filters)` — read-only queries against the in-memory `ProjectIndex` ([STATE_AND_LIFECYCLE.md §2](STATE_AND_LIFECYCLE.md)). `query_frontier` reports an artifact completed only once it has been marked so via `report_artifact_completed`.
- `run_subagent(name, task_message, input_artifact_ids)` — generates a fresh `session_id`, looks up the agent in the registry, builds the LLM call, runs it through the worker, persists the session log, returns the IDs of artifacts the sub-agent published. Blocks until the sub-agent's LLM loop terminates.
- `run_author_critic_iteration(...)` — composite tool. Internally invokes `run_subagent` for the Author, reads the resulting artifact, invokes `run_subagent` for the Critic with the Author's artifact ID injected into the task, reads the Critic's `feedback` artifact, returns `{artifact_id, verdict, concerns[]}`. The two underlying sub-agent invocations are visible on the wire as ordinary `agent.started`/`agent.finished` pairs.
- `ask_user` — surface a `prompt.question` `kind=request` frame per WS_PROTOCOL.md §6.1 for the Guide's own judgment calls. The worker `await`s a `Future` keyed on the request's envelope `id`; the WS dispatcher resolves the Future when a `kind=response` with the matching `correlation_id` arrives. In autonomous mode `ask_user` is withheld from the Guide's tool set entirely (FR-AUT-02), so there is nothing to resolve. The user **review gate** is not an Guide tool: it is surfaced by a critic or solo agent's `request_user_review_artifact` leaf call, which the engine routes through the same `prompt.approval` machinery (`runtime/_gates.py`) and auto-accepts in autonomous mode.
- `rollback(target_sha)` — invokes the procedure in [STATE_AND_LIFECYCLE.md §8.3](STATE_AND_LIFECYCLE.md), then terminates the current Guide session and starts a fresh one (since the rollback discards in-flight workspace state the Guide was reasoning about).
- `finalize_project()` — flushes state, transitions wire `state.phase` to `done`, ends the Guide session normally.

### 5.3 Iteration cap and bail logic

The Author/Critic iteration cap (default 5, FR-AGT-05) lives in the Guide's system prompt as a non-negotiable rule. The runtime does not enforce it — it just dispatches whatever `run_author_critic_iteration` calls the Guide makes. When the Guide decides to bail, it calls `ask_user` to surface the situation; the user replies with guidance or an accept-as-is decision.

### 5.4 Component dependency DAG

The Architect publishes a project-wide `architecture` artifact whose content includes the component dependency DAG (responsibility codename → depends_on list). The Planner consumes this when authoring the Plan; the Plan encodes ordering and dependency information as task metadata. The runtime does not topologically sort anything — that responsibility is in Planner's prompt, and the Guide follows the resulting Plan task order.

### 5.5 Compaction

When the entry agent's main context crosses **90%** of the **current model's context window** (per-model `context_window` in `kodo/llms/_registry.py`; *not* a global setting), the engine compacts it **in place** — implemented in `runtime/_engine.py`, not a separate module. Switching to a smaller-window model auto-compacts with the outgoing model before the switch takes effect (`handle_config_changed`):

1. The engine runs the tool-less `compactor` sub-agent (`subagents/subagent_compactor.md`) directly, handing it the current main transcript. Output is a compact "prior-context block" capturing the goal, decisions, progress (artifacts/files/plan position), durable tool results, open items, and the next step.
2. A `compaction` marker carrying the summary is appended to `session.jsonl`, and the live message history is reset to a single synthetic block wrapping that summary. The full log is **not** rewritten — it stays as audit history and `__load_main_messages` rebuilds the LLM context from the latest marker onward.
3. Wire events surface the transition: `context.compacting {active}` brackets the run and `context.compacted {summary_excerpt, summary, tokens_before, tokens_after}` concludes it (the divider is clickable and expands to the full `summary`); the live gauge rides on `context.stats`. See WS_PROTOCOL.md §5.7a.
4. The entry agent resumes transparently. The user can also force compaction at any idle moment via the header **Compact now** button (`compact.now`).

This in-place scheme supersedes the earlier session-rotation design (a fresh `session_id` + `guide.compacted`); see STATE_AND_LIFECYCLE.md §4.5.

---

## 6. Agent design

### 6.1 Leaf sub-agent prompt structure

Every leaf sub-agent's LLM call is structured as:

```text
system:
  [agent role + purpose + behavior-testing principle (FR-TST) if test-related]
  [global conventions from project memory artifacts if any]

user (cached block):
  ## Project narrative          (from the narrative artifact)
  ## Architecture               (from the architecture artifact)
  ## Responsibility context     (per-responsibility agents only)
  - requirements                (the relevant requirements artifact)
  - functional-design           (when an Author/Critic stage downstream of it)
  - test-plan                   (Test Coder, Coder, Code Reviewer)

user (uncached):
  ## Task
  {{task message assembled by the runtime from the Guide's run_subagent inputs}}

  ## Prior revision (revisions only)
  {{previous_artifact_id resolved to content; passed when the Guide
    is re-running the Author after a critic verdict or user feedback}}
```

`cache_control` breakpoints sit after the system prompt and after the cached user block, so successive calls in the same sub-agent's loop reuse the cache. Artifact content is pulled from the workspace MCP server at task-assembly time; the runtime never embeds disk paths in the prompt.

### 6.2 Guide prompt structure

The Guide's call is similar in shape but its cached user block carries the *index summary* instead of fixed neighbour artifacts:

```text
system:
  [Guide role and purpose]
  [The canonical sequence (FR-ORCH-06) as a non-negotiable default]
  [Review-gate handling (FR-WF-05/06): critics and solo agents own the gate;
   how the Guide responds when feedback or an escalation comes back]
  [Author/Critic iteration cap (5) and the bail/escalate judgment]
  [Tool surface reference: names only, no schemas (CLAUDE.md "transport-agnostic
   tool contract"); schemas live in code]
  [Discovery vs execution sub-mode rules (FR-ORCH-07)]

user (cached block):
  ## Project index summary       (live artifacts per responsibility/type)
  ## Frontier                    (per-responsibility next-stage hint)
  ## Plan                        (when accepted; full Plan content)
  ## Pending prompts             (any outstanding user-blocking moments)

user (uncached):
  ## Most recent event
  {{e.g., a sub-agent just finished and published artifact X;
        or user just answered a question; or this is a cold-start}}
```

The Guide decides its next tool call from this context. Its output is interpreted by the runtime as ordinary tool-use content blocks; there is no special prose parsing.

### 6.3 Per-agent constraints

Each leaf sub-agent's markdown file encodes its constraints in the system prompt. Detailed prompts are authored during the M3 milestone (see [PLAN.md](PLAN.md)).

Notable constraints:

- **Test Designer & Test Design Critic** — guard-rails against call-count assertions, internal mocks, tautological tests. The Critic publishes `feedback` with `verdict: "rejected"` citing FR-TST-01..03 when violations appear.
- **Planner** — produces a structured `plan` artifact: markdown narrative for the user plus a machine-readable task list (`task_id`, target sub-agent, responsibility_code, input artifact references, depends_on). Task status is *not* stored in the Plan; the Guide derives it from the index (a task is done when its expected output artifact has been accepted). This avoids mutating a published artifact.
- **Coder** — receives only the failing tests + functional-design + requirements artifact IDs. Has access to `tools/shell` (to run tests) and the workspace tools. Loops "publish revision → run tests" until all green or the Guide bails.
- **Code Reviewer** — publishes `feedback`; concerns may request behavior changes only when they map to a requirement.

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
    emit prompt.permission (kind=request, WS_PROTOCOL.md §6.3)
    await Dev response (or auto-allow if autonomous, §12.3)
    on "allow" → execute and, if response.remember != "no", install a matching rule at the requested scope
    on "feedback" → treat as deny + pass feedback string back to agent as the tool result
```

---

## 8. Mirror & checkpoints (FR-MIR)

The mirror at `<project>/.kodo/checkpoints/` is initialised by `Kodo: Init Project` with a single empty commit and a fixed branch `kodo`. The mirror is *not* a git worktree of the main repo; it is a separate repository whose working tree contains a copy of `src/` and `gen/`.

`MirrorRepo` is a pure git wrapper (`init`, `stage_and_commit(message) → sha`, `checkout(sha)`, `log()`, `head_sha()`) and owns no file copying. Promotion of accepted artifacts and checkpoint commits are sequenced by `Promoter`, defined in [STATE_AND_LIFECYCLE.md §8.1](STATE_AND_LIFECYCLE.md):

- Each Promoter run reads one completed workspace artifact, writes it to its `src/`/`gen/` destination and into the mirror's working tree (alongside a `.kodo.json` metadata sidecar), calls `MirrorRepo.stage_and_commit(message)`, deletes the workspace staging file, and updates `ProjectIndex` (flips the entry to `completed`). Commit message format: `<project_code>/<responsibility_code>/<type>: <session_id> → <artifact_id>`.
- The Promoter run is what `report_artifact_completed` triggers: the engine resolves the toolchain from the Tech Stack, builds a `ComponentRegistry` from the architecture artifact, and runs the Promoter; the wire surfaces the result as an `artifact.published` event carrying `checkpoint_sha` (WS_PROTOCOL.md §5.6).

Rollback (`checkpoint.rollback`, FR-MIR-04) follows the procedure in [STATE_AND_LIFECYCLE.md §8.3](STATE_AND_LIFECYCLE.md): terminate sessions, clear workspace, `MirrorRepo.checkout(target_sha)`, replace `src/`/`gen/` from the mirror tree, rebuild the index, resume.

Checkpoints are produced one per completed artifact (not one per gate). Each `report_artifact_completed` call produces its own Promoter run and its own checkpoint commit; a review gate covering several artifacts yields one completion call and one checkpoint per artifact.

### 8.4 A second, unrelated checkpoint mechanism for Problem Solver

Everything above is the **Guided** pipeline's mirror, scoped to promoted artifacts. The **problem-solving** workflow has no artifacts to promote — it edits the user's real project files directly — so it gets a separate, much lower-level checkpoint mechanism instead, gated to `workflow_mode == "problem_solving"` and otherwise dormant. The two systems share nothing: no code, no storage location, no wire command.

Mechanism: a generic per-root **shadow git mirror** (`kodo/mirror/`'s `ShadowMirror`, driving `git` over an explicit `(work_tree, git_dir)` pair so the tracked files are the real project files — no copying) commits the enclosing root's tree before and after every `filesystem`/`edit_file`/`run_command` dispatch (`runtime/_checkpoints.py:RootMirrorManager`, lazily creating `<root>/.kodo/checkpoints/` + a `.kodo/kodo.md` marker the first time a root is touched). Every commit is append-only: `undo(sha)` restores only the files that commit touched (to their pre-commit state); `rollback(sha)` restores the whole tree to that commit. Both operations are themselves new commits, so the user can always roll forward again. The WebView surfaces this as an "↩ undo this change" link and a "⟲ Rollback to this state" control on each checkpointed tool call.

This is a deliberately minimal, low-level primitive — not a rewrite of FR-MIR — and it does not back the Guide's `rollback` tool. Full implementation detail (the `command_may_mutate` heuristic, the wire messages, the two known cross-root edge cases) lives in [INTERNALS.md §10b/§12.1/§15](INTERNALS.md). A future milestone may rebuild the Guided artifact mirror on top of this engine; that has not happened yet.

---

## 9. State & memory (FR-STA)

### 9.1 Transient state

Session data lives at `<project>/.kodo/sessions/<session-id>/`, co-located with the sub-agent session logs (§4 of STATE_AND_LIFECYCLE.md). `<session-id>` is a POSIX timestamp string (e.g. `1748792400`), naturally sortable so the most recent session is the last one. The Guide marker file at `<project>/.kodo/guide.session` records the active session ID.

```
.kodo/sessions/<posix-timestamp>/
    meta.json         # human-readable: session_name ("Unnamed Session"), created_at
    transient.json    # mutable runtime state: stage, last_prompt, autonomous
    session.jsonl     # append-only guide LLM context (all messages)
    agents/           # one JSONL per sub-agent invocation
    mcp/              # one JSONL per MCP tool call
```

- `transient.json` is overwritten in place on every state change (not append-only).
- `session.jsonl` is append-only; each line is a `{role, content}` message.
- On resume, the engine loads `transient.json` for phase/prompt/autonomous state and replays `session.jsonl` to reconstruct `__orch_messages` for the Guide LLM call.
- A new session directory is created only when no prior session exists or the prior session reached a terminal phase; otherwise the existing directory is reused across restarts.

### 9.2 Memory

Memory lives as artifacts under `<project>/src/.memory/`. The Guide may instruct a sub-agent to publish a memory artifact; promotion lands it in `src/.memory/` with a mirror checkpoint, surfacing as an `artifact.published` wire event like any other promoted artifact (WS_PROTOCOL.md §5.6). Security rules apply at the sub-agent's tool call.

Memory artifacts are included in the cached user block of every leaf sub-agent's LLM call (§6.1) and in the Guide's index snapshot (§6.2), keeping them inexpensive on subsequent calls.

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

- **Conversation**: vertical timeline rendering `agent.*`, `review.*`, `artifact.*`, `prompt.*` cards in arrival order (WS_PROTOCOL.md §5–§6).
- **PromptCard**: one card class with three variants — `prompt.question` (free-text or choice), `prompt.approval` (gate, agree/feedback), `prompt.permission` (security, allow/deny + remember).
- **ArtifactCard**: filename, type, "Open" link (`view: "full"`) or "Open diff" link (`view: "diff"` → `vscode.diff` host command with the mirror's prior version).
- **ToolCallCard**: tool name, one-line summary, expandable details (shell stdout/stderr, exit code colour).
- **ReviewStatus**: low-fi inline status lines (`review.started`, `review.verdict`) — no card chrome.
- **UsagePanel**: cumulative cost; drawer for per-agent breakdown.
- **StopButton**: pinned top-right; sends `stop {}` request.
- **AutonomousToggle**: pinned top-left; sends `mode.set`.

### 10.3 State

- The **extension host** owns persistent state (connection status, current phase, current sub-agent, conversation buffer, usage totals, autonomous flag, etc.) for the lifetime of the VS Code window. The WS client maintains it in memory; closing the panel does not affect it.
- The WebView is a stateless view onto that state. On mount the Preact app posts `{type:"ready"}` to the host; the host replies with the current cached state, and live envelopes flow into both the cache and the WebView from then on.
- WebView-side state is managed with `@preact/signals`; one signal per top-level slice (conversation, usage, phase, autonomous). It is purely UI-mirror state — the source of truth is the extension host's cache, which in turn mirrors the server.
- WebView local-storage is used only for ephemeral draft text in the prompt input across panel close/open.

---

## 11. Project layout & `kodo.md` schema (FR-PRJ)

### 11.1 Filesystem

```text
<project>/
├── src/                              # specification artifacts (FR-WKS-11 via Promoter)
│   ├── narrative/
│   ├── architecture/                 # includes the component dependency DAG
│   ├── requirements/<responsibility>/
│   ├── plan/                         # the execution plan (FR-AGT-PL, FR-WKS-03)
│   ├── design/<responsibility>/      # functional-design + design-plan
│   ├── tech_stack/
│   ├── test_design/<responsibility>/
│   └── .memory/                      # project memory artifacts
├── gen/                              # generated artifacts (FR-WKS-10 via Promoter)
│   ├── src/<responsibility>/
│   └── test/<responsibility>/
└── .kodo/
    ├── kodo.md                       # project manifest — moved here from <project>/kodo.md
    ├── checkpoints/                  # mirror git repo (the Guided promotion mirror, §8)
    ├── workspace/                    # in-flight artifacts + .retired/ audit (STATE_AND_LIFECYCLE.md §1)
    ├── sessions/                     # session directories + sub-agent JSONL logs
    │   ├── <posix-timestamp>/        # one dir per Guide session
    │   │   ├── meta.json
    │   │   ├── transient.json
    │   │   ├── session.jsonl
    │   │   ├── agents/
    │   │   └── mcp/
    │   └── <uuid>.jsonl              # flat JSONL for each sub-agent invocation
    ├── settings.json
    ├── security.json
    ├── server.pid
    └── logs/
        └── server.log
```

The leaf-filename rules per artifact type are defined in [STATE_AND_LIFECYCLE.md §1.1](STATE_AND_LIFECYCLE.md). Promoter is the only writer to `src/` and `gen/`; the workspace MCP server is the only writer to `.kodo/workspace/`.

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

- `mode.set { autonomous: true }` flips the `autonomous` flag on the session. The flag is consumed by the agent registry (per-mode tool rendering), the gate handler (`runtime/_gates.py`), and the security layer.
- Agent registry: tools whose `ToolSpec.autonomous_mode` is `"unavailable"` (currently `ask_user`) are excluded from both the rendered `## Tools` section and the returned tool set, for every agent including the Guide. An agent that would have asked must assume-and-document or `escalate_blocker`.
- Gate handler: `request_user_review_artifact` resolves immediately with a synthesized acceptance without emitting a `prompt.approval` to the wire. Auto-acceptance is recorded in the reviewing agent's session log (so audit shows which gates were auto-accepted) and surfaced as a low-fi `state` event field rather than a per-gate event.
- Rollback: the Guide calls `rollback` directly, without the interactive `ask_user` confirmation, and documents it with a `<kodo_info>` callout.
- Security layer: rules whose action is `prompt` are treated as `allow` while autonomous is on. `deny` rules are still enforced.
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

### 14.1 Happy-path Narrative gate

```text
Dev (WebView)       Server (runtime worker)            LLM plugin
       │                       │                            │
       │  prompt.submit ──────►│                            │
       │                       │  Guide turn ──────► │── stream tokens ──┐
       │ ◄─── stream_chunk × N (Guide decides)                           │
       │ ◄─── stream_end                                                        │
       │                       │ ◄─ tool_use: run_subagent("narrative_author")│
       │                       │  spawn NarrativeAuthor ──► │── stream tokens ──┤
       │ ◄─── agent.started (narrative_author)                                  │
       │ ◄─── stream_chunk × N                                                  │
       │                       │ ◄─ tool_use: publish_artifact(narrative) ──────│
       │                       │  workspace records (in-flight)                 │
       │                       │ ◄─ tool_use: request_user_review_artifact(id) ─│
       │ ◄─── prompt.approval (kind=request, artifact_id=narrative)             │
       │  response {agree} ────►│                                               │
       │                       │  resolve gate Future; return accept to agent ──│
       │                       │ ◄─ tool_use: report_artifact_completed(id) ────│
       │                       │  engine promotes: Promoter fires; mirror commit│
       │ ◄─── artifact.published (path=src/narrative/narrative.md, checkpoint)  │
       │ ◄─── agent.finished (narrative_author)                                 │
       │                       │  return artifact_id to Guide            │
       │                       │  Guide continues ──►│── stream tokens ──┘
       │                       │ ◄─ tool_use: run_subagent("architect")
       │                       │  ...
```

### 14.2 STOP

```text
Dev: stop {} ─► Server: cancel worker task
                       cancel current LLM stream (LLM plugin .cancel)
                       cancel any pending prompt.* Future (re-emits as error)
                       finalise transient state
                       emit event state{phase: "stopped"}
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
| 3 | FR-WS-01..04 (full protocol spec lives in WS_PROTOCOL.md) |
| 4 | FR-LLM-01..07, FR-AGT-01..06, FR-TC-01..05 |
| 5 | FR-ORCH-01..08, FR-WF-02/05/06/07/08 |
| 6 | FR-AGT-OR, FR-AGT-NA..CR, FR-AGT-PL, FR-TST-01..04 |
| 7 | FR-SEC-01..07, FR-MCP-01..05 |
| 8 | FR-MIR-01..05 |
| 9 | FR-STA-01..06 |
| 10 | FR-VSIX-06..08, FR-COS-01..03 |
| 11 | FR-PRJ-01..06, FR-WKS-10/11 |
| 12 | FR-LLM-05..07, FR-AUT-01..03 |
| 13 | NFR-02 |
| 14 | (illustrative) |
