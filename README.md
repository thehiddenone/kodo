# Kōdo

**Kōdo** (コード) is a build system that converts natural language into working code through a multi-agent LLM workflow — designed from the ground up to run on your own hardware, model included.

## Concept

Most AI coding tools live or die by the quality of your prompt — if you know exactly what you want and how to ask for it, they shine; if you don't, you get something that looks plausible but misses the mark. Kōdo is built on the belief that the bar shouldn't be that high.

Rather than expecting users to front-load all the right details, Kōdo asks. A structured multi-agent workflow interviews you, probes your goals, and surfaces the decisions you didn't know you needed to make — turning a rough idea into a rich, precise specification before a single line of code is written. Those specs are first-class artefacts, versioned and owned by you, living alongside your source code.

That's the first core idea: the spec is the source of truth. Kōdo's job is to help you build a good one, then translate it into running code automatically. Want to change how something works? Update the spec — Kōdo handles the rest.

The second core idea: none of this should require a subscription, an API key, or sending your code to someone else's datacenter. Kōdo runs as a Visual Studio Code extension talking to a local Python server — and that server can drive an open-weight model on your own GPU just as readily as a hosted API. With a cloud model, nothing leaves your machine except the LLM API call. With a local model, **nothing leaves your machine at all.**

To keep its promises verifiable, Kōdo currently focuses on backend software — logic, APIs, data pipelines — where correctness can be checked by tests without simulating a user interface.

## Local LLMs are first-class

Kōdo treats a GGUF running under llama.cpp exactly like a hosted API — same agents, same tools, same approval gates — and does the unglamorous work needed to make that actually true:

- **A curated model catalogue.** Two dozen ready-to-install builds across Qwen 3.6 (27B dense and 35B-A3B MoE), Qwen3-Coder-Next 80B, GPT-OSS 120B and 20B, Qwen 3.5 9B, Gemma 4 26B, and Ornith 1.0 35B — each entry carrying its quant spec, on-disk size, and hand-written hardware guidance for both discrete-GPU PCs and Apple Silicon Macs.

- **Hardware-fit detection.** Kōdo reads your GPU VRAM and system RAM and checks every catalogue entry against them *before* you download — a red warning when a build won't run on your machine, a yellow one when it will slow down at large contexts. The sizing accounts for llama.cpp's ability to split a model between GPU and system RAM (per-layer offloading for dense models, expert offloading for MoE models), so a modest consumer GPU is judged by what it can actually run — not by VRAM alone.

- **A real download manager.** Pause and resume that survive restarts and crashes, split-shard GGUFs handled automatically, live progress in every open window, and background failures surfaced instead of silently swallowed. See [`doc/LOCAL_MODEL_MANAGER.md`](doc/LOCAL_MODEL_MANAGER.md).

- **Agentic reliability hardening.** A local model's tool calls are parsed out of its raw token stream, and that format can slip in ways a hosted API's never does. Kōdo launches `llama-server` with grammar-constrained tool parsing, salvages tool calls the model emitted as plain text (behind a user confirmation), and strips stray `<think>` tags from reasoning — the difference between a demo and a 200-tool-call session that survives. See [`doc/LOCAL_INFERENCE.md`](doc/LOCAL_INFERENCE.md).

- **Bring your own.** Any HuggingFace GGUF, any local model file, or the URL of a llama-server you already run elsewhere — plus a binary override to point Kōdo at your own `llama-server` build.

Prefer a hosted model? Anthropic's model family is supported as a first-class alternative, with per-agent effort tiers and prompt caching keeping multi-agent token costs down. Either way, the server, your files, your specs, checkpoints, and session history stay on your machine.

Anthropic is the first cloud provider, not the last: support for OpenAI, Google, Meta, Alibaba, DeepSeek, and Kimi is planned, along with OpenRouter as an aggregator. Further out is a hybrid mode that treats local and cloud as one pool — a small local model triages each step of the workflow, escalating to a frontier model only when the work genuinely benefits from one and keeping everything routine on your own GPU.

## Small models, dense contexts

Are local models good enough for serious, multi-step engineering? Kōdo's architecture is built to make the answer yes — the multi-agent workflow isn't just quality control, it's context engineering for models that run on hardware you already own.

