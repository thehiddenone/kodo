# Kōdo Server — Internal Design & Module Reference

> Generated from a full read of `src/kodo` (≈11.8k LOC, ~100 modules).
> This document maps **every link between Python modules and classes** —
> subclassing, composition, import, use, and dependency injection — and records
> the **implementation state** of each package. It is written from the code, not
> from prior design docs; where the code and the older [DESIGN.md](DESIGN.md)
> disagree, the code wins and the discrepancy is flagged.

---

## 1. What the system is

Kōdo is an agentic harness that turns a natural-language product request into
working code through a pipeline of LLM sub-agents arbitrated by a single
**Orchestrator** LLM. The Python package `kodo` is the **server**: an asyncio
aiohttp process that speaks a WebSocket wire protocol to a VS Code extension
(`kodo-vsix`, a separate repo). One server instance runs per project.

The server is deliberately a **thin substrate**. There is no hard-coded stage
machine or workflow DAG in Python. Every "what runs next" decision belongs to
the Orchestrator LLM, expressed through a small tool surface. The Python side
provides: an LLM streaming abstraction, a virtual artifact workspace, a git
mirror for checkpoints/rollback, a toolchain abstraction, session persistence,
and the wire transport.

---

## 2. Dependency layering

### 2.1 Import matrix

Every package and the `kodo` packages it imports (real `from kodo.x` / `import
kodo.x` statements only — docstring mentions excluded). Derived directly from the
source:

| Package | Imports from `kodo` |
|---|---|
| `common` | *(nothing)* |
| `project` | *(nothing)* |
| `toolchains` | *(nothing)* |
| `state` | *(nothing)* |
| `security` | *(nothing — stub)* |
| `transport` | `common` |
| `workspace` | `project`, `toolchains` |
| `toolspecs` | `workspace` |
| `llms` | `common`, `transport`, `toolspecs` |
| `subagents` | `toolspecs` |
| `runtime` | `common`, `transport`, `toolspecs`, `workspace`, `toolchains`, `project`, `state`, `subagents`, `llms` |
| `server` | `common`, `transport`, `project`, `state`, `workspace`, `subagents`, `llms`, `runtime` |

One edge breaks a clean strict hierarchy:

- **`toolspecs → workspace`.** `toolspecs/_list_artifacts.py` imports
  `ArtifactType` from `workspace._models`, so the otherwise "pure data" catalog
  reaches down into `workspace` for that one enum.

> **Note — two packages were merged to flatten the graph:**
>
> - The git **mirror** subsystem (checkpoints, promotion, the mirror git repo)
>   was a top-level `mirror` package; it now lives inside `workspace`
>   (`_repo.py`, `_promoter.py`, `_checkpoints.py`), as the on-disk counterpart
>   of the in-memory `ProjectIndex`. Importers reference it via `kodo.workspace`.
> - The local-inference utilities (installer, downloader, llama-server manager)
>   were a top-level `llm_utils` package that formed an **import cycle** with
>   `llms`. They now live inside `llms/llamacpp/` (`_installer.py`,
>   `_downloader.py`, `_llama_server.py`, `_manager.py`), re-exported from
>   `kodo.llms.llamacpp`. Since they are only used by llama.cpp inference, the
>   former `llms ⇄ llm_utils` cycle is gone.

### 2.2 Layered diagram

Lowest tier = packages that import nothing from `kodo`; each tier above imports
only from tiers below it. Lines are imports (`▼` points from importer to
imported); the annotation on each line names the packages pulled in.

```text
 T5  ┌──────────┐
     │  server  │  ▼ runtime · llms · workspace · subagents ·
     └────┬─────┘    state · project · transport · common
          │
          ▼
 T4  ┌──────────┐
     │ runtime  │  ▼ llms · subagents · toolspecs · workspace ·
     └────┬─────┘    toolchains · state · project · transport · common
          │
   ┌──────┴──────────┐
   ▼                 ▼
 ┌───────────┐   ┌────────┐
 │ subagents │   │  llms  │                              T3   (llms ⊇ llamacpp utils)
 └─────┬─────┘   └───┬────┘
       │ toolspecs   │ toolspecs · transport · common
       ▼             ▼
 ┌───────────┐
 │ toolspecs │                                          T2
 └─────┬─────┘
       │ workspace
       ▼
 ┌───────────┐      ┌───────────┐
 │ transport │      │ workspace │                       T1   (workspace ⊇ mirror)
 └─────┬─────┘      └─────┬─────┘
       │ common           │ project · toolchains
       ▼                  ▼
 ┌────────┬─────────┬────────────┬───────┬──────────┐
 │ common │ project │ toolchains │ state │ security │   T0  ← import nothing from kodo
 └────────┴─────────┴────────────┴───────┴──────────┘
```

(`runtime` and `server` also reach past the tier directly below them — e.g.
`runtime → toolspecs`/`workspace`/`common` — as the matrix in §2.1 lists in full;
only the principal lines are drawn above to keep the figure readable.)

