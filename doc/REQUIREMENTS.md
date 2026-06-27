# Kodo MVP — Requirements

> Status: draft for review. Source: `doc/kodo-design-intake.md` + design conversation.
> Each requirement carries a stable ID (`FR-AREA-NN` / `NFR-NN`); refer to IDs in design and tests.

---

## 1. Purpose

Kodo is a build system that converts natural-language requirements into working code through a multi-agent LLM workflow. Specifications (narrative, requirements, designs, test plans) are treated as source code and live alongside generated artifacts in the project repository. A solo developer drives the process from a VS Code extension; a local server orchestrates LLM agents that progressively author and review the specifications, then implement code that is constrained to satisfy a previously written test suite.

## 2. Personas

- **Solo Dev (primary, only persona for MVP).** A developer who wants to convert an idea into a working back-end solution. Comfortable with git, a terminal, Python and/or Node toolchains, and an LLM API account. Owns the project repo. Has Anthropic API access.

Out of scope: teams, multi-tenant deployments, hosted/cloud variants, non-developer users.

## 3. Glossary

| Term | Definition |
| --- | --- |
| Project | A directory containing `kodo.md`, `src/`, `gen/`, and `.kodo/`. The unit of work. |
| `.kd` file | Markdown file under `src/` describing some aspect of the project (narrative, responsibilities, requirements, design, test plan). For MVP, `.kd` is plain Markdown. |
| `kodo.md` | Project manifest, at `<project>/.kodo/kodo.md` (moved under `.kodo/` post-MVP; was at the project root). Required headings declare a Kodo project. |
| Narrative | Top-level natural-language description of the end product. The "north star". One per project. |
| Responsibility | A single, named area of behavior the product must deliver. |
| Component | The implementation unit for one responsibility — typically one package/module containing a main class plus satellites. |
| Guide | The sub-agent that decides what runs next. Holds the tool surface used to spawn other sub-agents, query the project index, ask the user judgment-call questions (`ask_user`), trigger rollback, and finalize the project. It does not surface artifact review gates — those are owned by critics and solo agents. The sole entity authorized to invoke any other sub-agent. |
| Guide session | The session log of the Guide sub-agent. Persists across the project's lifetime; its live LLM context is compacted **in place** (summary marker in `session.jsonl`) when context-window usage approaches exhaustion, while the full log is retained as audit history. |
| Canonical sequence | The default order in which the Guide drives a project when no user-driven re-entry is in flight: Narrative → Architecture → Requirements → Plan → [Plan-driven execution] → Final. The Guide MAY deviate when responding to user feedback or escalations. |
| Plan | A project-wide artifact authored by Planner that enumerates the remaining work as discrete tasks (sub-agent, component, inputs, dependencies). The Guide consults the Plan to pick the next unfinished task after the Plan gate is approved. |
| Planner | The sub-agent that produces the Plan after Requirements have been accepted for every responsibility. |
| Agent | A pluggable component with a declared role, a system prompt, declared capabilities, and a model preference. |
| Author / Critic pair | Two agents collaborating on an artifact: the Author produces it, the Critic publishes a `feedback` artifact carrying `verdict` and `concerns`. The Guide runs one iteration at a time and decides whether to iterate further or stop; the Critic owns the convergence verdict and, on acceptance, the user review gate and completion signal. |
| Review Gate | A moment where the agent that owns an artifact's convergence verdict — a Critic, or a solo agent with no critic — presents the converged artifact to Dev for sign-off via `request_user_review_artifact` and waits for `agree` or `feedback`. In autonomous mode the gate is auto-accepted. Once accepted, the same agent fires `report_artifact_completed`, which drives promotion. |
| Mirror | A `git` repository inside `<project>/.kodo/checkpoints/` used to checkpoint generated artifacts. |
| Checkpoint | A commit in the mirror representing a coherent state Dev can return to. |
| Memory | Distilled long-term project context written as `.kd` files in the main repo. Committed by Dev. |
| Transient state | Per-agent in-flight conversation/retry state stored under `~/.kodo/transient/<project-hash>/<session>/`. Disposable. |
| Toolchain plugin | A pluggable component that knows how to `init`, `add_dependency`, `build`, `test`, and `format` for a target language (Python, Node for MVP). |
| LLM plugin | A pluggable component that fronts a model provider (Anthropic for MVP) and exposes a uniform capability set. |
| MCP | Model Context Protocol. MVP ships in-process `tools/fileio` and `tools/shell` MCP servers. |
| Security layer | The single mediator for every tool call request, governed by user-defined and built-in regex rules. |
| STOP | An always-available control that immediately cancels all in-flight agent work for the project. |
| Workspace | The virtual artifact store through which agents exclusively publish and retrieve named artifacts; backed by `.kodo/workspace/` on disk. Replaces agent-facing filesystem access for all artifact types. |
| Artifact | A named piece of content produced or consumed by an agent, uniquely identified by a UUID and tagged with codename(s). Each artifact is a file on disk. |
| Live artifact | An artifact currently active in the workspace and returnable by `read_artifact`. |
| Retired artifact | An artifact that has been superseded; removed from the live workspace but preserved on disk under `.kodo/workspace/.retired/` for audit. |
| PROJECTCODE | A short mnemonic uppercase identifier for the project as a whole, assigned by Architect (e.g. `ETRD`). First segment of all requirement IDs. |
| RESPONSIBILITYCODE | A short mnemonic uppercase identifier for a single responsibility, assigned by Architect (e.g. `AUTH`). Second segment of requirement IDs. |
| REQUIREMENTCODE | A short mnemonic uppercase identifier for an individual requirement within a responsibility, assigned by Requirements Author (e.g. `LOGIN`). Third segment of requirement IDs. |

## 4. MVP exit ticket

