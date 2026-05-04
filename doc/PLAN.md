# Kodo MVP — Development Plan

> Status: draft for review. References: [REQUIREMENTS.md](REQUIREMENTS.md), [DESIGN.md](DESIGN.md). Exit ticket: build the E\*TRADE bot end-to-end with Kodo.

---

## 0. Approach

### 0.1 Starting point

**Discard the existing `src/kodo/` scaffolding** (`server/_app.py`, `_orchestrator.py`, `_worker.py`, the placeholder `workflow/`). They were prototyped against an HTTP + multiprocessing design that conflicts with the websocket + single-asyncio-worker design in [DESIGN.md](DESIGN.md). The Anthropic LLM stub, the MCP registry shape, and the `tools/fileio` + `tools/shell` MCP servers can be salvaged as references but will be reimplemented to match the design's interfaces.

The first milestone reorganises the repo to the layout in DESIGN section 1.1.

### 0.2 Sequencing principles

1. **Build the spine before the limbs.** Wire protocol, workflow engine skeleton, and one trivial agent end-to-end before adding more agents.
2. **Dogfood early.** As soon as the workflow can run a single agent end-to-end, use Kodo (with stubs) to author its own remaining agents' prompts. By M5 we should be using Kodo's narrative authoring on the E\*TRADE bot.
3. **Behavior tests at every milestone.** Each milestone exits with a working observable demo, not just passing unit tests. Aligns with FR-TST and forces real integration.
4. **The exit ticket is the only release gate.** Internal milestones are progress markers; only successful E\*TRADE bot generation ships v1.0.

### 0.3 Development conventions

- Server code follows `src/kodo/CLAUDE.md`: private name-mangling, no `Any`/`Optional`/`List`, behavior-only tests, modules and `py.typed` everywhere.
- Type-checked with `mypy --strict`. Lint with `ruff`.
- Tests with `pytest`; behavior-only assertions; mocks only at HTTP/process boundaries (matches FR-TST so we eat our own dog food).
- Extension code uses TypeScript strict, `eslint`, `vitest`.
- Release pipeline produces (a) PyInstaller binary per OS/arch, (b) `.vsix` with a release-version manifest pointing at the binary.

---

## 1. Milestones

Each milestone has: deliverables, exit criteria, rough size, and dependencies.

### M0 — Repo reset & toolchain (small, ~2 days)

**Deliverables**
- Remove existing scaffolding except `pyproject.toml`, `LICENSE`, `README.md`, `src/kodo/CLAUDE.md`, `hatch_build.py`, `scripts/*`.
- New empty package layout per DESIGN §1.1.
- CI: `mypy --strict`, `ruff`, `pytest` running on every push.
- New repo `kodo-vsix` initialised with TypeScript + Vite + Preact + esbuild for VSIX bundling.
- Documented dev-loop: how to run server locally pointed at a fake project; how to launch the WebView in dev mode.

**Exit criteria**
- `pytest` runs with zero tests passing (and zero failures).
- `kodo-server --help` prints CLI usage.
- `code --extensionDevelopmentPath=./kodo-vsix` opens VS Code with a "Hello Kodo" command registered.

**Requirements covered**
- No FR coverage — preparatory milestone. Establishes the engineering substrate that lets later milestones honor **NFR-07** (code conventions per `src/kodo/CLAUDE.md`).

---

### M1 — Wire spine: server + extension can shake hands and stream (medium, ~1 week)

**Deliverables**
- `kodo.transport`: envelope, message catalog, outbox, aiohttp WS handler.
- `kodo.server.{_app,_lifecycle,_config}`: launches WS on `127.0.0.1:9042`, writes PID file, handles graceful shutdown.
- Extension: `server-launcher.ts` finds/downloads/launches binary; `ws-client.ts` talks the protocol; minimal WebView (Preact) shows a "ping" button that round-trips.
- Token-streaming demo: a fake `agent.tokens` stream of 200 chunks visible in the WebView.