- **T0 — leaf packages** (`common`, `project`, `toolchains`, `state`,
  `security`): import nothing from `kodo`. `security` and `state/_memory.py` are
  **stubs** (see §13).
- **T1**: `transport` (wire framing over `common`), `workspace` (artifact store
  **plus the merged git mirror** — checkpoints/promotion — over `project` +
  `toolchains`).
- **T2**: `toolspecs` (tool catalog, dips into `workspace` for `ArtifactType`).
- **T3**: `subagents` (prompt renderer over `toolspecs`), `llms` (LLM streaming;
  its `llamacpp` subpackage also holds the local-inference lifecycle utilities
  merged from the former `llm_utils`).
- **T4 — `runtime`**: the engine and tool dispatch; composes nearly every domain
  service.
- **T5 — `server`**: the composition root; builds the object graph and registers
  handlers.

---

## 3. `common/` — wire envelope & protocols

| Module | Defines | Notes |
|---|---|---|
| [_envelope.py](../src/kodo/common/_envelope.py) | `Envelope` (frozen dataclass), `MessageKind` (Literal) | The atomic WS frame `{kind, id, correlation_id?, payload}`. Factory classmethods: `make_response`, `make_event`, `make_stream_chunk`, `make_thinking_chunk`, `make_stream_end`; plus `to_json`/`from_json`. |
| [_protocols.py](../src/kodo/common/_protocols.py) | `ApiKey` (frozen dataclass), `MessageSink` (Protocol), `ApiKeyProvider` (Protocol) | `MessageSink.send(env)` and `ApiKeyProvider.get_key(vendor)` are the two seams that decouple the engine from the transport and the key broker. |

**Links:** `_protocols.py` imports `Envelope` from `_envelope.py`. Nothing in
`common` imports anything else in `kodo`. `MessageSink`/`ApiKeyProvider` are
**structural** protocols — implementations (`WebSocketDispatcher`, `KeyBroker`)
never subclass them; they just match the shape.

**State:** Complete.

---

## 4. `transport/` — WebSocket framing & dispatch

| Module | Defines | Links |
|---|---|---|
| [_messages.py](../src/kodo/transport/_messages.py) | `MSG_*` / `SREQ_*` / `EVT_*` string constants | Pure constants. A deprecated/legacy block is retained. |
| [_outbox.py](../src/kodo/transport/_outbox.py) | `Outbox` | Composes nothing; holds a `list[str]` buffer + `asyncio.Lock`. Buffers frames while disconnected (50 MB cap), `drain_to(ws)` on reconnect. Imports `Envelope`, `aiohttp.web`. |
| [_ws.py](../src/kodo/transport/_ws.py) | `WebSocketDispatcher`, `HandlerFn`, `APP_STATE_KEY`, `get_state()` | **Composes** one `Outbox`. Two dispatch paths: client `kind=request` → registered `HandlerFn` by `payload.type`; client `kind=response` → resolves an `asyncio.Future` by `correlation_id`. `register_response_future()` is the mechanism behind every server-initiated prompt. On disconnect it cancels all pending futures. |

**Key role:** `WebSocketDispatcher` **is** the `MessageSink` the engine sends
through (its `send()` delegates to `Outbox.send_or_buffer`). It is also the
backend that `GateOrchestrator` and `KeyBroker` register futures against.

**State:** Complete and used. (Legacy `MSG_*`/`EVT_*` constants are dead.)

---

## 5. `project/` — layout & manifest

| Module | Defines | Links |
|---|---|---|
| [_layout.py](../src/kodo/project/_layout.py) | `ProjectLayout` (frozen dataclass), `ProjectLayoutError`, `kodo_user_dir()` | Pure path algebra over a `root`: `kodo_md`, `src_dir`, `gen_dir`, `kodo_dir`, `workspace_dir`, `checkpoints_dir`, `sessions_dir`, `llm_requests_dir`, etc. `validate()` and `init()`. |
| [_manifest.py](../src/kodo/project/_manifest.py) | `Manifest` (frozen), `ManifestError`, `parse_manifest()` | Parses `kodo.md` headings + toolchain list. |

**Links:** `ProjectLayout` is **used by value** (constructed ad hoc) throughout:
`Workspace`, `Config`, `Lifecycle`, `CheckpointManager`, `Rollback`,
`WorkflowEngine`. `_manifest.py` is currently **not consumed** by the runtime —
toolchain selection happens from the Tech Stack artifact instead (see
`toolchains/_select.py`), so `parse_manifest` is effectively orphaned at
runtime.

**State:** Complete; `parse_manifest` under-used.

---

## 6. `toolspecs/` — the tool catalog (pure data)

One module per tool, each exporting a single frozen `ToolSpec` constant. No
dispatch logic lives here (that is in `runtime/`).

[_spec.py](../src/kodo/toolspecs/_spec.py) defines the `ToolSpec` dataclass:

```python
name, external_name, user_description, description,
input_schema, when_to_use: tuple[str, ...], autonomous_mode: str | None = None
```

`when_to_use` and `autonomous_mode` are rendered into each agent prompt's
`## Tools` section by `AgentRegistry` (§11). `autonomous_mode` containing
`"unavailable"` drives per-mode tool filtering.

