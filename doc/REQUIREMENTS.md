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
|---|---|
| Project | A directory containing `kodo.md`, `src/`, `gen/`, and `.kodo/`. The unit of work. |
| `.kd` file | Markdown file under `src/` describing some aspect of the project (narrative, responsibilities, requirements, design, test plan). For MVP, `.kd` is plain Markdown. |
| `kodo.md` | Project manifest at the project root. Required headings declare a Kodo project. |
| Narrative | Top-level natural-language description of the end product. The "north star". One per project. |
| Responsibility | A single, named area of behavior the product must deliver. |
| Component | The implementation unit for one responsibility — typically one package/module containing a main class plus satellites. |
| Workflow | The full sequence of stages that take a project from idea to working code. MVP ships exactly one workflow. |
| Stage | A coarse phase of the workflow (Narrative, Architecture, Requirements, Design, Test Plan, Test Coding, Implementation, Final). |
| Agent | A pluggable component with a declared role, a system prompt, declared capabilities, and a model preference. |
| Author / Reviewer pair | Two agents collaborating on a stage: the Author produces an artifact, the Reviewer accepts or returns feedback. |
| Approval Gate | A point in the workflow where the Dev must Agree (or provide feedback) before the next stage starts. |
| Mirror | A `git` repository inside `<project>/.kodo/checkpoints/` used to checkpoint generated artifacts. |
| Checkpoint | A commit in the mirror representing a coherent state Dev can return to. |
| Memory | Distilled long-term project context written as `.kd` files in the main repo. Committed by Dev. |
| Transient state | Per-agent in-flight conversation/retry state stored under `~/.kodo/transient/<project-hash>/<session>/`. Disposable. |
| Toolchain plugin | A pluggable component that knows how to `init`, `add_dependency`, `build`, `test`, and `format` for a target language (Python, Node for MVP). |
| LLM plugin | A pluggable component that fronts a model provider (Anthropic for MVP) and exposes a uniform capability set. |
| MCP | Model Context Protocol. MVP ships in-process `tools/fileio` and `tools/shell` MCP servers. |
| Security layer | The single mediator for every tool call request, governed by user-defined and built-in regex rules. |
| STOP | An always-available control that immediately cancels all in-flight agent work for the project. |

## 4. MVP exit ticket

**Kodo MVP is "done" when Kodo can be used to build, end-to-end, an algorithmic stock trading bot that interacts with the E\*TRADE API, with all generated tests passing.** This includes:

- A complete narrative, set of responsibilities, per-component requirements, functional designs, and test plans authored interactively with Kodo.
- All tests written by Kodo's Test Coder pass when executed by the Python toolchain plugin.
- The end-to-end test exercises the bot against an E\*TRADE sandbox endpoint (no real-money trades during Kodo validation).
- Dev can replay the full session via mirror checkpoints.

This is the only acceptance criterion that gates MVP release. Per-feature requirements below exist to make this attainable.

## 5. Functional requirements

### 5.1 VS Code extension (FR-VSIX)

- **FR-VSIX-01.** The extension SHALL be implemented in TypeScript and packaged as a `.vsix`.
- **FR-VSIX-02.** On activation, the extension SHALL ensure a Kodo Server binary is present under `~/.kodo/bin/` and matches the version expected by the extension; if missing or mismatched, the extension SHALL download the matching binary from the published GitHub release for the current OS/arch and store it under `~/.kodo/bin/`.
- **FR-VSIX-03.** The extension SHALL launch one Kodo Server subprocess per VS Code window, passing the project root as a CLI argument.
- **FR-VSIX-04.** The extension SHALL read the Anthropic API token from VS Code SecretStorage and pass it to the server via a non-persisted handshake (env var on subprocess spawn).
- **FR-VSIX-05.** The extension SHALL provide a `Kodo: Init Project` command that creates `kodo.md`, `src/`, `gen/`, and `.kodo/` in the workspace root if absent.
- **FR-VSIX-06.** The extension SHALL provide a `Kodo: Open Panel` command that opens a WebView showing the conversation, file events, approval prompts, and usage panel.
- **FR-VSIX-07.** The extension SHALL provide a globally visible **STOP** control inside the WebView that cancels all running agent work for the project.
- **FR-VSIX-08.** The extension SHALL register URL handlers for diff and file links so that clicking a diff link in the WebView opens VS Code's native diff editor, and clicking a file link opens the file in the editor.
- **FR-VSIX-09.** The extension SHALL gracefully handle server-side disconnects by displaying a reconnect status and resuming the message stream when the server returns; no Dev input is lost across reconnects.

### 5.2 Kodo Server lifecycle (FR-SRV)

- **FR-SRV-01.** The server SHALL be implemented in Python and shipped as a single PyInstaller binary for Windows, macOS, and Linux.
- **FR-SRV-02.** A single server instance SHALL be bound to exactly one project (the path passed at launch). Multi-project servers are explicitly out of scope.
- **FR-SRV-03.** The server SHALL listen on `127.0.0.1:9042` for WebSocket connections from the extension. The port is configurable via CLI flag for development.
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

