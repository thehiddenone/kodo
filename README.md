# Kōdo

**Kōdo** (コード) is an agentic harness that treats natural language as source code.

## Concept

Most AI coding tools hand the LLM a file and ask it to figure out what changed.
Kōdo takes a different approach: the `.kd` file format explicitly captures *what has changed*
alongside the current specification, so the LLM receives precise change context rather than
having to infer it by comparing versions.

Think of it as a **build system for natural language**. Just as `make` tracks which source files
changed and recompiles only what's necessary, `kodo build` reads your `.kd` files, determines
what work needs to be done, and drives one or more LLMs to produce the minimum necessary code
changes.

## A Kōdo project's structure

A Kōdo project treats `.kd` files as its primary source. Everything else is derived:

```text
src/    *.kd files — the source of truth; what humans write and review
gen/    generated source code — produced by Kōdo from the specs in src/
dist/   final artifact — executable, package, or deployable
```

The relationship mirrors a traditional compiled project: specs are to generated source as
source is to binary. Humans own `src/`; Kōdo owns the rest of the pipeline.

## Workflow

1. **Author** — edit `.kd` files (standard Markdown with extra context tracked under the hood)
2. **Build** — run `kodo build`; Kōdo batches LLM calls efficiently across all changed specs
3. **Review** — inspect generated changes in a git-mirrored staging repo before anything touches your main repo
4. **Approve** — promote reviewed code from the mirror into your project

The mirror repo acts as a checkpoint and diff surface. Nothing lands in your codebase without
explicit human approval.

## Key features

**Spec authoring agent** — a built-in subagent helps you write and refine `.kd` files.
Rather than starting from a blank page, you describe intent in plain language and the agent
structures it into well-formed specs, suggests how to decompose complex requirements, and
flags anything likely to produce ambiguous or conflicting output.

**Explicit change context** — `.kd` files carry information about what was added, removed, or
revised since the last build. The LLM sees intent, not just state.

**Batched, token-efficient builds** — Kōdo groups LLM calls to maximise prompt cache reuse and
minimise cost. A batched build over Claude can save ~50% on tokens compared to naive
per-file calls.

**Multiple LLM backends** — not tied to a single provider; configure the backend that fits your
cost and capability requirements.

**Git-mirrored staging** — generated code lives in a mirror repo, checkpointed via git. Diff,
revert, or cherry-pick freely before promoting to your main repo.

**Optional single-responsibility validation** — before building, Kōdo can run a pre-flight pass
to verify that each `.kd` file has a single, coherent purpose. This catches scope creep in specs
early and tends to produce cleaner, more predictable code generation. It is opt-in; you are free
to organise `.kd` files however suits your project.

**End-to-end pipeline** — Kōdo does not stop at code generation. It understands how to scaffold
a project from scratch and how to invoke the appropriate toolchain to compile, package, or
otherwise build the generated artefacts. The goal is a single command that takes specs to a
deployable output, with no manual steps in between.

**Visual Studio Code extension** — a dedicated VS Code extension surfaces the full Kōdo
workflow inside the editor: authoring `.kd` files with syntax support, triggering builds,
and inspecting sync state without leaving the IDE.

**MCP support** — LLMs have access to any [Model Context Protocol](https://modelcontextprotocol.io)
server, giving them access to external tools, data sources, and services during code generation.

**Plugin system** — toolchains, LLM backends, and deployment targets are all first-class
extension points. Adding support for a new language ecosystem or a custom deployment workflow
means writing a plugin, not forking the core. The plugin API is designed to be narrow and
stable so integrations remain low-maintenance as Kōdo evolves.

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

Early-stage / experimental. The `.kd` format, tag schema, and build protocol are under active design.