[\_\_init\_\_.py](../src/kodo/toolspecs/__init__.py) aggregates two catalogs:

- **`LEAF_TOOLS_BY_NAME: dict[str, ToolSpec]`** — the 12 tools a leaf sub-agent
  may be granted: `publish_artifact`, `read_artifact`, `escalate_blocker`,
  `ask_user`, `request_user_review_artifact`, `report_artifact_completed`,
  `create_file`, `edit_file`, `delete_file`, `copy_file`, `move_file`,
  `run_command`. Consumed by `runtime/_subagent_dispatch.tools_for_agent`.
- **`ALL_TOOLS: tuple[ToolSpec, ...]`** — all 24 specs including both `ask_user`
  variants. Consumed by `subagents/_registry` to render prompts.

The two `ask_user` specs share `name="ask_user"` but differ:
[_ask_user.py](../src/kodo/toolspecs/_ask_user.py) (`ASK_USER`, leaf) vs
[_ask_user_orchestrator.py](../src/kodo/toolspecs/_ask_user_orchestrator.py)
(`ORCHESTRATOR_ASK_USER`). Both carry `autonomous_mode="unavailable …"`, as does
`REQUEST_USER_REVIEW_ARTIFACT` (`"auto-accepted …"`).

**Implementation state of the specs** (spec exists ≠ dispatch exists):

| Spec | Dispatch site | State |
|---|---|---|
| `publish_artifact`, `read_artifact` | `SubagentDispatcher` | ✅ implemented |
| `escalate_blocker`, `ask_user`, `request_user_review_artifact`, `report_artifact_completed` | `SubagentDispatcher` | ✅ implemented |
| `create_file`/`edit_file`/`delete_file`/`copy_file`/`move_file`/`run_command` | `SubagentDispatcher` | ✅ implemented, but **granted to no agent** (not in any frontmatter) |
| `query_frontier`, `list_artifacts`, `run_subagent`, `run_author_critic_iteration`, `rollback`, `finalize_project` | `ToolSurface` | ✅ implemented |
| `toolchain_build`/`toolchain_test`/`toolchain_deps` | — | ⚠️ **spec only, no dispatch.** Declared by `coder`/`test_coder` frontmatter; rendered into prompts but silently dropped by `tools_for_agent` (not in `LEAF_TOOLS_BY_NAME`). |
| `disable_autonomous_mode`, `post_update` | — | ⚠️ **spec only, no dispatch.** Declared by `orchestrator` frontmatter; rendered into its prompt but **not** in `ORCHESTRATOR_TOOLS`, so never passed to the LLM. |

**State:** Catalog complete; several specs are intentional placeholders ahead of dispatch.

---

## 7. `workspace/` — virtual artifact store + git mirror (single source of truth)

The `workspace` package holds two tightly-coupled halves: the **in-memory
artifact store** (the `ProjectIndex` + staging) and the **on-disk git mirror**
(checkpoints, promotion). The mirror was formerly a separate `kodo.mirror`
package; it is merged here because promotion writes the durable counterpart of
the in-memory index and rollback restores from it. All public names are exported
from [\_\_init\_\_.py](../src/kodo/workspace/__init__.py).

**Artifact store:**

| Module | Defines | Role |
|---|---|---|
| [_models.py](../src/kodo/workspace/_models.py) | `ArtifactType` (StrEnum), `Verdict` (StrEnum), `Concern`, `Artifact` | Data only. |
| [_index.py](../src/kodo/workspace/_index.py) | `ProjectIndex`, `IndexEntry` (frozen), `ArtifactState` (Literal) | **The in-memory catalog.** Metadata-only; content stays on disk at `IndexEntry.location`. Never persisted — reconstructed at bootstrap. Methods: `add`, `remove`, `mark_completed`, `get_by_id`, `get_by_key`, `all/completed/in_flight_entries`. |
| [_workspace.py](../src/kodo/workspace/_workspace.py) | `Workspace` | **Composes** a shared `ProjectIndex` (injected) + a `ProjectLayout` (built internally from `project_root`). Owns staging mechanics: writes per-artifact JSON under `.kodo/workspace/`, retires superseded files to `.retired/`, appends `events.jsonl`, validates publish rules. `read()` branches by state (in-flight = staging JSON, completed = raw promoted file). `bind_index()` swaps the index after bootstrap/rollback. `asyncio.Lock` serialises mutations. |
| [_component_registry.py](../src/kodo/workspace/_component_registry.py) | `ComponentRegistry` | Parses the architecture artifact's markdown table → codename→display-name map → `component_dir()` (snake_case). `.empty()` fallback. |
| [_materialization.py](../src/kodo/workspace/_materialization.py) | `materialization_path()`, `materialize()`, `dematerialize()` | Pure functions mapping `Artifact` + `ToolchainPlugin` + `ComponentRegistry` → a `src/`/`gen/` path. **Imports `toolchains._interface.ToolchainPlugin`** (the one upward-looking dependency, to a sibling domain). |
| [_errors.py](../src/kodo/workspace/_errors.py) | `WorkspaceError`, `WorkspaceValidationError`, `ArtifactNotFoundError` | Exception hierarchy. |