- **FR-AGT-01.** An agent SHALL be defined by a single Markdown file at `kodo/agents/<name>.<model>.md`. The file SHALL have YAML frontmatter declaring `name` and `tools` (a list of MCP tool names the agent may invoke), and a body containing the full system prompt for the named model. Agents are not Python classes or plugins — they have no `role`, no typed `inputs`/`outputs`, and no capability set beyond the tool list.
- **FR-AGT-02.** Each agent file SHALL be a complete, self-contained prompt for the model encoded in its filename. Multiple files MAY exist for a single `name` (one per model variant); each file is independent — there is no shared "common" body across variants.
- **FR-AGT-03.** Agents SHALL be looked up by `(name, model)` at runtime. Looking up an agent for a model with no matching file SHALL be a hard error.
- **FR-AGT-04.** Agents SHALL be invoked only by workflow code; no direct agent-to-agent calls. The workflow function is the sole orchestration mechanism.
- **FR-AGT-05.** Each Author/Reviewer pair SHALL iterate until the Reviewer signals acceptance or a configurable iteration limit is reached (default 5). On limit reached: in interactive mode, the Dev is alerted and asked to intervene; in autonomous mode, the Author's last output is accepted as-is and the event is logged.
- **FR-AGT-06.** Agents SHALL ask clarifying questions liberally. Disambiguation is preferred over assumption.

#### 5.5.2 Required agents for MVP

For each agent below, MVP SHALL include one markdown file under `kodo/agents/` for the default model (`claude-sonnet-4-6` for all agents except Dev Proxy, which uses `claude-haiku-4-5-20251001`). The "reads / writes" annotations describe what the workflow function passes the agent and where its output lands; they are documentation, not declared types.

- **FR-AGT-NA.** **Narrative Author** — reads: Dev prompt. Writes: `src/narrative.kd`.
- **FR-AGT-AR.** **Architect** — reads: narrative. Writes: `src/responsibilities.kd` (list with names + brief descriptions) and component scaffolding (one directory per component under `src/<component>/` with empty `requirements.kd`, `design.kd`, `test_plan.kd`).
- **FR-AGT-RA.** **Requirements Author** — reads: narrative + responsibility description. Writes: `src/<component>/requirements.kd`.
- **FR-AGT-RR.** **Requirements Reviewer** — reads: same. Verifies for ambiguity, contradiction, and gaps; produces feedback or "accept".
- **FR-AGT-FD.** **Functional Designer** — reads: requirements. Writes: `src/<component>/design.kd` with interfaces and behaviors.
- **FR-AGT-FC.** **Functional Design Critic** — verifies against requirements and SOLID; produces feedback or "accept".
- **FR-AGT-TD.** **Test Designer** — reads: requirements + design. Writes: `src/<component>/test_plan.kd` plus a flag identifying which test belongs to the end-to-end suite.
- **FR-AGT-TC.** **Test Design Critic** — verifies test plan for contradictions, coverage gaps, and behavior-vs-implementation focus (FR-TST).
- **FR-AGT-TX.** **Test Coder** — reads: test plan + design. Writes: test source files under `gen/<component>/tests/` and `gen/tests/e2e/`. All tests SHALL be expected-to-fail when first generated.
- **FR-AGT-CO.** **Coder** — reads: design + failing tests. Writes: implementation files under `gen/<component>/`. Iterates until tests pass.
- **FR-AGT-CR.** **Code Reviewer** — gate-keeps Coder output; signals accept or feedback.

#### 5.5.3 Dev Proxy (autonomous mode)

- **FR-AGT-DP-01.** Dev Proxy SHALL be defined as an agent markdown file (per FR-AGT-01) whose system prompt establishes the role of "autonomous-mode proxy" and accepts a list of user-defined rules at runtime. It SHALL respond to events that would otherwise interrupt the user when autonomous mode is active. Rules are natural-language statements that the proxy applies to the event with contextual judgement (pattern matching against event content, prior decisions, project state).
- **FR-AGT-DP-02.** Default action for events not clearly covered by any rule: **Allow / Agree**.
- **FR-AGT-DP-03.** Dev Proxy rules and (optionally) its preferred model SHALL be configurable per project in `.kodo/settings.json` under `dev_proxy`.
- **FR-AGT-DP-04.** Dev Proxy LLM calls SHALL be reported through the standard usage stream (FR-COS), so the cost of autonomous runs is visible.

### 5.6 Workflow & approval (FR-WF)