**Kodo MVP is "done" when Kodo can be used to build, end-to-end, an algorithmic stock trading bot that interacts with the E\*TRADE API, with all generated tests passing.** This includes:

- A complete narrative, set of responsibilities, per-component requirements, an execution Plan, functional designs, and test plans authored interactively with Kodo.
- All tests written by Kodo's Test Coder pass when executed by the Python toolchain plugin.
- The end-to-end test exercises the bot against an E\*TRADE sandbox endpoint (no real-money trades during Kodo validation).
- Dev can replay the full session via mirror checkpoints.

This is the only acceptance criterion that gates MVP release. Per-feature requirements below exist to make this attainable.

## 5. Functional requirements

### 5.1 VS Code extension (FR-VSIX)

- **FR-VSIX-01.** The extension SHALL be implemented in TypeScript and packaged as a `.vsix`.
- **FR-VSIX-02.** On activation, the extension SHALL ensure a Kodo Server binary is present under `~/.kodo/bin/` and matches the version expected by the extension; if missing or mismatched, the extension SHALL download the matching binary from the published GitHub release for the current OS/arch and store it under `~/.kodo/bin/`.
- **FR-VSIX-03.** The extension SHALL activate automatically on VS Code window startup (not on first user command), launch one Kodo Server subprocess per VS Code window, passing the project root and a freshly-picked free loopback port as CLI arguments. Multiple VS Code windows SHALL be able to run Kodo concurrently without coordination.
- **FR-VSIX-04.** The extension SHALL obtain the Anthropic API token as follows: on activation, read the `KODO_ANTHROPIC_API_KEY` environment variable; if it is non-empty, persist it to VS Code SecretStorage (overwriting any prior value) and use it; if the env var is absent or empty, fall back to whatever is already stored in SecretStorage; if neither source yields a key, display a warning instructing the Dev to set `KODO_ANTHROPIC_API_KEY` and restart VS Code, then continue with no key. The token SHALL be passed to the server subprocess via `ANTHROPIC_API_KEY` in the child process environment and SHALL NOT be written to any file by the extension or the server.
- **FR-VSIX-05.** The extension SHALL provide a `Kodo: Init Project` command that opens a folder-picker dialog; upon folder selection it SHALL create `kodo.md`, `src/`, `gen/`, and `.kodo/` in the chosen directory, add that directory to the current VS Code workspace (if not already present), and open `kodo.md` in the editor.
- **FR-VSIX-06.** The extension SHALL provide a `Kodo: Open Panel` command that opens (or reveals) a WebView showing the conversation, file events, approval prompts, and usage panel. The WebView SHALL be a view onto extension-host-resident state: while the panel is closed the WebSocket connection MUST remain open, agent state MUST keep updating, and reopening the panel MUST rehydrate the UI from the cached state without forcing a server-side reconnect.
- **FR-VSIX-07.** The extension SHALL provide a globally visible **STOP** control inside the WebView that cancels all running agent work for the project.
- **FR-VSIX-08.** The extension SHALL register URL handlers for diff and file links so that clicking a diff link in the WebView opens VS Code's native diff editor, and clicking a file link opens the file in the editor.
- **FR-VSIX-09.** The extension SHALL gracefully handle server-side disconnects by displaying a reconnect status and resuming the message stream when the server returns; no Dev input is lost across reconnects.

### 5.2 Kodo Server lifecycle (FR-SRV)

- **FR-SRV-01.** The server SHALL be implemented in Python and shipped as a single PyInstaller binary for Windows, macOS, and Linux.
- **FR-SRV-02.** A single server instance SHALL be bound to exactly one project (the path passed at launch). Multi-project servers are explicitly out of scope.
- **FR-SRV-03.** The server SHALL listen on a loopback (`127.0.0.1`) WebSocket port supplied at launch via the `--port` CLI argument. The extension is the canonical caller and SHALL pick a free ephemeral port at activation time so multiple VS Code windows can run Kodo in parallel without clashing. The CLI default (9042) is a fallback for manual invocation only.
- **FR-SRV-04.** The server SHALL refuse connections from non-loopback addresses.
- **FR-SRV-05.** On startup, the server SHALL verify `git` is on PATH and abort with a clear error if not.
- **FR-SRV-06.** On startup, the server SHALL write a PID file at `<project>/.kodo/server.pid`. If a running server is already bound to the project, the new server SHALL exit non-zero. The extension SHALL clean up stale PID files.
- **FR-SRV-07.** On graceful shutdown, the server SHALL flush transient state to disk, close WebSocket connections, kill its child MCP processes, and exit zero.
- **FR-SRV-08.** The server SHALL preserve transient state across crashes on a best-effort basis sufficient to resume the current agent's last in-flight LLM call (see FR-STA).

### 5.3 Wire protocol (FR-WS)

- **FR-WS-01.** All extension↔server messages SHALL be JSON and conform to a single envelope: `{kind, id, correlation_id?, payload}` where `kind ∈ {request, response, event, stream_chunk, stream_end}`.
- **FR-WS-02.** The protocol SHALL support request/response with `correlation_id`, server-initiated events without correlation, and token-level streaming for LLM output via `stream_chunk` followed by exactly one `stream_end`.
- **FR-WS-03.** The protocol SHALL define at minimum these message families: `prompt`, `agent_event`, `file_event`, `shell_event`, `approval_request`, `approval_response`, `stop`, `usage_update`, `error`. Each is enumerated in the design document.
- **FR-WS-04.** The server SHALL queue outbound messages while the extension is disconnected, and replay them on reconnect.

### 5.4 LLM plugin: Anthropic (FR-LLM)