A monolithic coding agent accumulates one enormous transcript and re-reads all of it on every turn. Long context is exactly where local models hurt most: quality degrades, and the KV cache eats the very memory the weights need. Kōdo never puts any model in that regime. Each sub-agent starts from a clean context containing only the distilled inputs for its one job — the spec section under review, the test plan to implement, an investigation summary — does that job, returns a structured, schema-validated result, and exits. Nothing accumulates. Every context stays small and dense, which is precisely where a 9–80B open-weight model does its best work. When a long-running session does grow, context compaction kicks in automatically, sized to the active model's real context window.

The second half of the answer is verification. Kōdo writes tests from requirements before any implementation exists, pairs every author agent with a critic, and defines "done" as "the tests pass." That replaces *right on the first try* — what you pay frontier-model prices for — with *verifiably right after iteration*. On your own GPU, iteration costs electricity, not tokens.

## Two ways to work

**The guided pipeline** takes a green-field idea to a tested system through staged specification and review — the full workflow below.

**The Problem Solver** is the everyday entrance: point it at any codebase (Kōdo-built or not) and ask for a change, a fix, or a written investigation. It orchestrates dedicated Investigator, Planner, and Developer sub-agents for substantial work, and just does small asks directly — no ceremony for a one-file change.

The two are complementary rather than parallel: guided mode is planned project work, the Problem Solver is for what needs handling right now. The roadmap brings them together — the Problem Solver is set to learn how to operate on guided projects, updating the spec together with the code so an urgent fix never leaves the two out of sync.

## The guided workflow

1. **Init** — `Kodo: Init Project` lays down `.kodo/` (with the `kodo.md` manifest), `specs/`, `src/`, and `test/`.
2. **Prompt** — describe your idea in the WebView. The Narrative Author drafts a top-level description, optionally after a preliminary investigation of your existing code and the web.
3. **Architecture** — the Architect carves the work into components and emits a dependency graph used later for integration-test scheduling.
4. **Per-component specs** — for each component, author/critic pairs iterate on Requirements, then Functional Design, then a Test Plan, with an approval gate between every stage.
5. **Tests first** — the Test Coder produces failing tests from the test plan; nothing is implemented yet.
6. **Implementation** — the Coder iterates until every test passes; the Code Critic gates the diff.
7. **End-to-end** — where the Architect deems it applicable, an end-to-end test plan and suite assemble the whole system behind declared seams and verify it as a black box.
8. **Final approval** — the workflow closes and you review the full checkpoint history.

At each gate you can **Agree** (proceed) or **provide feedback** (re-run only the responsible author/critic pair with your input). A global **STOP** is available at all times. **Autonomous mode** runs the workflow unattended — agents resolve uncertainty with documented assumptions instead of questions, while the security layer stays live.

## A Kōdo project's structure

A Kōdo project treats `.kd` files as its primary source. Generated code lives alongside.

```text
specs/    *.kd files — narrative, responsibilities, per-component specs; the source of truth
src/      generated source code — all components and modules, including entry points
test/     generated unit tests, integration tests, and the end-to-end test
.kodo/    Kōdo working state — kodo.md manifest, checkpoint mirror (git), settings, logs
```

The relationship mirrors a traditional compiled project: specs are to generated code as source is to binary. Humans own `specs/` and approve everything that lands in `src/` and `test/`. For the MVP, `.kd` is plain Markdown — extended-tag variants are post-MVP.

## Key features

**Multi-agent workflow** — two entry agents (the Kōdo guide and the Problem Solver) orchestrate a roster of twenty-plus specialised sub-agents: authors paired with critics that gate output until quality is acceptable (capped at five iterations before escalating), plus standalone investigators, planners, developers, and toolchain agents.

**TDD by construction** — tests are written from requirements before any implementation exists. The Coder's loop terminates when tests pass; if a requirement is not testable, the Test Designer pushes back during specification, before code is written.

**Behaviour testing, not implementation testing** — generated tests assert observable outcomes rather than call counts or internal mocks. LLMs tend toward brittle, implementation-coupled tests; Kōdo's Test Design Critic enforces the opposite by default.

**Approval gates with feedback loops** — every stage ends at a gate. Agree to proceed, or provide feedback that re-runs only the responsible agent pair — never the entire workflow. No work lands in `src/` or `test/` until you approve.

**Full control over changes via the git mirror** — every mutating step lands as a commit in a shadow git mirror inside `.kodo/checkpoints/`, without touching (or requiring) a repository of your own. Roll the whole workspace back to any point in history — and forward again, nothing is destructive — or undo and redo an individual change's files independently, and diff between any two states. Nothing an agent does is beyond your reach to inspect or reverse. See [`doc/CHECKPOINTS.md`](doc/CHECKPOINTS.md).