- **FR-WF-01.** MVP SHALL implement exactly one workflow that runs all eleven agents in the sequence: Narrative → Architecture → (per component: Requirements → Design → Test Plan) → Test Coding → Implementation → Final.
- **FR-WF-02.** The workflow engine SHALL use a single async task queue with **exactly one worker** for MVP.
- **FR-WF-03.** Components are independent during the per-component stages and during their own implementation. The workflow SHALL serialise their work in a deterministic order (alphabetical by component name) for MVP.
- **FR-WF-04.** Integration test scheduling SHALL respect a dependency graph emitted by the Architect: an integration test runs only after every component it depends on has been implemented and unit-tested.
- **FR-WF-05.** Approval Gates SHALL exist at the following points: after Narrative; after Responsibilities/components list; after each component's Requirements; after each component's Design; after each component's Test Plan; after each component's implementation diff is produced; one final approval after E2E test passes.
- **FR-WF-06.** An approval prompt presented to the Dev SHALL offer exactly two affirmative actions: **Agree** (no comments), or **Provide Feedback** (free-form text). There SHALL be no explicit Reject button. Submitting feedback SHALL re-run only the responsible Author/Reviewer pair for that artifact.
- **FR-WF-07.** A globally visible **STOP** control SHALL cancel all in-flight agent work, abort streaming LLM calls, kill child MCP processes, and leave the workflow in a `STOPPED` state from which Dev can resume by re-running the last incomplete stage.
- **FR-WF-08.** The workflow engine SHALL expose, via the wire protocol, the current stage, the current agent, and the current target component.

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
- **FR-MIR-03.** A checkpoint SHALL be created automatically at every Approval Gate, with a commit message identifying the gate and the artifact (e.g., `[narrative] approved`).
- **FR-MIR-04.** Dev can list checkpoints and roll back to any prior checkpoint via a WebView control. Rollback overwrites the mirror's working tree and the corresponding files in `<project>/src/` and `<project>/gen/`.
- **FR-MIR-05.** MVP does NOT commit to the main project repo automatically. Dev manages main-repo commits manually.

### 5.11 Autonomous mode (FR-AUT)

- **FR-AUT-01.** Autonomous mode is a per-session toggle in the WebView.
- **FR-AUT-02.** When active: Dev Proxy auto-handles approval requests and tool-call prompts (default Allow); LLM rate-limit pauses become silent waits instead of paging the Dev; STOP remains available.
- **FR-AUT-03.** Errors that cannot be auto-resolved (auth failures, billing) SHALL still page the Dev.

### 5.12 State, settings, memory (FR-STA)

- **FR-STA-01.** Transient per-agent state SHALL live at `~/.kodo/transient/<project-hash>/<session-id>/<agent>.jsonl`. Each agent appends one record per LLM call (request hash, response hash, usage).
- **FR-STA-02.** On server crash, on restart, the workflow engine SHALL detect the most recent transient state and offer to resume the interrupted agent's last call.
- **FR-STA-03.** "Memory" SHALL live as `.kd` files under `<project>/src/.memory/`. These are committed to the main repo by Dev.
- **FR-STA-04.** Agents SHALL be able to propose memory updates as ordinary file writes (subject to security layer); the writes appear in the WebView as file events for Dev review.
- **FR-STA-05.** Settings SHALL load with precedence: project `<project>/.kodo/settings.json` > user `~/.kodo/settings.json` > built-in defaults.
- **FR-STA-06.** VS Code workspace settings SHALL only carry VSIX-side concerns (server binary path override, log level).

### 5.13 Cost & usage display (FR-COS)

- **FR-COS-01.** The WebView SHALL display cumulative session cost in USD, with a per-agent breakdown available in a drawer.
- **FR-COS-02.** No hard cost cap is enforced in MVP. Display only.
- **FR-COS-03.** Each LLM call's cost SHALL be persisted to transient state for post-mortem inspection.

### 5.14 Project layout & lifecycle (FR-PRJ)

- **FR-PRJ-01.** A Kodo project's root SHALL contain: `kodo.md`, `src/`, `gen/`, `.kodo/`.
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

## 6. Non-functional requirements

- **NFR-01. Reliability.** Server crash recovery: an interrupted workflow SHALL be resumable from the most recent Approval Gate or, if no gate has been crossed, from the most recent transient checkpoint within the current agent.
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
- Workflow selection — only the full eleven-agent workflow ships.
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
2. Set Anthropic API token via VS Code SecretStorage prompt.
3. Open an empty workspace, run `Kodo: Init Project`.
4. Submit prompt: *"Build an algorithmic stock trading bot for E\*TRADE that places orders based on a configurable strategy."*
5. Iterate with Kodo through Narrative, Responsibilities, per-component Requirements / Design / Test Plan stages — Approval Gates are reached and Dev approves them.
6. Test Coder produces failing tests; Coder iterates until they pass; Code Reviewer accepts.
7. End-to-end test passes against E\*TRADE sandbox credentials (Dev-supplied).
8. Final approval; mirror history shows checkpoints for every gate.
9. The bot can be run by hand (`python -m generated_bot ...` or equivalent) and places at least one sandbox trade.

Failure of any step blocks MVP release.

## 9. Open issues

- **`.kd` file extensions.** Decision deferred: MVP treats `.kd` as Markdown. Tagged-Markdown extensions are post-MVP.
- **Memory write policy.** Whether memory updates require an approval gate of their own, or flow through normal file-write security rules. Current decision: normal security rules + file event in WebView.
- **Plugin model.** Plugin discovery is by import (bundled). Out-of-tree plugins are not supported in MVP.