- **FR-LLM-01.** The Anthropic LLM plugin SHALL implement the common `LLMPlugin` interface: `query`, `stream_query`, `attach_mcp`, `report_usage`, `cancel`.
- **FR-LLM-02.** The plugin SHALL use the official `anthropic` Python SDK.
- **FR-LLM-03.** The plugin SHALL use Anthropic prompt caching with `cache_control` breakpoints on (a) the per-agent system prompt and (b) the per-call project context block (narrative + responsibilities + neighboring `.kd` files relevant to the agent).
- **FR-LLM-04.** The plugin SHALL report token usage and dollar cost per call, separating cache-write, cache-read, input, and output tokens.
- **FR-LLM-05.** The plugin SHALL retry transient HTTP errors with exponential backoff: 3 attempts at 2s, 8s, 32s. After exhaustion, the failure SHALL bubble up to the Dev with a notification and pause the workflow.
- **FR-LLM-06.** Quota / billing failures SHALL always bubble up to the Dev as an unrecoverable error.
- **FR-LLM-07.** The plugin SHALL support cancellation: an in-flight stream can be aborted within 1s of receiving a cancel signal.

### 5.5 Agents (FR-AGT)

#### 5.5.1 Agent definition

- **FR-AGT-01.** An agent SHALL be defined by a single Markdown file at `kodo/subagents/<name>.<model>.md`. The file SHALL have YAML frontmatter declaring `name` and `tools` (a list of MCP tool names the agent may invoke), and a body containing the full system prompt for the named model. Agents are not Python classes or plugins — they have no `role`, no typed `inputs`/`outputs`, and no capability set beyond the tool list.
- **FR-AGT-02.** Each agent file SHALL be a complete, self-contained prompt for the model encoded in its filename. Multiple files MAY exist for a single `name` (one per model variant); each file is independent — there is no shared "common" body across variants.
- **FR-AGT-03.** Agents SHALL be looked up by `(name, model)` at runtime. Looking up an agent for a model with no matching file SHALL be a hard error.
- **FR-AGT-04.** Agents SHALL be invoked only by the Guide's tool surface (see FR-ORCH-02); no direct agent-to-agent calls and no engine-level invocation outside Guide-tool dispatch.
- **FR-AGT-05.** Each Author/Critic pair SHALL iterate one round per `run_author_critic_iteration` tool call (FR-ORCH-03). The Guide's prompt SHALL encode an iteration cap of 5 rounds and the judgment rules for accepting the last output, escalating to the user, or continuing. There is no engine-enforced iteration cap.
- **FR-AGT-06.** Agents SHALL ask clarifying questions liberally. Disambiguation is preferred over assumption.

#### 5.5.2 Required agents for MVP

For each agent below, MVP SHALL include one markdown file under `kodo/subagents/` for the default model (`claude-sonnet-4-6` for all agents). The "reads / writes" annotations describe what the Guide passes the agent and where its output lands; they are documentation, not declared types. All artifact production goes through the workspace (FR-WKS), so the path columns below describe the post-promotion location (FR-WKS-10/11).

- **FR-AGT-OR.** **Guide** — reads: full index, current workflow state, user input. Writes: no artifacts directly (the Guide does not call `publish_artifact`). Drives every other sub-agent invocation via its tool surface (FR-ORCH-03). The Guide's prompt encodes the canonical sequence (FR-ORCH-06), the iteration cap and bail rules for Author/Critic loops (FR-AGT-05), and how it responds when a review gate returns feedback (FR-WF-05/06). The Guide does not itself surface review gates — critics and solo agents do.
- **FR-AGT-NA.** **Narrative Author** — reads: Dev prompt. Writes: artifact of type `narrative`.
- **FR-AGT-AR.** **Architect** — reads: narrative. Writes: artifact of type `architecture` (responsibility list with codenames and display names plus component dependency DAG).
- **FR-AGT-RA.** **Requirements Author** — reads: narrative + one responsibility's description. Writes: artifact of type `requirements` scoped to that responsibility.
- **FR-AGT-RR.** **Requirements Critic** — reads: same as Requirements Author. Publishes `feedback` with `verdict` and `concerns`.
- **FR-AGT-PL.** **Planner** — reads: narrative + architecture + every accepted `requirements` artifact. Writes: artifact of type `plan` enumerating the remaining work as discrete tasks. Each task carries at minimum `task_id`, target sub-agent, responsibility_code (when applicable), input artifact references, and `depends_on`. Task status is not stored in the Plan; it is derived from the workspace index (a task is "done" when its expected output artifact is accepted).
- **FR-AGT-FD.** **Functional Designer** — reads: requirements (for one responsibility). Writes: artifact of type `functional-design`.
- **FR-AGT-FC.** **Functional Design Critic** — verifies design against requirements and SOLID; publishes `feedback`.
- **FR-AGT-TD.** **Test Designer** — reads: requirements + functional-design. Writes: artifact of type `test-plan` plus a flag identifying which test belongs to the end-to-end suite.
- **FR-AGT-TC.** **Test Design Critic** — verifies test plan for contradictions, coverage gaps, and behavior-vs-implementation focus (FR-TST); publishes `feedback`.
- **FR-AGT-TX.** **Test Coder** — reads: test-plan + functional-design. Writes: artifacts of type `test`. All tests SHALL be expected-to-fail when first generated.
- **FR-AGT-CO.** **Coder** — reads: functional-design + failing tests. Writes: artifacts of type `code`. Iterates until tests pass.
- **FR-AGT-CR.** **Code Reviewer** — gate-keeps Coder output; publishes `feedback`.

### 5.6 Orchestration & approval (FR-ORCH, FR-WF)

#### 5.6.1 Agentic orchestration (FR-ORCH)

MVP replaces the prior hardcoded stage machine with an agentic Guide. The Guide is an LLM-driven sub-agent (FR-AGT-OR) that decides what runs next by calling tools.

