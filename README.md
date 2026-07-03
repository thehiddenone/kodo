# Kōdo

**Kōdo** (コード) is a build system that converts natural language into working code through a multi-agent LLM workflow.

## Concept

Most AI coding tools live or die by the quality of your prompt — if you know exactly what you want and how to ask for it, they shine; if you don't, you get something that looks plausible but misses the mark. Kōdo is built on the belief that the bar shouldn't be that high.

Rather than expecting users to front-load all the right details, Kōdo asks. A structured multi-agent workflow interviews you, probes your goals, and surfaces the decisions you didn't know you needed to make — turning a rough idea into a rich, precise specification before a single line of code is written. Those specs are first-class artefacts, versioned and owned by you, living alongside your source code.

That's the core idea: the spec is the source of truth. Kōdo's job is to help you build a good one, then translate it into running code automatically. Want to change how something works? Update the spec — Kōdo handles the rest. No manual code edits, no prompt re-engineering. The conversation you had upfront keeps paying dividends every time requirements evolve.

A Narrative Author captures intent, an Architect decomposes the problem, Requirements and Functional Designers flesh out each component, and a Test Designer defines exactly what "done" looks like — all before implementation begins. The Coder's only job is to satisfy those tests. Every stage is reviewed before the next begins.

To keep that promise reliable, Kōdo currently focuses on backend software — logic, APIs, data pipelines — where correctness can be verified without simulating a user interface. It's a deliberate constraint that makes automated testing tractable and results consistently trustworthy.

Kōdo runs as a Visual Studio Code extension talking to a local Python server. Both ship together; nothing leaves your machine except the LLM API call.

## A Kōdo project's structure

A Kōdo project treats `.kd` files as its primary source. Generated code lives alongside.

```text
kodo.md   project manifest — declares this is a Kōdo project; selects the toolchain
specs/    *.kd files — narrative, responsibilities, per-component specs; the source of truth
src/      generated source code — all components and modules, including entry points
test/     generated unit tests, integration tests, and the end-to-end test
.kodo/    Kōdo working state — checkpoint mirror (git), settings, logs
```

The relationship mirrors a traditional compiled project: specs are to generated code as source is to binary. Humans own `specs/` and approve everything that lands in `src/`/`test/`. For the MVP, `.kd` is plain Markdown — extended-tag variants are post-MVP.

## Workflow

1. **Init** — `Kodo: Init Project` lays down `kodo.md`, `specs/`, `src/`, `test/`, and `.kodo/`.
2. **Prompt** — describe your idea in the WebView. The Narrative Author drafts a top-level description.
3. **Architecture** — the Architect carves the work into components and emits a dependency graph used later for integration-test scheduling.
4. **Per-component specs** — for each component, Author/Reviewer pairs iterate on Requirements, then Functional Design, then Test Plan, with an approval gate between every stage.
5. **Tests first** — the Test Coder produces failing tests from the test plan; nothing is implemented yet.
6. **Implementation** — the Coder iterates until every test passes; the Code Reviewer gates the diff.
7. **Final approval** — the end-to-end test passes, the workflow closes, you review the full mirror history.

At each gate you can **Agree** (proceed) or **provide feedback** (re-run only the responsible Author/Reviewer pair with your input). A global **STOP** is available at all times. **Autonomous mode** lets a small LLM agent (the Dev Proxy) answer for you, following natural-language rules you define, when you want unattended runs.

## Key features

**Multi-agent workflow** — eleven specialised agents (Narrative Author, Architect, Requirements Author/Reviewer, Functional Designer/Critic, Test Designer/Critic, Test Coder, Coder, Code Reviewer) collaborate on every project. Each Author has a Reviewer that gates output until quality is acceptable, capped at five iterations before escalating.

**TDD by construction** — Kōdo writes tests from requirements before any implementation exists. The Coder's loop terminates when tests pass; if a requirement is not testable, the Test Designer pushes back during specification, before code is written.

**Behaviour testing, not implementation testing** — generated tests assert observable outcomes (a price change produced an order, a record was written) rather than call counts or internal mocks. LLMs tend toward brittle, implementation-coupled tests; Kōdo's Test Design Critic enforces the opposite by default.

**Approval gates with feedback loops** — every stage ends at a gate. Agree to proceed, or provide feedback that re-runs only the responsible agent pair — never the entire workflow. No work lands in `src/`/`test/` until you approve.

**Mirror checkpoints** — every approval is a git commit inside `.kodo/checkpoints/`. Browse history, roll back to any prior checkpoint, diff between any two states. Your main repository is never modified by Kōdo without explicit promotion.

**Token-efficient builds** — the Anthropic LLM plugin uses prompt caching with cache-control breakpoints on each agent's system prompt and on the project-context block, so most agent-to-agent transitions read from the cache.

**End-to-end pipeline** — Kōdo scaffolds the project, generates code, invokes the toolchain, and runs the full test suite up to and including an end-to-end test. The goal is a single workflow from idea to deployable artefact. MVP toolchains: Python (`pytest`, `uv`) and Node (`vitest`, `npm`).

**Visual Studio Code extension** — a dedicated extension hosts a WebView for the Kōdo session: streamed agent output, file diffs (opened in VS Code's native diff editor), shell results, approval prompts, cumulative cost, autonomous toggle, and STOP — all without leaving the IDE.

**Security layer** — every tool call passes through a per-call allow-or-ask judgement driven by the Command Control posture (permissive / defensive / smart). Smart mode statically analyzes shell commands for targets outside the workspace and runs an LLM intent judge over high-impact calls; anything it cannot clear raises a permission prompt in the WebView. See [`doc/SECURITY.md`](doc/SECURITY.md).

**Plugin system** — three first-class plugin kinds: LLM plugins (the model provider), agent plugins (a specialised role in the workflow), and toolchain plugins (the language ecosystem). The plugin API is narrow and stable so integrations remain low-maintenance as Kōdo evolves.

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
doc/        — Design notes and format specification
scripts/    — Build tooling (pre/post build, version stamping)
src/        — Source code
test/       — Tests (unit tests, functional tests, etc)
```

## Status

Early-stage. The MVP design is captured in [`doc/REQUIREMENTS.md`](doc/REQUIREMENTS.md), [`doc/DESIGN.md`](doc/DESIGN.md), and [`doc/PLAN.md`](doc/PLAN.md). The single release gate is the ability to build an algorithmic E\*TRADE trading bot end-to-end with all generated tests passing — no other criterion ships v1.0. MVP scope: back-end-only, green-field-only, Anthropic-only.