**Exit criteria**
- Open VS Code → activate extension → server launches → WebView shows green "connected" → ping/pong works → fake stream renders.
- Disconnect WebView (close panel), reconnect: outbox replays missed events.
- `mypy --strict` clean on `kodo.transport` and `kodo.server`.

**Requirements covered**
- VSIX lifecycle: **FR-VSIX-01** (TS extension), **FR-VSIX-03** (one server per window), **FR-VSIX-09** (reconnect tolerance).
- Server lifecycle: **FR-SRV-01** (Python server), **FR-SRV-03** (loopback `:9042`), **FR-SRV-04** (loopback only), **FR-SRV-06** (PID file), **FR-SRV-07** (graceful shutdown).
- Wire protocol: **FR-WS-01** (envelope), **FR-WS-02** (request/response/event/streaming), **FR-WS-04** (replay on reconnect). Message catalog (**FR-WS-03**) seeded with `hello`, `state`, fake `agent.tokens`.
- NFRs: **NFR-02** (first-token latency), **NFR-06** (loopback bind), **NFR-07** (conventions).

---

### M2 — LLM plugin + project layout + workflow skeleton (medium, ~1 week)

**Deliverables**
- `kodo.llms.anthropic`: `_claude.py`, `_cache.py`, `_retry.py`, `_usage.py`. Streaming, cache-control breakpoints, 2/8/32s retry, usage accounting.
- `kodo.project.{_layout,_manifest}`: validates Kodo project structure, parses `kodo.md`.
- `kodo.workflow.{_engine,_stages,_session}`: single-worker queue, stage machine, no agents wired yet.
- `kodo.state._transient`: append-only JSONL, session metadata.
- Extension: `Kodo: Init Project` command creates the layout.
- One end-to-end test: launch server, call into Anthropic with a trivial prompt, see streamed tokens in the WebView, see a transient JSONL written. Cost shows in UsagePanel.

**Exit criteria**
- E2E test passes against the real Anthropic API with a small prompt.
- `kodo.md` validation passes/fails as expected.
- A `transient/<hash>/<session>/agents/raw.jsonl` file exists after a session.
- Cost panel shows non-zero cumulative cost.

**Requirements covered**
- LLM plugin: **FR-LLM-01..07** complete (streaming, cache, retries, usage, cancel, quota errors).
- Project layout & lifecycle: **FR-PRJ-01..06** complete; **FR-VSIX-05** (Init Project command), **FR-VSIX-04** (token via SecretStorage → server env).
- Server: **FR-SRV-05** (git on PATH), **FR-SRV-08** (best-effort persistence underpinnings).
- Workflow skeleton: **FR-WF-02** (single async worker), **FR-WF-08** (state events) — partial, no agents wired yet.
- State: **FR-STA-01** (transient JSONL layout), **FR-STA-02** (resume detection groundwork), **FR-STA-05** (settings precedence), **FR-STA-06** (workspace-settings scope).
- Cost: **FR-COS-01..03** complete.
- Wire protocol: **FR-WS-03** extended with `usage.update`, `state`.
- NFRs: **NFR-04** (no telemetry), **NFR-05** (server log file).

---

### M3 — First agents + approval gates + mirror (large, ~2 weeks)

**Deliverables**
- `kodo.agents.{_interface,_registry}` plus the Narrative Author and Architect.
- Author/Reviewer iteration loop (5-iter limit); for M3 use a single critic stub for both agents until M5.
- `kodo.workflow._gates`: approval gate orchestration with feedback re-runs.
- `kodo.mirror.{_repo,_checkpoints}`: git mirror init + checkpoint-on-gate.
- WebView: ApprovalGate card with Agree/Feedback, FileEvent card with diff link, Conversation timeline.
- VS Code diff bridge: clicking "Open diff" opens native diff editor.