- **FR-ORCH-01.** The Guide SHALL be a sub-agent per FR-AGT-01 (single markdown file per model under `kodo/subagents/guide.<model>.md`). It uses the same `Agent` shape, registry, and session model as every other sub-agent.
- **FR-ORCH-02.** The Guide SHALL be the sole entity authorized to spawn a sub-agent. No code path outside its tool dispatch may invoke `run_subagent` or `run_author_critic_iteration`. The engine refuses to drive a sub-agent invocation initiated by any other caller.
- **FR-ORCH-03.** The Guide's tool surface for MVP SHALL be:
  - `query_frontier()` — return the per-responsibility frontier view derived from the project index. A read-only query: an artifact counts as completed only once an agent has marked it so via `report_artifact_completed` (FR-WKS-15), not by any inference inside this tool.
  - `list_artifacts(filters)` — query the index by `artifact_id`, `type`, `responsibility_code`, `requirement_id`, etc. At least one filter is required.
  - `run_subagent(name, task_message, input_artifact_ids)` — invoke a sub-agent. Blocks until the spawned session completes (single-worker constraint, FR-WF-02). Returns the IDs of artifacts the sub-agent published.
  - `run_author_critic_iteration(author_name, critic_name, input_artifact_ids, previous_artifact_id?)` — execute one round of the Author/Critic loop. Spawns the Author (passing `previous_artifact_id` as feedback context when set), then spawns the Critic, then returns `{artifact_id, verdict, concerns[]}`. The Guide decides whether to iterate or stop; the Critic owns the user review gate and the completion signal once it accepts (FR-WF-05).
  - `ask_user(question, mode, choices?)` — surface a free-form or choice question via WS_PROTOCOL.md §6.1 for the Guide's own judgment calls (triaging an escalation, confirming a rollback or a large cascade). Blocks until the user responds. Withheld in autonomous mode (FR-AUT-02): the Guide makes the call itself and documents it.
  - `rollback(target_sha)` — invoke the rollback procedure in [STATE_AND_LIFECYCLE.md §8.3](STATE_AND_LIFECYCLE.md). In interactive mode the Guide MUST confirm with the user via `ask_user` before calling this; in autonomous mode it makes the call itself.
  - `finalize_project()` — terminal call. Transitions wire `state.phase` to `done` and ends the Guide session.

  The user sign-off and completion tools (`request_user_review_artifact`, `report_artifact_completed`) are **not** on the Guide's surface — they are leaf tools held by critics and solo agents (FR-WKS-15). The Guide never approves artifacts on the user's behalf; it reacts to feedback returned through `run_author_critic_iteration` and to escalations raised via `escalate_blocker`.
- **FR-ORCH-04.** The Guide session SHALL persist for the project's lifetime, surviving cold starts and resumes per [STATE_AND_LIFECYCLE.md §4](STATE_AND_LIFECYCLE.md). It is terminated only by `finalize_project()`, by rollback (§8.3), or by user-initiated STOP that includes a session-end choice.
- **FR-ORCH-05.** When the entry agent's main context approaches context-window exhaustion, the engine SHALL trigger compaction: summarize the current context into a compact prior-context block (via the dedicated `compactor` sub-agent), reset the live LLM context to that block in place, and surface the transition to the user via the wire (WS_PROTOCOL.md §5.7a). The full session log is retained as audit history; only the latest compacted block plus subsequent messages are resent to the model. The user MAY also trigger compaction manually while idle, and switching to a model with a smaller context window SHALL trigger compaction (with the outgoing model) before the switch takes effect. The compaction threshold (90% of the current model's context window — the per-model `context_window` in the LLM registry, not a global setting) is a design-level constant, not a requirement. *(Implemented in place; the earlier session-rotation phrasing is superseded — see STATE_AND_LIFECYCLE.md §4.5.)*
- **FR-ORCH-06.** When no user-driven re-entry is active, the Guide's prompt SHALL drive the canonical sequence: **Narrative → Architecture → Requirements (per responsibility) → Plan → [Plan execution: Functional Design / Test Plan / Test Coding / Coding per responsibility, then integration tests, then E2E] → Final**. The Guide MAY deviate from this sequence when responding to user feedback or escalations, but the deviation is its own judgment, not an engine override.
- **FR-ORCH-07.** Bootstrap decides whether the Guide is in "discovery" or "execution" sub-mode by checking for an accepted `plan` artifact in the index. No accepted Plan → discovery; the Guide's prompt focuses on driving the canonical sequence up to and including Plan acceptance. Accepted Plan present → execution; the Guide selects the next unfinished Plan task (status derived from the index) and dispatches the corresponding sub-agent.
- **FR-ORCH-08.** The Guide's activity SHALL be visible on the wire as ordinary `agent.*` events (WS_PROTOCOL.md §5.2/§5.3) so the user can observe its reasoning and tool calls. The panel MAY style Guide cards distinctly from leaf sub-agent cards, but the wire shape is the same.

#### 5.6.2 Workflow invariants (FR-WF)