**Git mirror (merged from the former `kodo.mirror`):**

| Module | Defines | Role |
|---|---|---|
| [_repo.py](../src/kodo/workspace/_repo.py) | `MirrorRepo`, `MirrorRepoError`, `CheckpointInfo` (frozen) | Async git porcelain over `.kodo/checkpoints/` via `asyncio.create_subprocess_exec`: `init`, `stage_and_commit`, `head_sha`, `checkout`, `log`. No `kodo` imports. |
| [_promoter.py](../src/kodo/workspace/_promoter.py) | `Promoter`, `PromoterError` | **Composes** `MirrorRepo` + `ToolchainPlugin` + `ComponentRegistry`. `promote()` writes an accepted artifact to its `src/`/`gen/` path **and** the mirror tree + `.kodo.json` sidecar, then commits. Imports siblings `_materialization`, `_component_registry`, `_models`, `_repo`. |
| [_checkpoints.py](../src/kodo/workspace/_checkpoints.py) | `CheckpointManager` | **Composes** a `MirrorRepo` built from `ProjectLayout.checkpoints_dir`. `ensure_initialized`, `create_checkpoint`, `list_checkpoints`. |

**Links:** `Workspace` ← composition ← `ProjectIndex` (shared, injected by the
engine). `_materialization.py` and `_component_registry.py` are used by both
`Workspace` (indirectly) and `_promoter.py` — now intra-package sibling imports.
The **same `ProjectIndex` instance** is shared between `Workspace` and
`ToolSurface` (engine injects one into both). The engine takes a
`CheckpointManager` (param still named `mirror`) and constructs `Promoter`s
on demand; `Rollback` composes a `MirrorRepo`.

**State:** Complete. This is the most mature subsystem (high test coverage).

---

## 8. `toolchains/` — language plugin abstraction

| Module | Defines | Links |
|---|---|---|
| [_interface.py](../src/kodo/toolchains/_interface.py) | `ToolchainPlugin` (ABC), `ToolchainBuildResult`, `ToolchainTestResult`, `ToolchainTestCase`, `ToolchainTestScope` | ABC with `name`, `languages`, `init`, `add_dependency`, `build`, `test`, `format`, `source_filename`, `test_filename`. |
| [python/_plugin.py](../src/kodo/toolchains/python/_plugin.py) | `PythonPlugin(ToolchainPlugin)` | **Subclasses** the ABC. pytest + ruff + uv/pip via `asyncio.create_subprocess_shell`. |
| [python/_pytest.py](../src/kodo/toolchains/python/_pytest.py) | `parse_pytest_json`, `parse_pytest_stdout` | Output parsers → `ToolchainTestResult`. |
| [node/_plugin.py](../src/kodo/toolchains/node/_plugin.py) | `NodePlugin(ToolchainPlugin)` | **Subclasses** the ABC. vitest + npm. |
| [node/_vitest.py](../src/kodo/toolchains/node/_vitest.py) | `parse_vitest_stdout` | Output parser. |
| [_select.py](../src/kodo/toolchains/_select.py) | `select_toolchain(tech_stack_content, project_root)` | Maps the Tech Stack artifact's "primary programming language" line → `NodePlugin` or `PythonPlugin` (defaults Python). |

**Links:** Only `source_filename`/`test_filename`/`materialization_path` are
exercised at runtime (via `materialization.py` and `Promoter`). `build`/`test`/
`add_dependency`/`init`/`format` are **fully implemented but unreachable from the
agent loop today** because the `toolchain_*` tools have no dispatch (§6). The
engine consumes `select_toolchain` lazily in `__resolve_toolchain` and caches the
result, resetting it on rollback.

**State:** Plugins complete; not yet wired to agent tool calls.

---

## 9. `llms/` — LLM streaming abstraction