**Exit criteria**
- Submit a prompt → Narrative Author writes `src/narrative.kd` → gate fires → click Feedback → revised narrative → click Agree → checkpoint commit appears in `.kodo/checkpoints/` → Architect runs → produces `src/responsibilities.kd` and `responsibilities.dag.json` + per-component skeletons → gate fires.
- Mirror has two checkpoint commits with correct messages.
- Feedback loop demonstrably re-runs the responsible agent pair only.

**Requirements covered**
- Agent contract: **FR-AGT-01** (plugin shape), **FR-AGT-02** (no direct agent-to-agent calls), **FR-AGT-03** (5-iter Author/Reviewer cap), **FR-AGT-04** (clarifying questions).
- Agents: **FR-AGT-NA** (Narrative Author), **FR-AGT-AR** (Architect, including DAG emission).
- Workflow: **FR-WF-01** (workflow definition begins — narrative + architecture stages live), **FR-WF-05** (gates: narrative, responsibilities), **FR-WF-06** (Agree / Feedback only, no Reject).
- Mirror: **FR-MIR-01..05** complete (separate git repo, checkpoint-on-gate, rollback).
- VSIX: **FR-VSIX-06** (WebView panel), **FR-VSIX-08** (diff bridge).
- Wire protocol: **FR-WS-03** extended with `agent.{started,finished,tokens}`, `file.change`, `approval.{request,respond}`.

---

### M4 — MCP, security layer, toolchain plugin: Python (large, ~2 weeks)