- **FR-WF-02.** The engine SHALL use a single async task queue with **exactly one worker** for MVP. This applies to the Guide's session and any sub-agent session it spawns; concurrent execution is not supported in MVP.
- **FR-WF-05.** A review gate SHALL be surfaced to the user at each of the following moments: after Narrative; after Architecture (responsibilities + project code + dependency DAG); after each responsibility's Requirements; after Plan; after each responsibility's Functional Design; after each responsibility's Test Plan; after each responsibility's Implementation (with its tests passing); one final gate after E2E tests pass. The gate is surfaced by the agent that owns the converged artifact — the Critic of the relevant Author/Critic pair, or a solo agent with no critic — by calling `request_user_review_artifact` once it has accepted (FR-WKS-15), never by the Guide. On the user's acceptance the same agent calls `report_artifact_completed`, which marks the artifact completed and drives promotion (FR-WKS-10). In autonomous mode, `request_user_review_artifact` is auto-accepted without surfacing to the user (FR-AUT-02); the agent's behavior does not change between modes — it fires the call unconditionally.
- **FR-WF-06.** A review prompt presented to the user SHALL offer exactly two affirmative actions: **Agree** (no comments), or **Provide Feedback** (free-form text). There SHALL be no explicit Reject button. On `agree`, the reviewing agent reports the artifact completed. On feedback, the reviewing agent opens a revision round on the same artifact; feedback that indicates an earlier upstream artifact is wrong is raised to the Guide via `escalate_blocker`, which decides whether to re-spawn upstream sub-agents.
- **FR-WF-07.** A globally visible **STOP** control SHALL cancel all in-flight agent work, abort streaming LLM calls, kill child MCP processes, and leave the workflow in a `STOPPED` state. Resume after STOP rehydrates the Guide session per [STATE_AND_LIFECYCLE.md §4.3](STATE_AND_LIFECYCLE.md); pending tool calls are re-executed with request-ID dedup per §4.4.
- **FR-WF-08.** The Guide's current activity (its current tool call, the sub-agent it most recently spawned, the responsibility under work) SHALL be exposed via the wire protocol in `state` events (WS_PROTOCOL.md §5.1). The engine does not maintain a separate "stage" concept; the wire's `phase` field is engine-level (intake / running / awaiting_user / stopped / done), not a workflow stage.

### 5.7 Toolchain plugins (FR-TC)

- **FR-TC-01.** A `ToolchainPlugin` interface SHALL define: `init(project_root)`, `add_dependency(name, version)`, `build()`, `test() -> TestResult`, `format()`. `TestResult` includes pass/fail counts, per-test status, and a coverage path if available.
- **FR-TC-02.** MVP SHALL ship two plugins: `kodo.toolchains.python` and `kodo.toolchains.node`.
- **FR-TC-03.** The Python plugin SHALL use a project layout suitable for `pytest`. Dependency management uses `pyproject.toml` + `uv` (preferred) or `pip` (fallback). Test runner: `pytest`.
- **FR-TC-04.** The Node plugin SHALL use `package.json` with `npm`. Test runner: the framework selected by Test Coder (default: `vitest`).
- **FR-TC-05.** Toolchain plugins SHALL only be invoked through the workflow engine and the security layer; they do not call agents directly.

### 5.8 MCP (FR-MCP)

- **FR-MCP-01.** MVP SHALL provide two in-process MCP servers: `tools/fileio` and `tools/shell`, registered with the agent's LLM plugin via the standard MCP capability declaration.
- **FR-MCP-02.** `tools/fileio` SHALL expose: `read_file`, `write_file`, `list_dir`, `move`, `delete`. All paths are resolved relative to the project root and confined to it.
- **FR-MCP-03.** `tools/shell` SHALL expose a single `run` tool taking a command and an optional cwd (default: project root). The shell SHALL be the user's default (PowerShell on Windows, bash elsewhere).
- **FR-MCP-04.** Every MCP tool call SHALL pass through the security layer (FR-SEC) before execution.
- **FR-MCP-05.** Writing a file via `tools/fileio` SHALL NOT trigger an interactive security prompt unless Dev has enabled "review all writes" mode.

### 5.9 Security layer (FR-SEC)

- **FR-SEC-01.** Every tool call from any agent SHALL be evaluated by the security layer before execution.
- **FR-SEC-02.** Rules SHALL be ordered, regex-based decision rules with three possible outcomes: `allow`, `deny`, `prompt`. First match wins.
- **FR-SEC-03.** Two rule scopes SHALL exist: **session** (lives until VS Code exit) and **global** (`~/.kodo/security.json`, persists until Dev removes).
- **FR-SEC-04.** Built-in default rules SHALL be shipped, including denial of operations outside the project root, denial of `rm -rf /`, prompts for arbitrary network calls, and allow rules for common safe operations (`ls`, `cat`, `pytest`, `npm test`, etc.). The full default ruleset is enumerated in the design.
- **FR-SEC-05.** When a rule yields `prompt`, the server SHALL emit an `approval_request` event over the WebSocket and pause the calling agent until a response arrives.
- **FR-SEC-06.** In autonomous mode, the security layer SHALL allow all tool calls (Dev Proxy default = Allow). The Dev acknowledges this risk by enabling autonomous mode.
- **FR-SEC-07.** Dev can add session and global rules from the WebView.

### 5.10 Mirror & checkpoints (FR-MIR)

- **FR-MIR-01.** A git mirror SHALL live at `<project>/.kodo/checkpoints/`. The mirror is initialised by `Kodo: Init Project` and is a regular git repository, not a worktree.
- **FR-MIR-02.** The mirror SHALL store all generated artifacts under `gen/`, all `.kd` files under `src/`, plus a session metadata file.
- **FR-MIR-03.** A checkpoint SHALL be created automatically each time an artifact is completed and promoted (FR-WKS-10/15) — one commit per promoted artifact — with a commit message identifying the artifact (e.g., `<project_code>/<responsibility_code>/<type>: <session_id> → <artifact_id>`).
- **FR-MIR-04.** Dev can list checkpoints and roll back to any prior checkpoint via a WebView control. Rollback overwrites the mirror's working tree and the corresponding files in `<project>/src/` and `<project>/gen/`.
- **FR-MIR-05.** MVP does NOT commit to the main project repo automatically. Dev manages main-repo commits manually.

### 5.11 Autonomous mode (FR-AUT)