| Module | Defines | Links |
|---|---|---|
| [_interface.py](../src/kodo/llms/_interface.py) | `LLMPlugin` (ABC); `Message`, `Usage`, `StreamEvent` + subclasses `ThinkingDelta`/`TokenDelta`/`ToolCallEvent`/`TurnEnd`; re-exports `ToolSpec` | `Usage.usd_cost` lazily imports `anthropic._usage.compute_cost`. Stream contract: yields token/thinking deltas, then `ToolCallEvent`s, then one `TurnEnd`. |
| [_registry.py](../src/kodo/llms/_registry.py) | `LLMEntry` (frozen), `get_llm_registry()` | Static catalog of cloud (Claude Opus/Sonnet/Haiku) + local (llama.cpp Qwen/Gemma GGUF) models. Maps name → plugin module + model/repo IDs. |
| [_logger.py](../src/kodo/llms/_logger.py) | `LoggingLLMPlugin(LLMPlugin)` | **Decorator** wrapping any `LLMPlugin`; writes `NNNN_request.json`/`NNNN_response.json`. Process-wide counter. |
| [_tool_logger.py](../src/kodo/llms/_tool_logger.py) | `ToolCallLogger` | Writes per-tool invocation/result JSON; turn counter. Used by the engine, not a plugin. |
| [anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | `ClaudePlugin(LLMPlugin)`, `UnrecoverableError` | **Subclasses** ABC. Uses `anthropic.AsyncAnthropic`; composes `_cache` (breakpoints) + `_retry` (`with_retry_iter`). Cancellation via per-`stream_id` `asyncio.Event`. |
| [anthropic/_cache.py](../src/kodo/llms/anthropic/_cache.py) | `build_system_blocks`, `build_message_params` | Prompt-cache breakpoint construction. |
| [anthropic/_retry.py](../src/kodo/llms/anthropic/_retry.py) | `with_retry`, `with_retry_iter`, `UnrecoverableError`, `RetryExhaustedError` | Exponential backoff (2/8/32s); classifies auth/billing as unrecoverable. |
| [anthropic/_usage.py](../src/kodo/llms/anthropic/_usage.py) | `compute_cost` | Per-model USD pricing table. |
| [llamacpp/_llama.py](../src/kodo/llms/llamacpp/_llama.py) | `LlamaPlugin(LLMPlugin)`, `ThinkingStreamParser` | **Subclasses** ABC. OpenAI-compatible client against `llama-server`; converts Anthropic-style content blocks ↔ OpenAI chat messages; parses `<think>` tags into `ThinkingDelta`. **Composes** `MessageSink` (to emit `EVT_LLAMA_STATE`) and calls its sibling `_manager.ensure_llama_running`. |

**Links:** Every plugin is wrapped in `LoggingLLMPlugin` by the engine's
`__resolve_plugin`. `LlamaPlugin` reaches *up* into `transport` (for state
events) and *sideways* into its sibling local-inference utilities (§10).

**State:** Complete for both providers.

---

## 10. `llms/llamacpp/` — local inference lifecycle (merged from `llm_utils`)

These modules were a standalone top-level `llm_utils` package that formed an
import cycle with `llms`; they were moved under `llms/llamacpp/` (only llama.cpp
inference uses them) and are re-exported from `kodo.llms.llamacpp`. They are
imported by `LlamaPlugin` (siblings) and by `server/_app.py` (install/start/stop
handlers) — via `kodo.llms.llamacpp`, never from the private modules.

| Module | Defines | Links |
|---|---|---|
| [_installer.py](../src/kodo/llms/llamacpp/_installer.py) | `LlamaInstall`, `install/uninstall/update_llamacpp`, `check_llamacpp_update`, `find_installed`, `server_executable` | Platform-aware llama.cpp binary install into `~/.kodo/llama.cpp/bN/`. No `kodo` imports. |
| [_downloader.py](../src/kodo/llms/llamacpp/_downloader.py) | `download_model`, `get_model_path` | `huggingface_hub` GGUF fetch + JSON index. Imports `LLMEntry` from `llms._registry` (intra-`llms`, no longer a cross-package cycle). |
| [_llama_server.py](../src/kodo/llms/llamacpp/_llama_server.py) | `LlamaServer`, `LlamaServerConfig`, `RunningServer`, `find_running_server` | PID-managed `llama-server` subprocess; class-level singleton via `get_active_llama_server()`; `adopt()` reclaims a survivor after restart. |
| [_manager.py](../src/kodo/llms/llamacpp/_manager.py) | `ensure_llama_running` | Composes installer + downloader + server: ensures the right model server is up. |

**Links:** Consumed by `llms/llamacpp/_llama.py` (runtime) and `server/_app.py`
(install/start/stop handlers). Self-contained otherwise.

**State:** Complete.

---

## 11. `subagents/` — agent files & prompt rendering

| Module | Defines | Links |
|---|---|---|
| [_loader.py](../src/kodo/subagents/_loader.py) | `SubAgent` (frozen: `name`, `tools: frozenset[str]`, `system_prompt`, `source_path`, `capability`), `AgentLoadError`, `load_agent()` | Parses `subagent_<name>.md` frontmatter + body. |
| [_registry.py](../src/kodo/subagents/_registry.py) | `AgentRegistry` | Loads all `subagent_*.md` + mandatory `preamble.md`. **Renders the `## Tools` section from `ToolSpec` data** (`ALL_TOOLS`), choosing leaf vs orchestrator `ask_user` by agent name, and filtering `autonomous_mode == "unavailable"` tools when `autonomous=True`. Prepends the preamble. |

**Links:** `_registry` imports `ALL_TOOLS`, `ASK_USER`, `ORCHESTRATOR_ASK_USER`
from `toolspecs`. `get(name, autonomous)` returns a `SubAgent` with `{PLACEHOLDER:TOOLS}`
replaced and preamble prepended. Consumed only by `WorkflowEngine`.

**The 14 agents + 1 preamble** (frontmatter `tools:` lists):

| Agent | Tools declared | Role |
|---|---|---|
| `orchestrator` | query_frontier, list_artifacts, run_subagent, run_author_critic_iteration, ask_user, rollback, **disable_autonomous_mode**, **post_update** | Arbiter (the only `ToolSurface` consumer). |
| `narrative_author` | publish, read, **ask_user**, request_review, report_completed | Solo, user-facing intake. |
| `architect`, `requirements_author`, `functional_designer`, `e2e_test_designer`, `test_designer` | publish, read, escalate_blocker | Authors (paired with a critic). |
| `architect_critic`, `requirements_critic`, `functional_design_critic`, `e2e_test_design_critic`, `code_critic` | publish, read, request_review, report_completed | Critics (own the review gate). |
| `coder` | publish, read, **toolchain_build/test/deps**, escalate_blocker | Implements code (toolchain tools not yet dispatchable). |
| `test_coder` | publish, read, escalate_blocker, request_review, report_completed | Writes tests. |

> ⚠️ **Frontmatter ↔ surface mismatch:** `orchestrator` declares
> `disable_autonomous_mode`/`post_update` (rendered into its prompt) but these
> are absent from `ORCHESTRATOR_TOOLS`; conversely `finalize_project` is in
> `ORCHESTRATOR_TOOLS` but **not** in the orchestrator frontmatter — so it is
> dispatchable yet never described to the model. `coder`/`test_coder` declare
> `toolchain_*` which `tools_for_agent` drops. These are the live gaps between
> "described to the LLM" and "executable."

**State:** Loader/registry complete; agent roster present; tool wiring partially complete.

---

## 12. `runtime/` — the engine and tool dispatch

This is the orchestration core. [\_\_init\_\_.py](../src/kodo/runtime/__init__.py)
re-exports the public surface.

### 12.1 `WorkflowEngine` ([_engine.py](../src/kodo/runtime/_engine.py))

The single-worker substrate. **Constructor-injected dependencies** (all from the
server composition root):

```
sink: MessageSink            gate: GateOrchestrator
key_provider: ApiKeyProvider get_settings: Callable[[], dict]
transient: TransientStore    layout: ProjectLayout
registry: AgentRegistry      mirror: CheckpointManager
```

It **internally constructs**: a shared `ProjectIndex`, a `Workspace` (wrapping
that index), a `SessionState`, and a `ToolSurface` (via `__make_tool_surface`,
re-made after bootstrap/rollback). It owns `__orch_messages` (the Orchestrator's
running `list[Message]`), cumulative USD, and a lazily-resolved `ToolchainPlugin`.

**Composition / call graph:**

- `start()` → `CheckpointManager.ensure_initialized()` → `ProjectBootstrap(...).run()`
  → binds returned `ProjectIndex` into `Workspace` + rebuilds `ToolSurface` →
  `TransientStore.attach_session` → spawns `__run_worker` task. If resumed, loads
  messages and may re-fire a pending prompt.
- `__resolve_plugin(capability)` → reads fresh settings → `get_llm_registry()` →
  builds `ClaudePlugin` (via `ApiKeyProvider.get_key`) or `LlamaPlugin`, wrapped
  in `LoggingLLMPlugin`.
- `__run_agent_turn(...)` — the **generic LLM tool loop**, shared by orchestrator
  and leaf agents: streams events → emits `EVT_LLM_TURN_START`, stream chunks,
  `EVT_AGENT_TOOL_CALL`, `EVT_USAGE_UPDATE` → logs via `ToolCallLogger` +
  `TransientStore.write_agent_record` → dispatches each tool via an injected
  `tool_dispatch` callback → loops until no tool calls (or `stop_after_tools`).
- `__run_orchestrator_with_input` → `tool_dispatch = ToolSurface.dispatch`,
  `tools = orchestrator_tools(autonomous)`.
- `__run_subagent` → builds a per-run `SubagentDispatcher`, `tools =
  tools_for_agent(agent)`, `tool_dispatch = dispatcher.dispatch`,
  `stop_after_tools = lambda: dispatcher.stop_requested`. Returns published IDs.
- `__run_author_critic_iteration` → calls `__run_subagent` twice (author then
  critic), reads the critic's feedback artifact from `Workspace`, emits
  `EVT_REVIEW_STARTED`/`EVT_REVIEW_VERDICT`. **This is the callback the
  Orchestrator's `run_author_critic_iteration` tool invokes.**
- `__complete_artifact` (injected into `SubagentDispatcher` as `complete_fn`) →
  reads artifact → `__resolve_toolchain` + `__component_registry` →
  `materialization_path` → `Promoter.promote` (mirror commit + sidecar) →
  `Workspace.mark_completed(location=...)`. This is **promotion-on-completion**.
- `__run_rollback` (injected into `ToolSurface` as `rollback_fn`) → `Rollback.execute`
  → rebinds index, resets toolchain, fresh orchestrator session.

**Three callbacks the engine passes into `ToolSurface`:** `__run_subagent`,
`__run_author_critic_iteration`, `__run_rollback`. **One callback into every
`SubagentDispatcher`:** `__complete_artifact`.

### 12.2 `ToolSurface` ([_tool_surface.py](../src/kodo/runtime/_tool_surface.py))

The **Orchestrator's** tool handlers. Holds `ProjectIndex`, `GateOrchestrator`,
`SessionState`, and the three engine callbacks (`RunSubagentFn`,
`RunAuthorCriticFn`, `RollbackFn`). `ORCHESTRATOR_TOOLS` = 7 specs;
`orchestrator_tools(autonomous)` drops `ask_user` (its `_AUTONOMOUS_DISABLED`).
`dispatch(name, input)` routes to `__query_frontier`, `__list_artifacts`,
`__run_subagent`, `__run_author_critic_iteration`, `__ask_user`, `__rollback`,
`__finalize_project`. `query_frontier` computes the next artifact type per
responsibility from a fixed per-responsibility order.

### 12.3 `SubagentDispatcher` ([_subagent_dispatch.py](../src/kodo/runtime/_subagent_dispatch.py))

The **leaf agents'** tool handlers — one instance per sub-agent run.
**Composition:** `Workspace`, `GateOrchestrator`, plus `agent_name`,
`session_id`, `autonomous`, and a `complete_fn` callback (defaults to
`workspace.mark_completed`; the engine injects `__complete_artifact`).
`dispatch()` routes `publish_artifact`/`read_artifact` to `Workspace`,
report tools to `GateOrchestrator`, file-IO/`run_command` to direct
(path-guarded) filesystem/subprocess calls. `stop_requested` is set by
`escalate_blocker` (not by completion). `tools_for_agent(agent)` resolves
declared names through `LEAF_TOOLS_BY_NAME`, skipping unknowns.

### 12.4 Supporting runtime modules

| Module | Defines | Role / links |
|---|---|---|
| [_bootstrap.py](../src/kodo/runtime/_bootstrap.py) | `ProjectBootstrap`, `BootstrapResult` | 4-phase cold start: scan mirror sidecars (`completed`), scan workspace JSON (`in_flight`), drop orphans/broken lineage, locate/create orchestrator session via `OrchestratorMarker`. Returns a populated `ProjectIndex`. Imports `state._transient._new_session_id`. |
| [_orchestrator.py](../src/kodo/runtime/_orchestrator.py) | `OrchestratorMarker` | Reads/writes `.kodo/orchestrator.session`. Used by bootstrap + rollback. |
| [_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator`, `ApprovalResponse`, `QuestionResponse` | **Composes** `WebSocketDispatcher` + `TransientStore`. `fire_approval`/`fire_question` send `kind=request`, register a future, persist the pending prompt (for restart re-surface), and await. `fire = fire_approval` alias. Used by both `ToolSurface` and `SubagentDispatcher`. |
| [_rollback.py](../src/kodo/runtime/_rollback.py) | `Rollback` | **Composes** `MirrorRepo` + `ProjectLayout`. 7-step restore; rebuilds via `ProjectBootstrap`. Imports `_session_log.SessionLog`, `_orchestrator.OrchestratorMarker`. |
| [_session.py](../src/kodo/runtime/_session.py) | `SessionState` | Mutable phase/agent/component/autonomous. `to_dict()` for `EVT_STATE`. Shared by engine + `ToolSurface`. |
| [_session_log.py](../src/kodo/runtime/_session_log.py) | `SessionLog` | Append-only JSONL per session. Used by `Rollback` (termination events). |

**State:** Engine, tool surfaces, bootstrap, gates, rollback are implemented and
exercised by the orchestrator/author-critic flow. Coverage is lower here than in
`workspace/` (many branches are restart/rollback paths).

---

## 13. `state/` & `security/`

| Module | State |
|---|---|
| [state/_transient.py](../src/kodo/state/_transient.py) `TransientStore` | ✅ Per-session dir under `.kodo/sessions/<id>/`: `meta.json`, `transient.json` (stage/prompt/autonomous/pending_prompt), `session.jsonl` (orchestrator messages), `agents/*.jsonl`. Injected into engine + gate. |
| [state/_memory.py](../src/kodo/state/_memory.py) | ⚠️ **Stub** (`__all__ = []`). |
| [security/](../src/kodo/security/) (`_layer`, `_rules`, `_store`, `_defaults`) | ⚠️ **Stubs.** No rule evaluation gates any tool call. `autonomous` filtering happens in the registry/tool surface, not here. The wire defines `SREQ_PROMPT_PERMISSION` but nothing emits it. |

---

## 14. `server/` — composition root

| Module | Role |
|---|---|
| [__main__.py](../src/kodo/server/__main__.py) | CLI → `Config.from_args` → `Lifecycle.check_and_write_pid` → `create_app` → aiohttp `TCPSite` on `127.0.0.1`. |
| [_config.py](../src/kodo/server/_config.py) | `Config` (frozen) — layered settings (project > user > defaults). `reload_settings()` is the `get_settings` callable injected into the engine (read fresh per dispatch). |
| [_lifecycle.py](../src/kodo/server/_lifecycle.py) | `Lifecycle` — PID file + signal handlers. |
| [_key_broker.py](../src/kodo/server/_key_broker.py) | `KeyBroker` — **implements `ApiKeyProvider`** (structural) over `WebSocketDispatcher`. |
| [_app.py](../src/kodo/server/_app.py) | `create_app()` — **the wiring**. |

**`create_app` builds the object graph** (this is the canonical DI map):

```
Outbox ─► WebSocketDispatcher (=MessageSink, =sink)
                 ├─► KeyBroker        (=key_provider)
                 └─► GateOrchestrator ◄── TransientStore
AgentRegistry(_AGENTS_DIR)   CheckpointManager(layout)
        │                              │
        └────────► WorkflowEngine ◄────┘
            sink, gate, key_provider, get_settings=config.reload_settings,
            transient, layout, registry, mirror
```

Then it registers `HandlerFn`s on the dispatcher (`hello`, `prompt.submit`,
`mode.set`, `stop`, `config.reload`, llama install/start/stop, model.install),
stores the engine on the app, and hooks `_start_background`/`_stop_background`
(which call `engine.start()`/`engine.stop()` and adopt any surviving
llama-server).

**State:** Complete.

---

## 15. End-to-end flows

**Prompt → work:** client `prompt.submit` → `_app` handler →
`engine.handle_prompt_submit` (enqueues) → worker → `__run_orchestrator_with_input`
→ `__run_agent_turn` streams the Orchestrator LLM → tool calls dispatch through
`ToolSurface` → `run_subagent`/`run_author_critic_iteration` call back into the
engine, which spawns leaf agents through `SubagentDispatcher` → artifacts land in
`Workspace`/`ProjectIndex`.

**Completion → promotion:** a critic/solo agent calls `report_artifact_completed`
→ `SubagentDispatcher.__report_completed` → engine `__complete_artifact` →
`Promoter.promote` writes the file into `src/`/`gen/` **and** the mirror tree +
`.kodo.json` sidecar, commits, then `Workspace.mark_completed` flips state and
deletes the staging file.

**User gate:** any `ask_user`/`request_user_review_artifact`/`escalate_blocker`
→ `GateOrchestrator.fire_*` sends a `kind=request`, registers a future, persists
`pending_prompt`, and awaits the client's `kind=response`. In `autonomous` mode
`ask_user` is withheld entirely and review auto-accepts.

**Restart:** `ProjectBootstrap` rebuilds the index from disk; `OrchestratorMarker`
+ `TransientStore` resume the session; an unanswered `pending_prompt` is
re-surfaced.

**Rollback:** Orchestrator `rollback` → `ToolSurface.__rollback` → engine
`__run_rollback` → `Rollback.execute` (mirror checkout, tree restore, fresh
bootstrap) → engine rebinds index + starts a fresh orchestrator session.

---

## 16. Implementation-state summary

| Subsystem | State |
|---|---|
| `common`, `transport`, `project`, `workspace` (incl. merged git mirror), `state/_transient` | ✅ Complete, well-tested |
| `llms` (Anthropic + llama.cpp, incl. merged local-inference utilities), `toolchains` plugins | ✅ Complete |
| `toolspecs` catalog, `subagents` loader/registry | ✅ Complete |
| `runtime` engine / tool surfaces / bootstrap / gates / rollback | ✅ Functional; lower branch coverage on restart/rollback |
| Toolchain agent tools (`toolchain_build/test/deps`) | ⚠️ Spec only — no dispatch, dropped by `tools_for_agent` |
| Orchestrator `disable_autonomous_mode` / `post_update` | ⚠️ Spec + prompt only — not in `ORCHESTRATOR_TOOLS` |
| Native file-IO / `run_command` tools | ⚠️ Implemented but granted to no agent |
| `finalize_project` | ⚠️ Dispatchable but not in orchestrator frontmatter |
| `security/*`, `state/_memory` | ⛔ Stubs |
| `project/_manifest` | ◽ Implemented but unused at runtime |

---

## 17. Cross-cutting observations

1. **Single shared `ProjectIndex`.** Constructed by the engine, replaced by
   bootstrap/rollback, and `bind_index`-ed into `Workspace`; `ToolSurface` holds
   the same reference. All reads (`query_frontier`, `list_artifacts`) and all
   writes flow through it. It is never persisted.
2. **Two tool-dispatch surfaces, one generic loop.** `__run_agent_turn` is
   agent-agnostic; the only difference between the Orchestrator and a leaf agent
   is the `tools` list and the `tool_dispatch` callback. Tools never bleed across
   agent types.
3. **Stateless LLM calls.** Tool specs are re-sent on every `stream_query`; the
   `messages` list (with `tool_use`/`tool_result` blocks) is the only memory.
4. **Structural protocols decouple the seams.** `MessageSink`
   (`WebSocketDispatcher`), `ApiKeyProvider` (`KeyBroker`) — no inheritance,
   just shape-matching, which keeps `runtime` independent of `transport`/`server`.
5. **The prompt ↔ surface gaps** in §6/§11 are the highest-signal place to look
   when an agent "can't call a tool it was told about."