**Deliverables**
- `kodo.mcp._{interface,registry}`: in-process MCP servers exposed to LLM plugin.
- `kodo.tools.{fileio,shell}`: in-process MCP servers (reimplemented per FR-MCP).
- `kodo.security.{_layer,_rules,_store,_defaults}`: rule schema, default ruleset, session/global stores, prompt event emission.
- `kodo.toolchains.python.{_plugin,_pytest}`: init, add_dependency (uv), build, test (pytest), format (ruff).
- WebView: Security prompt card, ShellEvent card.
- Add Requirements Author + Requirements Reviewer + Functional Designer + Functional Design Critic + Test Designer + Test Design Critic + Test Coder agents (prompts authored interactively with M3's Kodo if dogfooding milestone is hit).

**Exit criteria**
- Build a tiny throwaway sample project with Kodo: prompt → narrative → architecture → 1 component (a calculator) → requirements → design → test plan → tests written → tests fail (no impl yet). All artifacts present.
- Security layer denies a `curl evil.example.com` shell command via prompt.
- `pytest` runs and surfaces results in the WebView.

**Requirements covered**
- MCP: **FR-MCP-01..05** complete.
- Security layer: **FR-SEC-01..07** complete (rules, scopes, defaults, prompt event, autonomous bypass).
- Toolchain (Python): **FR-TC-01** (interface), **FR-TC-02** (plugin set), **FR-TC-03** (Python plugin) complete.
- Agents: **FR-AGT-RA** (Requirements Author), **FR-AGT-RR** (Requirements Reviewer), **FR-AGT-FD** (Functional Designer), **FR-AGT-FC** (Functional Design Critic), **FR-AGT-TD** (Test Designer), **FR-AGT-TC** (Test Design Critic), **FR-AGT-TX** (Test Coder).
- Behavior testing: **FR-TST-01..03** enforced via Test Design Critic prompt + rejection rules.
- Workflow: **FR-WF-05** extended (per-component Requirements/Design/Test-Plan gates).
- Wire protocol: **FR-WS-03** extended with `shell.run`, `security.prompt`.
- NFRs: **NFR-06** (security defaults audited).

---

### M5 — Coder + Code Reviewer + Node toolchain + autonomous mode (large, ~2 weeks)

**Deliverables**
- `kodo.agents.{coder,code_reviewer}`: implementation loop and review.
- `kodo.toolchains.node.{_plugin,_vitest}`: parity with Python plugin.
- `kodo.agents.dev_proxy`: small LLM agent driven by configurable natural-language rules; default action Allow.
- WebView: AutonomousToggle, ResumeBanner.
- Resume logic: server detects unfinished session at startup and offers resume.
- `STOP` plumbed end-to-end: cancels worker, cancels LLM stream within 1s, leaves `STOPPED` state.

**Exit criteria**
- Build the M4 calculator end-to-end (tests pass after Coder iterations) in interactive mode.
- Repeat in autonomous mode: zero Dev interactions from prompt to all-tests-pass.
- STOP at any moment leaves the workflow resumable.
- Node toolchain runs the same calculator scenario with `vitest`.

**Requirements covered**
- Agents: **FR-AGT-CO** (Coder), **FR-AGT-CR** (Code Reviewer), **FR-AGT-DP-01..03** (Dev Proxy, default Allow).
- Toolchain (Node): **FR-TC-04** complete (vitest, npm).
- Autonomous mode: **FR-AUT-01..03** complete.
- Workflow: **FR-WF-05** extended (per-component implementation gates), **FR-WF-07** (STOP cancels worker, LLM stream, leaves resumable state), **FR-VSIX-07** (STOP control surface).
- Resume: **FR-STA-02** complete (resume offer at startup), **FR-STA-03..04** (memory file conventions exercised).
- LLM cancellation: **FR-LLM-07** verified end-to-end (≤1s).
- Wire protocol: **FR-WS-03** extended with `stop`, `mode.set`, `session.resume`.
- NFRs: **NFR-01** (crash recovery).

---

### M6 — E\*TRADE bot dogfood + integration tests + e2e (medium-large, ~2 weeks)

**Deliverables**
- Run the full workflow on the E\*TRADE bot prompt: *"Build an algorithmic stock trading bot for E\*TRADE that places orders based on a configurable strategy."*
- Architect emits a multi-component DAG. Integration tests are scheduled per FR-WF-04.
- E2E test against E\*TRADE sandbox (Dev provides sandbox creds via env var; the agent passes them through `tools/shell` runtime env, never written to disk).
- All MVP exit-ticket steps (REQUIREMENTS §8) executed once successfully.

**Exit criteria**
- Bot project at `examples/etrade-bot/` (separate repo) builds, all tests pass, end-to-end places at least one sandbox trade.
- Mirror history shows checkpoints for every gate.
- A second clean run on a different sample idea (e.g. "RSS-to-SQLite ingester") also passes — sanity check that Kodo is not E\*TRADE-overfit.

**Requirements covered**
- Workflow at full scale: **FR-WF-01** complete (full eleven-agent workflow exercised), **FR-WF-03** (deterministic per-component ordering), **FR-WF-04** (DAG-driven integration test scheduling).
- Behavior testing: **FR-TST-04** (E2E test against sandboxed boundary, no internal mocks).
- All eleven agents (**FR-AGT-NA..CR**) plus Dev Proxy exercised in production.
- Acceptance test (REQUIREMENTS §8) executed once successfully — the literal MVP exit ticket.

---

### M7 — Hardening, packaging, release (medium, ~1 week)

**Deliverables**
- PyInstaller builds for Windows (x64, arm64), macOS (x64, arm64), Linux (x64, arm64).
- `.vsix` packaged with version-pinned binary download URLs and SHA-256 checksums.
- README updated with quick-start; CHANGELOG; license check; security layer default rules audited; logs rotate.
- Crash-and-resume test on every supported OS.

**Exit criteria**
- A clean machine (no Python installed except system) can install the VSIX and run the M6 acceptance test from scratch.
- All NFRs verified.
- Tag `v1.0.0`; publish.

**Requirements covered**
- Packaging: **FR-VSIX-02** complete (versioned binary download, SHA-256 verification), **FR-SRV-01** complete (PyInstaller per OS/arch).
- Acceptance run: REQUIREMENTS §8 acceptance test passes on each supported OS — the only release gate.
- All NFRs verified end-to-end: **NFR-01** (reliability/crash recovery on every OS), **NFR-02** (latency under load), **NFR-03** (portability across Win10+/macOS12+/Linux), **NFR-04** (no telemetry), **NFR-05** (rotating logs), **NFR-06** (loopback + secrets handling), **NFR-07** (conventions).

---

## 2. Critical path & dependencies

```
M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7
                ↑          ↑          ↑
                └──────────┴──── dogfood begins (use Kodo to author later agent prompts)
```

- M3 is the first milestone with end-user value (a working, if minimal, narrative + architecture flow). Risk concentrates here: WebView UX, gate semantics, and mirror correctness all land together.
- M4 is the largest by raw lines of code (security + MCP + toolchain). Plan for slip.
- M5 is the largest by concept density (5 new agents, 1 toolchain plugin, autonomous mode, STOP). Watch for compound bugs across cancellation paths.
- M6 is the unknown-unknown milestone: real E\*TRADE API quirks and LLM behaviour at full scale will surface issues. Budget contingency.

Total estimate: ~10–12 weeks of focused solo work. Pad by 30% for unknowns: ~13–16 weeks.

---

## 3. Dogfooding

From M3 onward, every agent's system prompt is itself a Kodo artifact: write a small "kodo-of-kodo" subproject under `dogfood/agents/` containing one component per agent. Use it to:

- Generate the system prompt files (`*.txt`) under `src/kodo/agents/`.
- Iterate on prompts via Kodo's own approval-gate UX.

This guarantees the workflow is exercised by its author daily and surfaces UX issues that would otherwise be invisible.

---

## 4. Testing strategy

- **Unit**: cover plugin interfaces, rule evaluation, manifest parsing, envelope serialisation. Behaviour-only — match generated-code conventions (FR-TST).
- **Integration**: each milestone exit criteria is essentially an integration test. Prefer running them under `pytest` with a `--live` flag for the ones requiring real network.
- **Acceptance**: the exit ticket from REQUIREMENTS §8.

No mocks of internal modules. HTTP mocked at the `aiohttp` boundary using `aiohttp`-test-utils. Anthropic API: a recorded-fixtures mode for fast tests, plus a `--live` mode running real calls (used in CI nightly, not on every push).

---

## 5. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Author/Reviewer loops degenerate (reviewer always picks at the same nit) | High | High | Strict iteration cap (5) + behaviour-test guard-rails baked into prompts; Dev override always available |
| Token cost balloons during dogfooding | High | Medium | Aggressive cache_control breakpoints; per-session cost shown in UI from M2; budget alarms (out-of-MVP, but logged) |
| WebSocket buffering blows up memory on long disconnect | Low | Medium | Bounded outbox (50 MB) + log overflow; reconnect is the common case |
| E\*TRADE sandbox API behaves differently from real API | Medium | Medium | Document the gap in generated bot README; e2e is sandbox-only by FR-TST-04 |
| PyInstaller binary size/AV false-positives on Windows | Medium | Low | Sign binary; exclude unused stdlib modules; document AV exception |
| Prompts authored against Claude 4.x degrade with future model | Medium | Low | Keep prompts in plain `.txt`, version with model name; M7 includes a model-pin strategy |
| Single-worker bottleneck makes E\*TRADE bot too slow to feel | Low | Medium | Single-worker is FR-WF-02; if pain shows, fast-follow milestone post-MVP for parallel components |

---

## 6. Out-of-MVP backlog (post-1.0)

Tracked here so they don't leak into MVP scope.

- Adopting existing codebases (generate `.kd` from source).
- Front-end / UI generation.
- Additional LLM providers.
- Selectable workflows (lighter agent sets for small tasks).
- Multi-worker concurrency for parallel component implementation.
- Cost caps + budget alerts.
- Telemetry (opt-in).
- Out-of-tree plugin install UX.
- Hosted / cloud variant.
- Backpressure protocol.
- `.kd`-specific tags / extended Markdown.
- Auto-commit to main repo.
- Editor integrations beyond VS Code.

---

## 7. Definition of done — the one that ships

REQUIREMENTS §8 acceptance test passes on a clean machine for each supported OS. No other gate.