- **FR-AUT-01.** Autonomous mode is a per-session toggle in the WebView.
- **FR-AUT-02.** When active: `ask_user` is withheld entirely from every agent's tool set, including the Guide's, because there is no user to answer (an agent that would have asked must assume-and-document, or `escalate_blocker` if truly blocked); every `request_user_review_artifact` call is auto-accepted by the engine, which synthesizes the user's acceptance and returns immediately without surfacing a `prompt.approval`; `rollback` proceeds without the interactive confirmation; the security layer auto-allows tool calls whose rule evaluation would otherwise yield `prompt`; LLM rate-limit pauses become silent waits instead of paging the Dev; STOP remains available. Tool availability is filtered per mode by the agent registry from each tool's `ToolSpec.autonomous_mode`; an agent's prose behavior does not otherwise change between modes.
- **FR-AUT-03.** Errors that cannot be auto-resolved (auth failures, billing) SHALL still page the Dev.

### 5.12 State, settings, memory (FR-STA)

- **FR-STA-01.** Transient per-agent state SHALL live at `~/.kodo/transient/<project-hash>/<session-id>/<agent>.jsonl`. Each agent appends one record per LLM call (request hash, response hash, usage).
- **FR-STA-02.** On server crash, on restart, the workflow engine SHALL detect the most recent transient state and offer to resume the interrupted agent's last call.
- **FR-STA-03.** "Memory" SHALL live as `.kd` files under `<project>/src/.memory/`. These are committed to the main repo by Dev.
- **FR-STA-04.** Agents SHALL be able to propose memory updates as ordinary file writes (reviewed_artifact_id to security layer); the writes appear in the WebView as file events for Dev review.
- **FR-STA-05.** Settings SHALL load with precedence: project `<project>/.kodo/settings.json` > user `~/.kodo/settings.json` > built-in defaults.
- **FR-STA-06.** VS Code workspace settings SHALL only carry VSIX-side concerns (server binary path override, log level).

### 5.13 Cost & usage display (FR-COS)

- **FR-COS-01.** The WebView SHALL display cumulative session cost in USD, with a per-agent breakdown available in a drawer.
- **FR-COS-02.** No hard cost cap is enforced in MVP. Display only.
- **FR-COS-03.** Each LLM call's cost SHALL be persisted to transient state for post-mortem inspection.

### 5.14 Project layout & lifecycle (FR-PRJ)

- **FR-PRJ-01.** A Kodo project's root SHALL contain: `src/`, `gen/`, `.kodo/` (which in turn contains `kodo.md`; see `_layout.py:ProjectLayout.kodo_md`, moved post-MVP from the project root).
- **FR-PRJ-02.** `kodo.md` SHALL contain at minimum these top-level headings: `# Kodo Project`, `## Toolchain`, `## Components`, `## Settings overrides`. The presence of `# Kodo Project` is the marker that identifies a directory as a Kodo project.
- **FR-PRJ-03.** `src/` SHALL contain `narrative.kd`, `responsibilities.kd`, optionally `.memory/*.kd`, and one subdirectory per component. Each component subdirectory contains `requirements.kd`, `design.kd`, `test_plan.kd`.
- **FR-PRJ-04.** `gen/` SHALL contain one subdirectory per component plus a `tests/e2e/` directory. Layout inside each component subdirectory is owned by the active toolchain plugin.
- **FR-PRJ-05.** `.kodo/` SHALL contain `checkpoints/` (the mirror), `settings.json`, `server.pid`, `security.json` (project-scoped rules), and a `sessions/` directory of session-metadata files.
- **FR-PRJ-06.** `Kodo: Init Project` SHALL refuse to overwrite a non-empty workspace unless `--force` is passed.

### 5.15 Behavior testing principle (FR-TST)

This section is load-bearing. The Test Designer, Test Design Critic, Test Coder, and Code Reviewer agents are constrained by it.

- **FR-TST-01.** Generated tests SHALL validate observable behavior — i.e., that an input or event produces the expected externally visible outcome (a state transition, a placed order, a written record). Tests SHALL NOT assert on call counts, internal call orderings, or private-method invocations.
- **FR-TST-02.** Mocks SHALL be used only at clearly identified system boundaries (external HTTP services, the broker API, the wall clock). Mocks of internal collaborators are forbidden.
- **FR-TST-03.** The Test Design Critic SHALL reject any test plan that contains call-count assertions or internal-mock-based scenarios, with feedback referencing this requirement.
- **FR-TST-04.** The end-to-end test SHALL exercise the full system against the highest-fidelity sandboxed boundary available (E\*TRADE sandbox for the MVP exit-ticket project). It SHALL NOT mock internal components.

### 5.16 Virtual workspace (FR-WKS)

The virtual workspace is the exclusive mechanism through which agents produce and consume named artifacts. It replaces agent-facing use of `fileio_write_file` and `fileio_read_file` for all artifact types, providing codename tagging, supersession semantics, and a complete audit trail.

- **FR-WKS-01.** The workspace SHALL be the exclusive mechanism through which agents publish and retrieve artifacts. Agents SHALL NOT hold `fileio_write_file` or `fileio_read_file` in their declared tool lists. The workspace tools (`publish_artifact`, `read_artifact`) supersede agent-facing fileio for all artifact types defined in FR-WKS-03.

- **FR-WKS-02.** The workspace SHALL expose two MCP tools — `publish_artifact` and `read_artifact` — whose JSON schemas are the authoritative specification maintained as `ToolSpec`s in `src/kodo/toolspecs/_publish_artifact.py` and `src/kodo/toolspecs/_read_artifact.py` in the Kodo source tree.

- **FR-WKS-03.** The known artifact types are: `narrative`, `architecture`, `requirements`, `plan`, `functional-design`, `design-plan`, `tech-stack`, `test-plan`, `code`, `test`, `feedback`. The `plan` type is project-wide and authored by Planner (FR-AGT-PL); its base directory is `src/plan/`. New types may be introduced by adding an enum value to both schemas; no other registration step is required.