**Crash-safe sessions** — every session persists as it runs and resumes exactly where it stopped, even mid-turn, after a crash or a window reload. Multiple sessions run in parallel tabs, across VS Code windows, against one shared local server. See [`doc/SESSIONS.md`](doc/SESSIONS.md).

**Security layer** — every tool call passes through a per-call allow-or-ask judgement driven by the Tool Control posture (permissive / defensive / smart). Smart mode statically analyzes shell commands for targets outside the workspace and runs an LLM intent judge over high-impact calls; anything it cannot clear raises a permission prompt. See [`doc/SECURITY.md`](doc/SECURITY.md).

**Web-capable research** — an agent-driven web search that paces its own discovery/read/synthesis loop, with browser-backed and static page extraction. See [`doc/WEB_SEARCH.md`](doc/WEB_SEARCH.md).

**Quantified quality control** — Kōdo's agent prompts are not tuned by gut feel. An internal harness, `kodo.validator`, drives real sessions through the real server, protocol, tools, and gates — no VS Code, no human — and records a complete transcript for scoring, giving system prompt changes a measurable quality signal. This matters doubly for local models: open-weight families differ enough in behaviour that a prompt that suits one can fail another, so the harness is what makes per-model system prompt curation tractable. See [`doc/VALIDATOR.md`](doc/VALIDATOR.md).

**Visual Studio Code extension** — streamed agent output, file diffs in VS Code's native diff editor, approval and permission prompts, cumulative cost, checkpoint controls, and a Local Inference Settings panel for browsing, downloading, and managing local models — all without leaving the IDE.

**Narrow extension surfaces** — LLM providers are plugins (Anthropic and llama.cpp today); agents and language toolchains are markdown-defined sub-agents, so adding a role or an ecosystem is a prompt plus a schema, not an engine change. See [`doc/ADDING_A_SUBAGENT.md`](doc/ADDING_A_SUBAGENT.md).

## Building the project

The project uses [hatch](https://hatch.pypa.io) for environment and build management.
The version scheme is `major.minor.patchb{build}` (e.g. `0.1.0b12`), where the build
number is stored in the `build_number` file and auto-incremented on release builds.

### Development cycle

```text
code change  →  build  →  test  →  commit  →  hatch run build  →  deploy wheel
```

| Command | What it does |
| --- | --- |
| `hatch run fmt` | Auto-format source with ruff. |
| `hatch run lint` | Lint source with ruff. |
| `hatch run typecheck` | Type-check with mypy. |
| `hatch run test` | Run the test suite with pytest. |
| `hatch run check` | Run fmt, lint, typecheck, and tests — no build. |
| `hatch build` | Quick wheel + sdist using the current version in `pyproject.toml`. Does **not** increment `build_number` or run checks. Use during development to verify the build. |
| `hatch run check-version` | Sync `__version__` in `__init__.py` from `pyproject.toml`. |
| `hatch run build` | Full release pipeline: stamp version, fmt, lint, typecheck, test, build, post-increment `build_number`. |

The `build_number` file contains the build number for the work currently in progress.
Commit your changes *before* running `hatch run build` — this way the committed source matches
the build number recorded in the repository. `hatch run build` is intended to be the final step
once code changes are done, tests are green, and everything is committed. It produces a numbered
wheel, then advances `build_number` so the repository is already pointing at the next iteration.

## Directory layout

```text
doc/        — Design docs (INTERNALS.md is the map)
scripts/    — Build tooling (pre/post build, version stamping)
src/        — Source code
test/       — Tests (unit tests, functional tests, etc)
```

## Status

Early-stage. The design is captured across [`doc/`](doc/) — start with [`doc/INTERNALS.md`](doc/INTERNALS.md).

Release is gated on demonstrated capability, not a feature list. The `kodo.validator` harness ([`doc/VALIDATOR.md`](doc/VALIDATOR.md)) runs real Kōdo sessions end-to-end — real server, real protocol, real tools and gates, no VS Code, no human — with local models under test. v1.0 ships when a battery of medium-to-high-complexity scenarios builds end-to-end inside that harness with all generated tests passing: an HTTP/HTTPS server in C++ or Rust, a transactional in-memory database, a distributed-consensus NoSQL key/value store, and more.

Current scope: backend software, green-field guided mode, Anthropic cloud or local llama.cpp models.