- **FR-WKS-04.** Each artifact SHALL carry: a UUID v4 (`id`), `type`, `author` (the name of the agent that published it), `project_code` (PROJECTCODE), `responsibility_code` (RESPONSIBILITYCODE), optional `requirement_ids` list (each formatted `PROJECTCODE_RESPONSIBILITYCODE_REQUIREMENTCODE`), text `content`, optional `filename_hint` (leaf filename only, no path), optional `supersedes` list (IDs of artifacts being retired), optional `reviewed_artifact_id` (for type `feedback`: the ID of the artifact being reviewed), optional `verdict` (for type `feedback`: `accepted` or `rejected`), optional `concerns` (for type `feedback` with `verdict=rejected`: a list of structured concern objects — see FR-WKS-07), optional `metadata` (string key-value pairs for supplementary context), and a `created_at` timestamp assigned by the workspace engine at publish time. `author` is required on every `publish_artifact` call. `reviewed_artifact_id`, `verdict`, and `concerns` are required on every `feedback` artifact with `verdict=rejected`.

- **FR-WKS-05.** Project-wide artifacts (`narrative`, `architecture`, `design-plan`, `tech-stack`) SHALL set `responsibility_code` equal to `project_code`. Per-responsibility artifacts SHALL set `responsibility_code` to the RESPONSIBILITYCODE of the responsibility they belong to.

- **FR-WKS-06.** On `publish_artifact`, the workspace engine SHALL: assign a UUID, record `created_at`, persist the artifact to `.kodo/workspace/{project_code}/{responsibility_code}/{artifact_id}_{filename_hint}` (using the artifact ID alone when `filename_hint` is absent), update the live index, append an event-log entry, and return the artifact ID. If `supersedes` is non-empty, each listed artifact SHALL be retired atomically in the same operation (see FR-WKS-09).

- **FR-WKS-07.** Artifacts of type `feedback` SHALL always carry `reviewed_artifact_id` (a live artifact ID), `verdict` (`accepted` or `rejected`), and — when `verdict` is `rejected` — a non-empty `concerns` list. The workspace engine SHALL reject any `feedback` publish that violates these rules, returning a structured error before writing anything. Each concern object SHALL carry at minimum `kind` and `description`; `first_line`, `last_line`, and `excerpt` are optional locators that identify the problematic block within the artifact content by line range and quoted text. Valid `kind` values are defined per critic agent in its prompt; the shared base vocabulary is: `ambiguity`, `contradiction`, `gap`, `compound`, `uncaptured_assumption`, `unmeasurable`, `missing_actor`, `requirement_uncovered`, `interface_mismatch`, `multiple_responsibilities`. Critic agents SHALL NOT invent kinds outside their defined vocabulary.

- **FR-WKS-07a.** Review state for any artifact SHALL be derivable from the artifact graph without mutable fields. Specifically: (a) an artifact has been reviewed if at least one `feedback` artifact exists with `reviewed_artifact_id` equal to its ID; (b) it passed review if that feedback's `verdict` is `accepted`; (c) the review count for a lineage is the total number of `feedback` artifacts whose `reviewed_artifact_id` field traces through the supersession chain (feedback on R1, R2, and R3 all count toward the lineage's review history). No artifact's fields are modified after publication.

- **FR-WKS-08.** `read_artifact` SHALL accept the following filters: `artifact_id`, `author`, `project_code`, `responsibility_code`, `requirement_id`, `type`. All supplied filters are combined with AND. At least one filter SHALL be required; a call with no filters SHALL be rejected. Only live artifacts are returned. An optional `include_content` flag (default `true`) omits the `content` field when `false`, for efficient large-listing use cases.

- **FR-WKS-09.** Retiring an artifact SHALL: remove it from the live index, move its on-disk file to `.kodo/workspace/.retired/{artifact_id}/{exact_filename_with_extension}` (the per-id directory preserves the original leaf filename so audit tooling and diff viewers can key off the extension), and append a `retired` entry to the event log. Retirement is permanent through the workspace API; there is no un-retire operation. A `supersedes` list of `[A, B]` in a single `publish_artifact` call retires A and B atomically with the creation of the new artifact; this covers 1-to-1 replacement, 1-to-N splits (multiple calls each listing the same old ID), and N-to-1 merges (one call listing multiple old IDs).

- **FR-WKS-10.** Materialization of an artifact at its `src/` or `gen/` path defined by FR-PRJ-03 / FR-PRJ-04 SHALL occur only on completion, via the Promoter mechanism specified in [STATE_AND_LIFECYCLE.md §8](STATE_AND_LIFECYCLE.md). Completion is signalled by `report_artifact_completed` (FR-WKS-15); it requires critic acceptance (FR-WKS-07a) and, in interactive mode, the user's acceptance at the review gate (FR-WF-05). On completion the Promoter materializes the artifact to `src/`/`gen/`, mirrors it with a checkpoint commit and a `.kodo.json` sidecar, marks the index entry `completed`, and removes the staging file from the workspace. Before completion the artifact exists only under `.kodo/workspace/`; toolchain plugins, sub-agents, and the wire protocol SHALL NOT assume any presence under `src/`/`gen/` for in-flight artifacts.

- **FR-WKS-11.** Promoter SHALL also propagate retirement of accepted artifacts to `src/`/`gen/`. When an accepted artifact is later retired with a superseding artifact, Promoter overwrites the corresponding `src/`/`gen/` file with the successor's content and commits the change to the mirror. When retired with no successor, Promoter deletes the file and commits the deletion. The detailed sequence is in STATE_AND_LIFECYCLE.md §8.1.

- **FR-WKS-12.** The workspace SHALL maintain an append-only event log at `.kodo/workspace/events.jsonl`. Each JSON line SHALL record: `timestamp`, `event` (`published` or `retired`), `artifact_id`, `type`, `author`, `project_code`, `responsibility_code`, `requirement_ids`, `supersedes`, `reviewed_artifact_id`, `verdict`, and `filename_hint`. The `concerns` list is not duplicated in the event log; it is retrievable from the artifact file itself. The event log combined with the on-disk artifacts SHALL be sufficient to reconstruct the full sequence of artifact lifecycle events for any session.

- **FR-WKS-13.** The project index (`ProjectIndex`) SHALL be the single runtime source of truth for the catalog and lifecycle state of every artifact, in-flight and completed. It holds metadata only — `id`, `type`, `project_code`, `responsibility_code`, `requirement_ids`, `filename_hint`, `created_at`, `state` (`in_flight` | `completed`), `location` (absolute path on disk), `supersedes`, `author`, and, for reviewed artifacts, `verdict` and `reviewed_artifact_id`; artifact content lives only on disk at `location`. The index is maintained live in memory and is NEVER persisted as its own file — it is a reflection of on-disk state. The Workspace owns staging mechanics and updates the index on every publish, supersession, and completion; it reads the index when it needs to locate an artifact.

- **FR-WKS-14.** On server startup, the engine SHALL reconstruct `ProjectIndex` from durable on-disk state before accepting any workspace tool calls: completed entries from the mirror's `.kodo.json` sidecars (state `completed`), in-flight entries from the workspace staging files (state `in_flight`). All data necessary to reconstruct the index SHALL be persisted in those two places; there is no separate index file to validate or repair.

- **FR-WKS-15.** Completion SHALL be an explicit signal, `report_artifact_completed(artifact_id)`, held only by critics and solo agents — never by an author of an author/critic pair about its own work. The signal asserts that the artifact has passed all of its gates: critic acceptance (FR-WKS-07a) and, in interactive mode, the user review gate (FR-WF-05). On the signal the engine resolves the toolchain from the Tech Stack, runs the Promoter (FR-WKS-10), flips the index entry to `completed`, and deletes the staging file. Until an artifact is reported completed, `query_frontier` reports it as in-flight; publication alone never makes an artifact complete.

## 6. Non-functional requirements

- **NFR-01. Reliability.** Server crash recovery: an interrupted workflow SHALL be resumable from the most recent completed-artifact checkpoint or, if no artifact has been completed, from the most recent transient checkpoint within the current agent.
- **NFR-02. Performance.** End-to-end latency from Dev keystroke (after a prompt is submitted) to the first streamed token in the WebView SHALL be under 1.5 seconds at the median, network conditions permitting.
- **NFR-03. Portability.** The server binary SHALL run on Windows 10+, macOS 12+, and modern Linux without external runtime dependencies beyond `git`.
- **NFR-04. Privacy.** No telemetry SHALL be sent off-machine. The Anthropic API token is the only secret leaving the host, and only to Anthropic.
- **NFR-05. Observability.** The server SHALL write a rotating log file at `<project>/.kodo/logs/server.log`; LLM payloads SHALL be redacted to hashes by default, full-payload logging is opt-in via settings.
- **NFR-06. Security.** Server binds only to loopback. Token never written to disk by the server. PID file contains no secrets.
- **NFR-07. Code conventions.** Server code follows `src/kodo/CLAUDE.md` (private mangled members, no `Any`/`Optional`/`List`/etc., behavior-tested).

## 7. Out of scope for MVP

- Adopting an existing codebase (Kodo only builds green-field).
- Front-end / UI generation. Back-end only.
- LLM providers other than Anthropic.
- Multiple workflows or user-selectable orchestration — only one Guide prompt ships in MVP, driving the canonical sequence (FR-ORCH-06).
- Multi-worker concurrency; one worker only.
- Cost caps, budgets, hard limits.
- Telemetry, analytics, error reporting to a remote service.
- Plugin install UX — plugins are bundled in the binary.
- Hosted / cloud / multi-tenant deployment.
- Backpressure on the WebSocket (best-effort buffering only).
- Auto-commit to the main project repo.

## 8. Acceptance test (gates MVP release)

The following procedure SHALL be runnable on a clean machine and result in a working E\*TRADE sandbox bot:

1. Install VSIX from a packaged release. First activation downloads the server binary.
2. Set `KODO_ANTHROPIC_API_KEY` in the shell environment before launching VS Code. On first activation the extension reads the env var, stores it in SecretStorage, and passes it to the server subprocess.
3. Open an empty workspace, run `Kodo: Init Project`.
4. Submit prompt: *"Build an algorithmic stock trading bot for E\*TRADE that places orders based on a configurable strategy."*
5. Iterate with Kodo through Narrative, Responsibilities, per-responsibility Requirements, Plan, and the per-responsibility Design / Test Plan moments — review gates are reached and Dev approves them.
6. Test Coder produces failing tests; Coder iterates until they pass; Code Reviewer accepts.
7. End-to-end test passes against E\*TRADE sandbox credentials (Dev-supplied).
8. Final approval; mirror history shows checkpoints for every gate.
9. The bot can be run by hand (`python -m generated_bot ...` or equivalent) and places at least one sandbox trade.

Failure of any step blocks MVP release.

## 9. Open issues

- **`.kd` file extensions.** Decision deferred: MVP treats `.kd` as Markdown. Tagged-Markdown extensions are post-MVP.
- **Memory write policy.** Whether memory updates require an approval gate of their own, or flow through normal file-write security rules. Current decision: normal security rules + file event in WebView.
- **Plugin model.** Plugin discovery is by import (bundled). Out-of-tree plugins are not supported in MVP.
