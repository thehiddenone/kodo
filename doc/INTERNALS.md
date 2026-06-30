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
**Guide** LLM. The Python package `kodo` is the **server**: an asyncio
aiohttp process that speaks a WebSocket wire protocol to a VS Code extension
(`kodo-vsix`, a separate repo). One server instance runs per project.

The server is deliberately a **thin substrate**. There is no hard-coded stage
machine or workflow DAG in Python. Every "what runs next" decision belongs to
the Guide LLM, expressed through a small tool surface. The Python side
provides: an LLM streaming abstraction, agents that read and write real
project files directly (no staging area), a per-document evolution log
tracking revision/review history, a git mirror for checkpoints/rollback,
session persistence, and the wire transport.

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
| `guided_state` | *(nothing)* |
| `state` | *(nothing)* |
| `security` | *(nothing — stub)* |
| `mirror` | *(nothing)* |
| `shellparser` | *(nothing)* |
| `binutils` | *(nothing)* |
| `transport` | `common` |
| `toolspecs` | *(nothing — pure data)* |
| `tools` | `guided_state`, `project`, `toolspecs` |
| `llms` | `common`, `transport`, `toolspecs` |
| `subagents` | `toolspecs` |
| `runtime` | `common`, `transport`, `toolspecs`, `tools`, `guided_state`, `project`, `state`, `subagents`, `llms`, `mirror`, `shellparser`, `binutils` |
| `server` | `common`, `transport`, `project`, `state`, `subagents`, `llms`, `runtime`, `binutils` |

`toolspecs` is now a true leaf: the old `toolspecs → workspace` edge (importing
`ArtifactType` for `list_artifacts`'s schema) is gone along with the artifact
system. `document_feedback`'s concern-item shape is defined inline in its own
toolspec rather than imported, to keep `toolspecs` dependency-free.

> **Note — `kodo.workspace` and `kodo.toolchains` were deleted outright** (not
> merged elsewhere). `workspace` was the artifact-staging + promotion system
> (`Workspace`/`ProjectIndex`/`Promoter`/`MirrorRepo`/`ComponentRegistry`);
> `toolchains` was the `ToolchainPlugin` ABC + `PythonPlugin`/`NodePlugin`
> subclasses, whose only two jobs — naming promoted files and (unreachably)
> implementing build/test in Python — are both gone: agents choose their own
> file paths (§7) and `toolchain_build` now executes agent-generated shell
> scripts instead (§8). The new `kodo.guided_state` (§7) is their much smaller
> replacement — a leaf package, imported only by `tools` and `runtime`.
>
> The local-inference utilities (installer, downloader, llama-server manager)
> remain merged into `llms/llamacpp/` (`_installer.py`, `_downloader.py`,
> `_llama_server.py`, `_manager.py`), re-exported from `kodo.llms.llamacpp` —
> unrelated to this change, noted here only because it was the other half of
> the historical "two packages were merged" note this section used to carry.

### 2.2 Layered diagram

Lowest tier = packages that import nothing from `kodo`; each tier above imports
only from tiers below it. Lines are imports (`▼` points from importer to
imported); the annotation on each line names the packages pulled in.

```text
 T5  ┌──────────┐
     │  server  │  ▼ runtime · llms · subagents ·
     └────┬─────┘    state · project · transport · common
          │
          ▼
 T4  ┌──────────┐
     │ runtime  │  ▼ tools · llms · subagents · toolspecs · guided_state ·
     └────┬─────┘    state · project · transport · common
          │
   ┌──────┴───────┬───────────────┐
   ▼              ▼               ▼
 ┌───────────┐   ┌────────┐   ┌───────────┐
 │ subagents │   │  llms  │   │   tools   │              T3   (llms ⊇ llamacpp utils;
 └─────┬─────┘   └───┬────┘   └─────┬─────┘                    tools imported only by runtime)
       │ toolspecs   │ toolspecs    │ toolspecs · guided_state · project
       │             │ transport    │
       │             │ common       │
       ▼             ▼              ▼
 ┌───────────┐
 │ toolspecs │                                          T2   (pure data — imports nothing from kodo)
 └───────────┘
 ┌───────────┐
 │ transport │                                           T1
 └─────┬─────┘
       │ common
       ▼
 ┌────────┬─────────┬──────────────┬───────┬──────────┬────────┬─────────────┬──────────┐
 │ common │ project │ guided_state │ state │ security │ mirror │ shellparser │ binutils │   T0  ← import nothing from kodo
 └────────┴─────────┴──────────────┴───────┴──────────┴────────┴─────────────┴──────────┘
```

`runtime` is the sole importer of `mirror` and `shellparser` (via `runtime/_checkpoints.py`,
§10b) — neither is reachable from `tools`, `subagents`, or `llms`.

(`runtime` and `server` also reach past the tier directly below them — e.g.
`runtime → toolspecs`/`guided_state`/`common` — as the matrix in §2.1 lists in full;
only the principal lines are drawn above to keep the figure readable.)

- **T0 — leaf packages** (`common`, `project`, `guided_state`, `state`,
  `security`, `mirror`, `shellparser`, `binutils`): import nothing from `kodo`.
  `security` and `state/_memory.py` are **stubs** (see §13); `mirror`/`shellparser`
  are the checkpoint primitives consumed by `runtime` (§10b); `binutils` is the
  third-party util manager (§10a); `guided_state` is the per-document evolution
  log (§7) that replaced `kodo.workspace`.
- **T1**: `transport` (wire framing over `common`).
- **T2**: `toolspecs` (tool catalog) — now a true leaf, importing nothing from
  `kodo` (the old `toolspecs → workspace` edge for `ArtifactType` is gone).
- **T3**: `subagents` (prompt renderer over `toolspecs`), `llms` (LLM streaming;
  its `llamacpp` subpackage also holds the local-inference lifecycle utilities
  merged from the former `llm_utils`), and `tools` (the **dispatch
  implementation** of every tool in the catalog — one `Tool` subclass per tool).
  `tools` has a hard import ceiling of T0/T1/T2 (`guided_state` + `project` +
  `toolspecs`); the collaborators it needs from higher tiers — the gate, the
  session, the sub-agent launcher — are inverted via structural Protocols and
  injected by `runtime`. It is imported only by `runtime`, never by `subagents`
  or `llms`.
- **T4 — `runtime`**: the engine; composes nearly every domain service and
  builds a per-run `tools.ToolDispatcher` for each agent (guide or leaf).
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
| [_layout.py](../src/kodo/project/_layout.py) | `ProjectLayout` (frozen dataclass), `ProjectLayoutError`, `kodo_user_dir()` | Pure path algebra over a `root`: `kodo_md`, `specs_dir`, `src_dir`, `test_dir`, `kodo_dir`, `checkpoints_dir`, `sessions_dir`, `llm_requests_dir`, etc. `validate()`, `init()`, and **`scaffold_kodo_dir()`**. No `workspace_dir` anymore — there is no staging area to point at. |
| [_manifest.py](../src/kodo/project/_manifest.py) | `Manifest` (frozen), `ManifestError`, `parse_manifest()` | Parses `kodo.md` headings + the `## Toolchain` name. Purely informational now — no engine-side toolchain selection consumes it (§8); a toolchain-setup sub-agent reads it via `read_file` when generating scripts. |

**`kodo_md` moved under `.kodo/`:** the manifest now lives at `<root>/.kodo/kodo.md`
(was `<root>/kodo.md`) — `init()`/`validate()` updated accordingly, as did the
extension's project-detection/create-flow. The shadow checkpoint mirror (§10b)
excludes `.kodo/` entirely, so the manifest is **intentionally never checkpointed**.

**`scaffold_kodo_dir()`** is the lightweight counterpart of `init()` used when
Kōdo first touches an arbitrary directory that isn't (yet) a full Kodo project —
e.g. a Problem Solver workspace folder getting its first checkpoint mirror
(`RootMirrorManager`, §10b/§12.4): it creates only `.kodo/` and a minimal `kodo.md`
marker, never `specs/`/`src/`/`test/`, and never overwrites an existing manifest.

**Links:** `ProjectLayout` is **used by value** (constructed ad hoc) throughout:
`Workspace`, `Config`, `Lifecycle`, `CheckpointManager`, `Rollback`,
`WorkflowEngine`, `RootMirrorManager`. `_manifest.py` is currently **not
consumed** by the runtime — toolchain selection happens from the Tech Stack
artifact instead (see `toolchains/_select.py`), so `parse_manifest` is
effectively orphaned at runtime.

**State:** Complete; `parse_manifest` under-used.

---

## 6. `toolspecs/` — the tool catalog (pure data)

One module per tool, each exporting a single frozen `ToolSpec` constant. No
dispatch logic lives here (that is in `tools/`, §6A).

[_spec.py](../src/kodo/toolspecs/_spec.py) defines the `ToolSpec` dataclass:

```python
name, external_name, user_description, description,
input_schema, when_to_use: tuple[str, ...], autonomous_mode: str | None = None
```

`when_to_use` and `autonomous_mode` are rendered into each agent prompt's
`## Tools` section by `AgentRegistry` (§11). `autonomous_mode` containing
`"unavailable"` drives per-mode tool filtering.

[\_\_init\_\_.py](../src/kodo/toolspecs/__init__.py) exposes one catalog:

- **`ALL_TOOLS: tuple[ToolSpec, ...]`** — all specs (tool names are unique),
  including the terminal `return_result` every sub-agent uses to return its
  typed result (§11).
  Consumed by `subagents/_registry` to render prompts. (Which of these specs are
  actually *dispatchable* is a `tools/` concern — see
  `tools.DISPATCHABLE_TOOLS_BY_NAME`, §6A.)

[_ask_user.py](../src/kodo/toolspecs/_ask_user.py) (`ASK_USER`) carries
`autonomous_mode="unavailable …"`. (`ask_user` was once split into a leaf spec
and a separate guide spec; they were collapsed into one — the runtime contract
was identical and the guide-only guidance already lives in the guide
prompt body.)

**Implementation state of the specs:** every spec in the catalog now has a
matching dispatch handler in `tools/` (§6A); there are no spec-only
placeholders.

| Spec | Role |
|---|---|
| `read_file` | Read a file whole, by one or more 1-based line ranges, or by regex `pattern` (ripgrep-backed, with `context_before`/`context_after`). The general-purpose read tool — granted to authors and critics alike. |
| `document_feedback` | A critic's review verdict on one file: `{path, accept, concerns?, summary?}` → appends a `feedback` entry to that file's `.jsonl` evolution log (`kodo.guided_state`, §7). Never decides what happens next — the engine alone drives accept/review from the recorded `accept` flag. |
| `guided_dev_status` | Scans `.kodo/guided_dev_state/` and reports every tracked document's status, derived from its log's last entry. The replacement for the old artifact-index `query_frontier`. Guided-mode only; the handler errors if called from any other workflow mode. |
| `escalate_blocker`, `ask_user` | ✅ implemented |
| `filesystem`/`edit_file`/`run_command` | ✅ implemented; granted to authoring sub-agents and the `problem_solver` agent. `filesystem` is **one tool** whose mandatory `operation` field selects among eight file/directory ops — `create_file`/`create_dir`/`delete_file`/`delete_dir`/`copy_file`/`copy_dir`/`move_file`/`move_dir` (dir ops are recursive: `copytree`/`rmtree`/`mkdir -p`; `copy_dir`/`move_dir` fail if the destination exists). `edit_file` stays separate: a **targeted string-match edit** (`old_string` → `new_string`; must match exactly and uniquely or it fails without writing), the **preferred** way to change a file's contents; pass the whole new content as `new_string` to regenerate a file end to end. These three are exactly `runtime/_engine.py:_MUTATING_TOOLS` — the engine checkpoints around every call to them in **both** workflow modes (§12.1) and each one's `output_schema` carries an **optional `checkpoint_sha`** field the engine fills in when a commit happened. `filesystem`/`edit_file` calls additionally earn a `new_revision` entry in a tracked document's `.jsonl` log (§7) when the checkpoint commit landed under `specs/`/`src/`/`test/`. |
| `get_root_paths`, `find_files`, `find_text_in_files` | ✅ implemented (workspace search). `get_root_paths` returns the mode-aware root list (bound project in Guided; every workspace folder in Problem Solver) from `ToolContext.root_paths`. `find_files`/`find_text_in_files` resolve `root` through the active resolver then shell out to the bundled `fd`/`rg` (§10a) via `ToolContext.util_paths`. Granted to `guide` + `problem_solver`. |
| `run_subagent`, `run_author_critic_iteration`, `rollback`, `finalize_project` | ✅ implemented. `run_author_critic_iteration` now operates on `{author_name, critic_name, path?, input_paths?, instructions, for_revision?}` — a real file path, not artifact IDs. `rollback` now delegates to the same shadow-git mirror Problem Solver uses (§7/§10b). |
| `disable_autonomous_mode` | ✅ implemented (`DisableAutonomousModeTool`, in `_TOOL_CLASSES`). Declared by `guide`; resolved by `tools_for_agent` and dispatched. (Progress reporting is no longer a tool — agents emit `<kodo_info>` callouts in their message text; see the performance preamble.) |
| `create_new_project` | ✅ implemented (`CreateNewProjectTool`). Granted to `guide` + `problem_solver`. Thin shim over `_EngineServices.create_project(name)`: the engine slugifies the name, makes a fresh directory under the session workspace root (auto-suffix `-2`/`-3`… on collision), scaffolds `.kodo/`+`kodo.md`+checkpoint mirror via `RootMirrorManager.prepare`, records it in the logical-root map, and pushes `EVT_WORKSPACE_ADD_FOLDER` so the extension adds it to the open workspace (WS_PROTOCOL §5.9c). |
| `toolchain_build`/`toolchain_deps` | ✅ implemented. `toolchain_build` executes the project's generated `scripts/<step>.{sh,ps1}` pair (the toolchain-setup agent's output, §8/§11) in canonical order — format → build → static_analysis → test — stopping at the first failure; a missing script returns a clear error directing the caller to run the toolchain-setup agent first. `toolchain_deps` performs **one** add/remove/update dependency op: it does not touch manifests itself but spawns the `toolchain_depsmgr` sub-agent (via the dedicated ungated `_EngineServices.run_dependency_manager`, **not** `run_subagent` — holding the tool is the authorization, so the sub-agent is never in any caller's allow-list/roster) which follows the project's `DEPENDENCIES.md`. When that sub-agent reports `status: "dependencies_md_missing"`, the tool returns the same status plus a remediation `message` telling the caller to run the toolchain-setup sub-agent (`toolchain_python`) first — error-forwarding via the matched tool/sub-agent schemas. |

**State:** Catalog complete; every dispatchable spec has a handler.

---

## 6A. `tools/` — unified tool dispatch (the handler layer)

A dedicated import tier **between** `toolspecs` (T2) and `subagents`/`llms`
(T3): it may import only T0/T1/T2 (in practice `guided_state` + `toolspecs`)
and is consumed only by `runtime`. It must never import `subagents`, `llms`, or
`runtime` — the collaborators those would supply are inverted via structural
Protocols and injected.

**There is no guide-vs-leaf split.** Every agent (guide included)
is granted exactly the tools its frontmatter declares, and every tool call is
routed through a single `ToolDispatcher` to the matching `Tool` subclass (bound
to the run's context).

| Module | Defines | Role |
|---|---|---|
| [_context.py](../src/kodo/tools/_context.py) | `ToolContext`, `RootPath`, `GateLike`, `SessionLike`, `EngineServices`, `QuestionLike`, `ApprovalLike` | The injected per-run context (collaborators + mutable `stop_requested`/`returned_output`) and the structural Protocols runtime satisfies. `EngineServices` is one protocol covering every engine-side operation a tool can trigger (sub-agent launch, **dependency-manager launch** (`run_dependency_manager`, the ungated `toolchain_depsmgr` spawn behind `toolchain_deps`), author/critic iteration, rollback, mode disable, project creation). `runtime.GateOrchestrator`/`SessionState` and the engine's `_EngineServices` adapter match them by shape. The mode a tool honours is read live from `SessionLike.effective_autonomous` (frozen per prompt), never snapshotted onto the context. Also carries `mode: str` (`"guided"`/`"problem_solving"`, frozen per prompt — gates `guided_dev_status` and tags `new_revision` jsonl entries) and `project_root: Path \| None` (the bound project's root, independent of mode, so a Problem-Solver edit to a tracked file is still recorded — see §7), plus `root_paths: tuple[RootPath, ...]` and `util_paths: dict[str, Path]` for the search tools. |
| [_tool.py](../src/kodo/tools/_tool.py) | `Tool` (ABC) | Binds one run's `ToolContext` (read-only `context` property) and declares the abstract `handle(self, tool_input) -> str`. |
| `_<tool_name>.py` (one module per dispatchable tool) | one `Tool` subclass each | e.g. `ReadFileTool`, `DocumentFeedbackTool`, `GetRootPathsTool`; implements `handle` reading `self.context`. Mirrors the `toolspecs` one-file-per-tool convention. |
| [_dispatch.py](../src/kodo/tools/_dispatch.py) | `ToolDispatcher`, `tools_for_agent`, `DISPATCHABLE_TOOLS_BY_NAME` | The `_TOOL_CLASSES` table pairs each dispatchable `ToolSpec` with its `Tool` subclass; `dispatch` instantiates the class bound to the run's context and calls `handle`; exposes per-run `stop_requested`/`returned_output`. `tools_for_agent(frozenset[str])` resolves an agent's declared names to specs (skipping spec-only placeholders — none today). |
| [_paths.py](../src/kodo/tools/_paths.py) | `resolve_within`, `ProjectPathResolver`, `LogicalPathResolver` | Project-root / logical-workspace path guards shared by the file-I/O, shell, and search handlers. |
| [_search.py](../src/kodo/tools/_search.py) | `run_util`, `UtilTimeout` | Shared subprocess launcher for `find_files`/`find_text_in_files`/`read_file`'s pattern mode and `toolchain_build`'s script execution: runs the util with stdin closed under a bounded timeout, killing the whole process tree on POSIX. Holds no tool dispatch. |

**Links:** `runtime/_engine.py` builds one `ToolDispatcher` per agent run via
`__make_dispatcher`, injecting `GateOrchestrator`, `SessionState`, and one
`_EngineServices` adapter (wrapping the engine's `__run_subagent` /
`__run_dependency_manager` / `__run_author_critic_iteration` / `__run_rollback` /
`__disable_autonomous`). The dispatcher takes **no**
`autonomous` flag — tools read `SessionState.effective_autonomous`, which the
worker freezes once per prompt, so a mid-prompt mode toggle never rebuilds the
dispatcher or splits the prompt's mode. Autonomous filtering of `ask_user`
happens once, in `subagents/_registry`. `__make_dispatcher` also passes `mode`
and `project_root` (read live from `current_project`, independent of mode),
`root_paths` (computed mode-aware from `current_project`/`SessionWorkspace.folders`
— the latter synced by the extension's `workspace.folders` frames) and
`util_paths` (resolved from `binutils.find_util(kodo_user_dir(), "fd"/"ripgrep")`).

**State:** Complete.

---

## 7. `guided_state/` — per-document evolution log (replaces the artifact workspace)

**Authors and critics work directly on real files** under `specs/`/`src/`/
`test/` via the native `filesystem`/`edit_file`/`read_file` tools — there is no
staging area, no in-memory index, and no toolchain-driven file naming.
`guided_state` tracks each document's revision/review history as a per-file,
append-only `.jsonl` log; **the current state of a file is always the last
line of its log**, read on demand — nothing is reconstructed at bootstrap.
This package replaced the former `kodo.workspace` (artifact staging +
promotion) and the naming half of `kodo.toolchains` (§8) outright; there is no
successor class hierarchy, just these pure functions. All public names are
exported from [\_\_init\_\_.py](../src/kodo/guided_state/__init__.py).

**Storage convention:** `<root>/specs/foo/bar.md` →
`<root>/.kodo/guided_dev_state/specs/foo/bar.md.jsonl` (`src/`, `test/`
analogously). A path outside those three roots is untracked — no log applies.
Because `.kodo/` is already excluded from the shadow-git mirror's tracked
tree (§10b), these logs are **never committed** by the same mirror that
commits the real document changes — exactly the "only the author's changes
are tracked by git" split the design requires.

| Module | Defines | Role |
|---|---|---|
| [_paths.py](../src/kodo/guided_state/_paths.py) | `shadow_path()`, `is_tracked()` | The real-path ↔ `.jsonl`-path mapping above. |
| [_records.py](../src/kodo/guided_state/_records.py) | `ConcernItem`, `Status`, `new_revision_entry()`, `feedback_entry()`, `review_result_entry()`, `accepted_entry()`, `derive_status()` | The four entry-type constructors (pure dict builders) and the status-derivation rule (last line's `type` → one of `pending_review`/`needs_revision`/`pending_acceptance`/`accepted`). |
| [_store.py](../src/kodo/guided_state/_store.py) | `append_new_revision()`, `append_feedback()`, `append_review_result()`, `append_accepted()`, `read_history()`, `read_status()`, `read_jsonl()` | Append/read the `.jsonl` log for one document. `append_new_revision` is a no-op outside the tracked roots; the other three raise `ValueError` for an untracked path (they should never be called for one). `append_accepted` reads the log's most recent `new_revision` to reuse its `commit_hash` — acceptance never produces a new commit. |
| [_scan.py](../src/kodo/guided_state/_scan.py) | `scan_tracked_files()` | Walks `.kodo/guided_dev_state/` and returns `{path, status, last_event}` per tracked document. Backs the `guided_dev_status` tool (§6). |

**The four jsonl entry types** (one append-only line each, see the records
module above for exact fields):

1. **`new_revision`** — engine-written, immediately after a `filesystem`/
   `edit_file` call's checkpoint commit lands under a tracked root (§12.1).
   Carries the commit `sha`, the agent name, the tool used, and a
   `workflow: "guided"|"problem_solving"` tag — **fired in both workflow
   modes**, so the Guide can reconcile state after a Problem-Solver session
   touched a tracked file. This is the *only* entry type Problem Solver ever
   produces.
2. **`feedback`** — written by the `document_feedback` tool (critics only):
   `accept` + `concerns` (the same shape `concern_item` always used).
3. **`review_result`** — engine-written only, never via a dispatched tool:
   the user's `approve`/`reject` decision from the interactive review gate.
4. **`accepted`** — engine-written only: the final marker, `commit_hash`
   copied from the preceding `new_revision`.

**State:** Complete; high test coverage (`test_guided_state.py`,
`test_engine_document_flow.py`).

---

## 8. Toolchain setup — generated build scripts (no plugin package)

There is **no `toolchains/` package anymore.** The former `ToolchainPlugin`
ABC + `PythonPlugin`/`NodePlugin` subclasses existed only to (a) decide
`source_filename`/`test_filename` for artifact promotion (§7, now gone — agents
choose their own paths) and (b) implement `build`/`test`/`add_dependency`
directly in Python, which the `toolchain_*` tools never actually dispatched.
Both reasons are gone.

The project's build model instead lives in **agent-generated scripts and docs**: a
language-specific toolchain-setup sub-agent (`toolchain_python`, sharing the
`base_toolchain.md` + `base_dependencies.md` contracts, §11) generates five
per-platform script pairs — `scripts/{build,format,static_analysis,test,full_build}.{sh,ps1}`
— plus two root docs: `DEVELOPMENT.md` (build/check/test how-to) and
`DEPENDENCIES.md` (the machine-followable **dependency contract** —
manager, kinds, and command-level add/remove/update steps). The
`toolchain_build` tool (§6, `tools/_toolchain_build.py`) is a thin,
language-agnostic executor: it runs the enabled steps' scripts in canonical
order (format → build → static_analysis → test), stopping at the first failure,
and returns a clear "ask the toolchain-setup agent" error when a script doesn't
exist yet. `toolchain_deps` is the dependency counterpart: it spawns the
`toolchain_depsmgr` sub-agent, which **executes `DEPENDENCIES.md`** for a single
add/remove/update op (and reports `dependencies_md_missing`, which the tool turns
into a "run the toolchain-setup agent first" remediation message).

**State:** Both tools have real dispatch (previously spec-only placeholders).
Dependency management remains deliberately unimplemented.

---

## 9. `llms/` — LLM streaming abstraction

| Module | Defines | Links |
|---|---|---|
| [_interface.py](../src/kodo/llms/_interface.py) | `LLMPlugin` (ABC); `Message`, `Usage`, `StreamEvent` + subclasses `ThinkingDelta`/`ThinkingSignature`/`TokenDelta`/`ToolCallEvent`/`TurnEnd`; re-exports `ToolSpec` | `Usage.usd_cost` lazily imports `anthropic._usage.compute_cost`. Stream contract: yields token/thinking deltas, an optional `ThinkingSignature` once a thinking block closes, then `ToolCallEvent`s, then one `TurnEnd`. See SESSIONS.md "Thinking blocks". |
| [_registry.py](../src/kodo/llms/_registry.py) | `LLMEntry` (frozen), `get_llm_registry()` | Static catalog of cloud (Claude Opus/Sonnet/Haiku) + local (llama.cpp Qwen/Gemma GGUF) models. Maps name → plugin module + model/repo IDs. |
| [_logger.py](../src/kodo/llms/_logger.py) | `LoggingLLMPlugin(LLMPlugin)` | **Decorator** wrapping any `LLMPlugin`; writes `NNNN_request.json`/`NNNN_response.json`. Process-wide counter. |
| [_tool_logger.py](../src/kodo/llms/_tool_logger.py) | `ToolCallLogger` | Writes per-tool invocation/result JSON; turn counter. Used by the engine, not a plugin. |
| [_sanitize.py](../src/kodo/llms/_sanitize.py) | `strip_kodo_callouts` | Regex-strips `<kodo_info>`/`<kodo_warn>`/`<kodo_crit>`/`<kodo>` callout tags (incl. their content) from assistant text. These tags are a one-way notification to the human user (§ performance preamble), so their content is never replayed back into the model's own context. Called only by the wire-format builders below and by `_engine.py`'s `__render_transcript` (compaction input) — never by anything that persists or renders history, so `session.jsonl`/the WebView still see the tags verbatim. |
| [anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | `ClaudePlugin(LLMPlugin)`, `UnrecoverableError` | **Subclasses** ABC. Uses `anthropic.AsyncAnthropic`; composes `_cache` (breakpoints) + `_retry` (`with_retry_iter`). Enables extended thinking on every call (`thinking={"type": "enabled", "budget_tokens": 4096}`); yields `ThinkingDelta` from the SDK's raw thinking delta and `ThinkingSignature` from its `signature_delta`. Cancellation via per-`stream_id` `asyncio.Event`. |
| [anthropic/_cache.py](../src/kodo/llms/anthropic/_cache.py) | `build_system_blocks`, `build_message_params`, `_drop_unsigned_thinking`, `_strip_callout_text` | Prompt-cache breakpoint construction. `_drop_unsigned_thinking` strips any persisted `"thinking"` block lacking a `signature` (e.g. one originated by llama.cpp in a mixed-provider session) before it reaches Claude, which rejects unsigned thinking blocks. `_strip_callout_text` runs `_sanitize.strip_kodo_callouts` over every assistant `"text"` block (and bare string content) before it is sent. |
| [anthropic/_retry.py](../src/kodo/llms/anthropic/_retry.py) | `with_retry`, `with_retry_iter`, `UnrecoverableError`, `RetryExhaustedError` | Exponential backoff (2/8/32s); classifies auth/billing as unrecoverable. |
| [anthropic/_usage.py](../src/kodo/llms/anthropic/_usage.py) | `compute_cost` | Per-model USD pricing table. |
| [llamacpp/_llama.py](../src/kodo/llms/llamacpp/_llama.py) | `LlamaPlugin(LLMPlugin)`, `ThinkingStreamParser` | **Subclasses** ABC. OpenAI-compatible client against `llama-server`; converts Anthropic-style content blocks ↔ OpenAI chat messages; parses `<think>` tags into `ThinkingDelta`. `_expand_assistant` re-wraps any persisted `"thinking"` content block (this provider's or a Claude-origin signed one) back into `<think>...</think>` text, dropping any signature, since llama.cpp has no use for it; it also runs assistant `"text"` blocks (and `_expand_message`'s bare string case) through `strip_kodo_callouts`. **Composes** `MessageSink` (to emit `EVT_LLAMA_STATE`) and calls its sibling `_manager.ensure_llama_running`. |

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

## 10a. `binutils/` — portable third-party util manager

Kōdo bundles three external CLI utils — **uv**, **ripgrep**, **fd** — under
`~/.kodo/bin/`. Each util gets its own directory with the binary directly inside
it, plus a sibling JSON manifest:

```
~/.kodo/bin/
    uv.json        uv/uv          (uv\uv.exe on Windows)
    ripgrep.json   ripgrep/rg
    fd.json        fd/fd
```

They are called **utils** (not "tools") to avoid colliding with the agent-facing
tool catalog (`kodo.toolspecs.ToolSpec` etc.), which is an unrelated concept.

Manifest schema (shared verbatim with the VS Code extension's
[`src/uv-setup.ts`](../../kodo-vsix/src/uv-setup.ts)): `{name, version, path,
download_url}`. Versions are **pinned** (`uv=0.11.24`, `ripgrep=15.1.0`,
`fd=10.4.2`) in `UTIL_SPECS`; bumping one is a code change here (and in the
extension, for uv).

Both the extension and this module check the manifest + binary and only download
the pinned release when missing, so whichever runs first wins and the other is a
no-op. The **extension installs only uv** (it needs uv to build the venv before
any Python runs); **this module installs all three**, so a future console-only
build works without the extension. The dual install path is intentional.

| Module | Defines | Links |
|---|---|---|
| [_utils.py](../src/kodo/binutils/_utils.py) | `UtilSpec`, `UtilInstall`, `UTIL_SPECS`, `ensure_util`, `ensure_all_utils`, `find_util` | Platform-keyed (`<os>-<arch>`) pinned download/extract into `~/.kodo/bin/<name>/`. Per-util target maps encode rg's musl(x64)/gnu(arm64) Linux split; all three now ship a native `aarch64-pc-windows-msvc` build. No `kodo` imports (takes `kodo_dir: Path`). |

The Python package is named `binutils` (the on-disk install dir stays `~/.kodo/bin/`)
to keep it distinct from the agent-facing tool catalog.

**Wiring:** `server/_app.py:_start_background` calls `ensure_all_utils(kodo_user_dir())`
via `asyncio.to_thread` once at startup — best-effort (per-util failures logged,
never fatal), off the event loop so a first-run download does not block readiness.
`ripgrep`/`fd` are now **invoked** by the `find_text_in_files`/`find_files` agent
tools: the engine resolves their binary paths via `find_util(...)` and injects them
into the per-run `ToolContext.util_paths` (see §12, search tools).

**State:** Complete.

---

## 10b. `mirror/` & `shellparser/` — generic checkpoint primitives

> **Formerly two unrelated "mirror" mechanisms shared the word; now there is
> one.** Guided mode used to run its own artifact-promotion mirror
> (`workspace._repo.MirrorRepo` / `_promoter.Promoter` /
> `_checkpoints.CheckpointManager`), separate from the generic, lower-level
> mechanism documented here, which commits the **real project tree** after
> every file-mutating tool call. That bespoke Guided mirror is **deleted** —
> Guided mode now drives this same generic mechanism, unconditionally, in
> both workflow modes (§12.1). There is exactly one shadow-git mirror per
> root, regardless of which workflow touched it.

Both packages are T0 leaves (import nothing from `kodo`) and have no opinion
about *when* to checkpoint — that judgment lives entirely in `runtime`.

| Module | Defines | Role |
|---|---|---|
| [mirror/_mirror.py](../src/kodo/mirror/_mirror.py) | `ShadowMirror`, `CommitInfo` (frozen) | Drives `git` over an **explicit `(work_tree, git_dir)` pair** via `GIT_DIR`/`GIT_WORK_TREE` env vars instead of a `.git` inside the tracked tree — so the tracked files are the real project files (no copy/duplication) while git's metadata lives elsewhere (`<root>/.kodo/checkpoints/.git`). `init(excludes)` seeds `info/exclude` then commits the **current tree as a baseline** (so undoing the very first change restores genuine pre-Kōdo state, not an empty tree). `commit(label) → sha` stages everything and commits; a clean tree short-circuits to the existing `HEAD` (no empty commits). `paths_changed(sha)` lists the work-tree-relative paths a commit touched (`git diff-tree --name-only`). `undo(sha)` restores **only** the paths `sha` touched to their pre-`sha` state (`git checkout sha^ -- <paths>`) — later edits to *other* files are untouched, but later edits to the *same* files are discarded. `rollback(sha)` restores the **entire** tree to `sha`'s state and deletes files created after it. Both `undo` and `rollback` record their effect as a **new commit** — the mirror is append-only, so re-applying an undone change ("redo") is always just rolling forward to a later commit; nothing is ever reset or force-pushed. `log()`/`head_sha()` round out the read side. |
| [shellparser/_parser.py](../src/kodo/shellparser/_parser.py) | `parse_command(str) → ParsedCommand`, `ParsedCommand`/`Segment`/`Redirection` (frozen) | **Parse-only, judgement-free** — splits a shell command line into pipeline `Segment`s (on `\| \|\| && ; &`) via `shlex`, each with its `executable`/`args`/`redirections`; never raises (falls back to a naive split on malformed input). It does **not** decide whether a command mutates the filesystem — that heuristic is caller-side (`runtime/_checkpoints.py:command_may_mutate`, below) by design, so the parser stays reusable by future callers (e.g. the security layer) without inheriting checkpoint-specific judgment calls. |

**State:** Complete; covered by `test/test_shadow_mirror.py` and `test/test_shellparser.py`.

---

## 11. `subagents/` — agent files & prompt rendering

| Module | Defines | Links |
|---|---|---|
| [_loader.py](../src/kodo/subagents/_loader.py) | `SubAgent` (frozen: `name`, `tools: frozenset[str]`, `system_prompt`, `source_path`, `capability`, `display_name`, `subagents`, **`bases: tuple[str, ...]`**, **`subagent_order: tuple[str, ...]`**, **`purpose`**, **`solo: bool`**, **`critic`**, **`standalone: bool`**), `AgentLoadError`, `load_agent()` | Parses `subagent_<name>.md` frontmatter + body. Extracts the **`## Purpose`** body section (caller-agnostic "what this agent does / when to call it"); reads the `solo`/`critic`/`standalone` frontmatter that drives a caller's roster; keeps the `subagents:` allow-list in declaration order as `subagent_order`. |
| [_subagentspec.py](../src/kodo/subagents/_subagentspec.py) + [specs/](../src/kodo/subagents/specs/) | `SubAgentSpec` (frozen: `name`, `description`, `input_schema`, `output_schema`) + one literal per agent in `specs/_<name>.py`, aggregated as `ALL_SUBAGENTS` | The typed input/output contract of a sub-agent — "a tool with agentic behavior". Every sub-agent **except** the entry agents (`guide`, `problem_solver`) has one. `specs/_shapes.py` holds declarative schema builders (`pipeline_input`/`author_output`/`critic_output`). |
| [_registry.py](../src/kodo/subagents/_registry.py) | `AgentRegistry` | Loads all `subagent_*.md`, the two mandatory preambles `preamble_security.md` and `preamble_performance.md`, **and any `base_*.md` shared snippets**. **Renders the `## Tools` section from `ToolSpec` data** (one `_SPECS_BY_NAME` map over `ALL_TOOLS`), filtering `autonomous_mode == "unavailable"` tools when `autonomous=True`. **Renders the `## Subagents` roster from `{PLACEHOLDER:SUBAGENTS}`** (`render_subagents_section()`, public), now including each callee's input/output schema. For an agent with a `SubAgentSpec` (`SUBAGENT_SPECS_BY_NAME`, `spec_for()`), **auto-grants `return_result`** and **injects a `## Your Task Contract` section** (its own input + augmented output schema). Prepends the preambles (security, then performance), then the agent's referenced bases, then the contract. |

**Links:** `_registry` imports `ALL_TOOLS` from `toolspecs`. `get(name,
autonomous)` returns a `SubAgent` with `{PLACEHOLDER:TOOLS}` replaced and the
prompt composed as **preamble (security, then performance) → bases → agent body**.
Because the system prompt is rebuilt on every turn, the preambles (and bases) are
always present regardless of context compaction (compaction rewrites only the
message history). Consumed only by `WorkflowEngine`.

**Shared bases (`bases:` frontmatter):** an agent may list `bases: [<name>, …]`;
each names a `base_<name>.md` file in the subagents dir whose body is prepended
(after the preambles, before the agent's own body, which may specialize it).
`base_*.md` files are **not** globbed as agents (the agent glob stays
`subagent_*.md`), so they never register as spawnable agents. The registry
validates every referenced base exists and is non-empty at construction
(fail-fast, alongside the tool-resolution check). This lets a family of agents
share one contract without duplication — used by the **toolchain-setup** family
(`base_toolchain.md`) and the **dependency contract** (`base_dependencies.md`,
the `DEPENDENCIES.md` format spec) shared by its *writer* (`toolchain_python`,
`bases: [toolchain, dependencies]`) and its *reader* (`toolchain_depsmgr`,
`bases: [dependencies]`).

**Sub-agent roster (`{PLACEHOLDER:SUBAGENTS}`):** a *caller* agent (one with a
`subagents:` allow-list) may embed `{PLACEHOLDER:SUBAGENTS}`. The registry replaces
it with a **roster**: a short **intro paragraph** (workflow vs standalone), then a
table of the invocable sub-agents, then each listed sub-agent's `## Purpose`
paragraph, all in the caller's allow-list order. The roster is built from the
**callee** agents' frontmatter + body, so a sub-agent's description lives once with
it and is reused by every caller (the `## Purpose` text is written
**caller-agnostic**). Table rules, per callee frontmatter: a `critic: <name>` marks
an **author** → one `run_author_critic_iteration` row naming the critic; `solo: true`
→ a `run_subagent` row; a **pure critic** (neither) is absorbed into its author's row
and gets no row of its own (but still gets a purpose paragraph). The **`Kind`** column
reads `standalone` when the callee declares `standalone: true`, else `workflow` —
distinguishing on-demand specialists (e.g. `toolchain_python`) from ordered-pipeline
agents. The roster carries **no ordering column**: ordering lives in the caller's
prose (the Guide's numbered pipeline + the Design Plan), since a single linear
predecessor (`depends_on`, now removed) misrepresented the real inter-agent
dependencies. An agent can be **both** solo and a critic — the renderer still
supports that (a solo+critic gets its own `run_subagent` row *and* its author's
`critic_name`), though no live agent currently is one.
`render_subagents_section(name)` is public so prompt-review tooling can render a
caller's roster even when its body omits the placeholder. Validated fail-fast at
construction (every listed sub-agent must exist and carry a `## Purpose`). Live
users: **`problem_solver`** (lists `toolchain_python`) and **`guide`** (the full
pipeline + `toolchain_python`; its `## Subagents` section embeds the placeholder and
a thin stage→agent map replaces the old hand-written `### Sub-Agent Names` table).
See [GUIDE_PROMPT_REVIEW.md](GUIDE_PROMPT_REVIEW.md) for the live assembled prompt and
the amendment record.

**The agents + 2 preambles + shared bases** (frontmatter `tools:` lists):

The **security preamble** carries the confidentiality / injection-resistance /
role-fixing / tool-discipline / output-hygiene rules. The **performance
preamble** carries execution-quality rules: Communication Style, Reasoning Is
Silent, **Edit Discipline** (targeted, minimal edits; prefer a targeted
`edit_file` over regenerating a whole file; no drive-by changes), Read Before You
Write, Match Existing Conventions, Verify Don't Assume, and Stay In Scope.

| Agent | Tools declared | Role |
|---|---|---|
| `guide` | guided_dev_status, get_root_paths, find_files, find_text_in_files, run_subagent, run_author_critic_iteration, ask_user, rollback, finalize_project, disable_autonomous_mode, **create_new_project** | Arbiter for the **guided** workflow. Resolved through the same `tools_for_agent` path as every other agent. `subagents:` allow-list includes the pipeline agents **+ `toolchain_python`**. |
| `problem_solver` | filesystem, edit_file, run_command, **toolchain_build/deps**, **run_subagent**, ask_user, **create_new_project** | Standalone generalist for the **problem-solving** workflow — runs *outside* the Guide pipeline, talking to the user directly and editing real files on disk (see §15). Declares `run_subagent` + `subagents: [toolchain_python]`, used only to delegate toolchain setup. **Embeds `{PLACEHOLDER:SUBAGENTS}` in a `## Subagents` section** — the live caller of the roster mechanism. |
| `toolchain_python` | run_command, filesystem, edit_file, find_files, find_text_in_files, get_root_paths, ask_user | **Toolchain-setup** agent (`bases: [toolchain, dependencies]`). Spawnable by both `guide` and `problem_solver`. Bootstraps/converts a project: generates the five per-platform build scripts (`scripts/{build,format,static_analysis,test,full_build}.{sh,ps1}`) + a `DEVELOPMENT.md` (build/check/test how-to) + a `DEPENDENCIES.md` (the dependency contract), now actually **executed** by `toolchain_build`/`toolchain_deps` (§8). Suggest-then-confirm invocation. |
| `toolchain_depsmgr` | get_root_paths, find_files, read_file, run_command, edit_file | **Dependency-management** agent (`bases: [dependencies]`). The acting force behind the `toolchain_deps` tool — **not** spawnable via `run_subagent` by anyone (no agent lists it; the tool drives it through the ungated `run_dependency_manager` service). Per run it performs one add/remove/update op by reading and executing the project's `DEPENDENCIES.md`; returns `status: completed/failed/dependencies_md_missing`. Toolchain-agnostic: all language specifics come from `DEPENDENCIES.md`. |
| `narrative_author` | filesystem, edit_file, read_file, ask_user | Solo, user-facing intake. Writes the Narrative and Tech Stack documents directly. |
| `architect`, `requirements_author`, `functional_designer`, `e2e_test_designer`, `test_designer` | filesystem, edit_file, read_file, escalate_blocker | Authors (paired with a critic); `coder` and `e2e_test_coder` additionally hold `toolchain_build`/`toolchain_deps`. |
| `architect_critic`, `requirements_critic`, `functional_design_critic`, `test_design_critic`, `e2e_test_design_critic`, `code_critic`, `e2e_test_code_critic` | read_file, document_feedback | Critics — record a verdict per file; the engine alone drives the accept/review flow (§7/§12.1). `test_design_critic` reviews the per-component Test Plan, holding every test to behavior over implementation; `e2e_test_code_critic` reviews the end-to-end suite *as code*, enforcing opaque-box, behavior-and-side-effect assertions over implementation details. |
| `test_coder` | filesystem, edit_file, read_file, escalate_blocker | Solo author of test code + stubs from the accepted Test Plan (no longer a critic — plan review moved to `test_design_critic`). |
| `e2e_test_coder` | filesystem, edit_file, read_file, toolchain_build, toolchain_deps, escalate_blocker | Author (paired with `e2e_test_code_critic`) of the product-level end-to-end integration suite (stage 9). Assembles the whole system as a black box behind local mock servers + injected configuration, runs it via `toolchain_build`, and iterates to a clean state before the critic; a genuine system-behavior mismatch is surfaced to the guide via `escalate_blocker` (`system_behavior_mismatch`), not papered over. |

**State:** Loader/registry complete (incl. `bases:` shared snippets **and the `{PLACEHOLDER:SUBAGENTS}` roster from per-agent `## Purpose` + `solo`/`critic`/`standalone` frontmatter**); agent roster present (pipeline + `problem_solver` + the `toolchain_python` toolchain-setup agent); every declared tool now has a dispatch handler.

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

It **internally constructs**: a `SessionState`, one `_EngineServices` adapter,
and a **`__root_mirrors: RootMirrorManager`** (§12.4/§10b — the *single*
shadow-git mirror coordinator now shared by both workflow modes; there is no
separate Guided-only mirror anymore, see §7). It builds a
`tools.ToolDispatcher` **per agent run** (via `__make_dispatcher`). A document's
state is never reconstructed at bootstrap — `kodo.guided_state` reads each
file's `.jsonl` log on demand. It owns `__main_messages` (the shared
entry-agent running `list[Message]`, agent-agnostic across Guide/Problem
Solver) and cumulative USD.

**Composition / call graph:**

- `start()` → `TransientStore.attach_session` → spawns `__run_worker` task. If
  resumed, loads messages, re-binds the persisted current project (if any) via
  `__bind_project`, and may re-fire a pending prompt. No index to rebuild —
  `bind_project`/`__bind_project` now only validate the `ProjectLayout`.
- **Public client entry points** (registered as WS handlers in `_app`, §14):
  `handle_prompt_submit(text, request_id)` enqueues a prompt;
  `handle_mode_set(autonomous)` sets the **Autonomous/Interactive** mode
  (`SessionState.autonomous`, user-facing) and persists it; `handle_workflow_set(mode)`
  sets the **Guided/Problem-Solving** workflow (`SessionState.workflow_mode`,
  normalised to `"guided"` | `"problem_solving"`); `stop()` cancels the worker.
  Both setters emit `EVT_STATE` and never interrupt an in-flight prompt.
- `__run_worker()` — dequeues one task at a time. **First it freezes the
  per-prompt autonomous mode** (`effective_autonomous = autonomous`), then
  **routes by `workflow_mode`**: `"problem_solving"` →
  `__run_problem_solver_with_input` (if the `problem_solver` agent is present,
  else `__handle_input_no_agent`); otherwise → `__run_guide_with_input`.
  Exits the loop once `phase == "done"`.
- `__resolve_plugin(capability)` → reads fresh settings → `get_llm_registry()` →
  builds `ClaudePlugin` (via `ApiKeyProvider.get_key`) or `LlamaPlugin`, wrapped
  in `LoggingLLMPlugin`.
- `__run_agent_turn(...)` — the **generic LLM tool loop**, shared by guide
  and leaf agents: streams events → emits `EVT_LLM_TURN_START`, stream chunks,
  `EVT_AGENT_TOOL_CALL`, `EVT_USAGE_UPDATE` → logs via `ToolCallLogger` +
  `TransientStore.write_agent_record` → dispatches each tool via an injected
  `tool_dispatch` callback → loops until no tool calls (or `stop_after_tools`).
- `__run_guide_with_input` → builds a `ToolDispatcher` for the
  guide, `tool_dispatch = dispatcher.dispatch`,
  `tools = tools_for_agent(agent.tools)` (the registry already filtered the
  agent's tools for `effective_autonomous`).
- `__run_problem_solver_with_input` → the **problem-solving** counterpart of the
  guide loop: loads the `problem_solver` agent, keeps its own running
  history (`__ps_messages`), and runs the same `__run_agent_turn` with its own
  per-run `ToolDispatcher` and `stop_after_tools = lambda: dispatcher.stop_requested`.
  It works the prompt end to end alone (no sub-agents, no critics) and yields
  back to the user. Both loops read `effective_autonomous`, so `ask_user` is
  withheld in autonomous mode for the Problem Solver too.
- `__run_subagent` → builds a per-run `ToolDispatcher`, `tools =
  tools_for_agent(agent.tools)`, `tool_dispatch = dispatcher.dispatch`,
  `stop_after_tools = lambda: dispatcher.stop_requested`. Returns the
  sub-agent's structured `return_result` output (or a bare
  `{schema_compliance: False}` fallback if it never called it — there is no
  artifact index to recover a partial result from).
- `__run_dependency_manager` (exposed via `_EngineServices.run_dependency_manager`,
  the callback the `toolchain_deps` tool invokes) → drives the fixed
  `toolchain_depsmgr` agent straight through `__spawn_subagent` (the ungated
  primitive), **bypassing the `__assert_can_spawn` allow-list gate** that
  `__run_subagent` applies. Possession of the `toolchain_deps` tool is the
  authorization; the agent is deliberately absent from `_DIRECT_ONLY_AGENTS`
  (which would make `__spawn_subagent` short-circuit it) and from every
  `subagents:` list, so the only path to it is the tool.
- `__run_author_critic_iteration` → spawns the author (`for_revision_path` set
  when revising), reads back `author_output.primary_path`, spawns the critic
  against that path, then reads `kodo.guided_state.read_status(path)` for the
  verdict — **the jsonl, not the critic's `return_result`, is authoritative**.
  Emits `EVT_REVIEW_STARTED`/`EVT_REVIEW_VERDICT`. **This is the callback the
  Guide's `run_author_critic_iteration` tool invokes.**
- `__finalize_document(path)` (called from the post-dispatch hook below, not
  exposed via `EngineServices` — there is no tool indirection) → autonomous
  mode immediately `append_accepted`s; interactive mode fires the same
  approval gate `request_user_review_artifact` used to, then records
  `append_review_result` (+ `append_accepted` on agreement). Replaces the old
  `__complete_artifact`/promotion path entirely — there is nothing to
  materialize, since the document was already a real file.
- `__record_guided_revision(...)` (also called from the post-dispatch hook) →
  after a `filesystem`/`edit_file` checkpoint commit, if the affected path is
  tracked under the bound project's `specs`/`src`/`test`, appends a
  `new_revision` jsonl entry carrying that exact commit's sha — in **both**
  workflow modes (§7).
- `__run_rollback` (exposed via `_EngineServices.rollback`) → delegates
  directly to `RootMirrorManager.rollback` (the same primitive Problem Solver
  uses) and resets the in-memory conversation. No index to rebuild.
- `__disable_autonomous` (exposed via
  `_EngineServices.disable_autonomous_mode`) backs the
  guide's `disable_autonomous_mode` tool.
- `__create_project` (exposed via `_EngineServices.create_project`) backs the
  `create_new_project` tool: slugify name → `_unique_child_dir` under the
  session physical root → `mkdir` → add to the logical-root map →
  `RootMirrorManager.prepare` (scaffolds `.kodo/`+mirror) → push
  `EVT_WORKSPACE_ADD_FOLDER`.
- **Per-tool-call checkpointing (both workflow modes)** — `__checkpoint_enabled()`
  is now unconditional (Guided mode drives the same mirror Problem Solver
  always has — there is no separate Guided checkpoint system to collide with,
  see §7). Inside `__dispatch_tool_calls`, around each of `_MUTATING_TOOLS =
  {"filesystem", "edit_file", "run_command"}`: `__checkpoint_prepare(tool_name,
  tool_input)` resolves the affected path(s) (`__mutation_paths` — `edit_file`'s
  `path`; `filesystem`'s `destination`/`path`/`source`; `run_command`'s `cwd`,
  gated by `command_may_mutate(parse_command(cmd))`, §10b) and calls
  `__root_mirrors.prepare(path)` **before** dispatch, so the baseline commit
  captures pre-change state. After dispatch, `__checkpoint_commit(...)` calls
  `__root_mirrors.commit_for_path(path, label)` (`run_command` additionally
  `sweep_initialized`s every other already-initialised mirror, to catch writes
  outside the command's `cwd`). `__finalize_tool_result` injects the resulting
  `checkpoint.sha` into the LLM-visible result as `checkpoint_sha` (declared
  optional in each of the 3 tools' `output_schema`, so `normalize_output` keeps
  it without flagging non-compliance), rides `{root, sha, parent}` out-of-band
  on `EVT_AGENT_TOOL_CALL_DETAIL` as a `"checkpoint"` key (`null` when no commit
  happened), and — for `filesystem`/`edit_file` only — drives
  `__record_guided_revision` (above). New public `handle_checkpoint_undo(root,
  sha)` / `handle_checkpoint_rollback(root, sha)` delegate straight to
  `RootMirrorManager.undo`/`.rollback` — **files-only**, they never touch
  conversation history (deliberately distinct from the Guide's
  conversation-rewinding `rollback` tool, which now calls the same
  `RootMirrorManager.rollback` primitive but additionally resets
  `__main_messages`).

**The engine injects into every `ToolDispatcher`:** `GateOrchestrator`,
`SessionState`, and one `_EngineServices` adapter wrapping `__run_subagent` /
`__run_author_critic_iteration` / `__run_rollback` /
`__disable_autonomous`. The per-prompt autonomous mode is read
from `SessionState.effective_autonomous` rather than passed in.

### 12.2 Tool dispatch (`tools.ToolDispatcher`)

Dispatch no longer lives in `runtime`; see §6A. The engine builds one
`tools.ToolDispatcher` per agent run (guide and leaf alike) and passes its
`dispatch` as the `tool_dispatch` callback into `__run_agent_turn`. After the run
it reads `dispatcher.returned_output` and uses `dispatcher.stop_requested`
as the `stop_after_tools` predicate. There is one unified surface — no
guide-vs-leaf split.

### 12.4 Supporting runtime modules

| Module | Defines | Role / links |
|---|---|---|
| [_bootstrap.py](../src/kodo/runtime/_bootstrap.py) | `locate_guide_session()` | Workspace-tier session location only: locate/create the Guide session marker + `sessions/` dir. There is no project-tier bootstrap anymore — a document's state lives entirely in its own `.jsonl` evolution log (§7), read on demand. |
| [_guide.py](../src/kodo/runtime/_guide.py) | `GuideMarker` | Reads/writes `.kodo/guide.session`. Used by `locate_guide_session`. |
| [_checkpoints.py](../src/kodo/runtime/_checkpoints.py) | `RootMirrorManager`, `CheckpointRef` (frozen), `command_may_mutate()` | The **single** shadow-git mirror coordinator, now driving both workflow modes (§12.1) — there is no longer a second, Guided-only mirror at the same path to collide with. Bridges the path-agnostic `mirror.ShadowMirror` to Kōdo's conventions: every root a session may touch gets its own independent mirror at `<root>/.kodo/checkpoints`, created **lazily** the first time a file-mutating tool writes under that root (scaffolding `<root>/.kodo/` + `kodo.md` via `ProjectLayout.scaffold_kodo_dir()`, §5, at that moment). `_root_for(path)` maps a path to its enclosing root by longest-prefix match. `_KODO_EXCLUDES` (node_modules/.venv/`__pycache__`/dist/build/egg-info/caches + always `.kodo/`+`.git/`) seed each mirror's `info/exclude` **on top of** the project's own `.gitignore` — this is *why* `.kodo/guided_dev_state/*.jsonl` (§7) is never committed by this same mirror. One `asyncio.Lock` serialises `prepare`/`commit_for_path`/`sweep_initialized`/`undo`/`rollback`. The free function `command_may_mutate(parsed: ParsedCommand) -> bool` is the caller-side mutation heuristic the parser (§10b) deliberately omits: `True` if any redirection is an output redirect (`> >> >\| &> &>> <>`), else `True` unless every executable's basename is on a small read-only allow-list (`ls cat grep find rg fd pwd wc diff …` — notably **not** `git`, since even read-only-looking git subcommands can touch `.git/` state) — **defaults to `True` (mutating) whenever uncertain**, so a missed checkpoint is never the failure mode; an unnecessary no-op commit is. |
| [_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator`, `ApprovalResponse`, `QuestionResponse` | **Composes** `WebSocketDispatcher` + `TransientStore`. `fire_approval`/`fire_question` send `kind=request`, register a future, persist the pending prompt (for restart re-surface), and await. `fire = fire_approval` alias. Satisfies `tools.GateLike`; reached by `__finalize_document` (§12.1) for the interactive document-review gate. |
| [_session.py](../src/kodo/runtime/_session.py) | `SessionState` | Mutable `phase`/`agent`/`component` plus the two mode fields: `autonomous` (user-facing Autonomous/Interactive, set by `handle_mode_set`, reported in `to_dict()`/`EVT_STATE`) and `effective_autonomous` (frozen per prompt by `__run_worker`; what tools/registry actually read), and `workflow_mode` (`"guided"`/`"problem_solving"`, in `to_dict()`). Shared by the engine; satisfies `tools.SessionLike` (`finalize_project` writes `phase`; tools read `effective_autonomous`). |
| [_session_log.py](../src/kodo/runtime/_session_log.py) | `SessionLog` | Append-only JSONL per session. |

**State:** Engine, dispatch (now in `tools/`), gates, rollback are
implemented and exercised by the guide/author-critic flow.

---

## 13. `state/` & `security/`

| Module | State |
|---|---|
| [state/_transient.py](../src/kodo/state/_transient.py) `TransientStore` | ✅ Per-session dir under `.kodo/sessions/<id>/`: `meta.json`, `transient.json` (stage/prompt/autonomous/pending_prompt), `session.jsonl` (guide messages), `agents/*.jsonl`. Injected into engine + gate. |
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

Then it registers `HandlerFn`s on the dispatcher (`hello`, `ping`, `prompt.submit`,
`mode.set` → `handle_mode_set` (Autonomous/Interactive), `workflow.set` →
`handle_workflow_set` (Guided/Problem-Solving), `stop`, `config.reload`, llama
install/start/stop, model.install, **`checkpoint.undo` → `_handle_checkpoint_undo`,
`checkpoint.rollback` → `_handle_checkpoint_rollback`**) — `mode.set`/`workflow.set`
each reply with a `mode.accepted`/`workflow.accepted` response, and the two
checkpoint handlers each pull `{root, sha}` from the request payload, call the
matching `engine.handle_checkpoint_undo`/`handle_checkpoint_rollback` (§12.1/§10b),
and reply `{type: "checkpoint.undo.done"|"checkpoint.rollback.done", root, sha:
<new sha>}` —
stores the engine on the app, and hooks `_start_background`/`_stop_background`
(which call `engine.start()`/`engine.stop()` and adopt any surviving
llama-server).

**State:** Complete.

---

## 15. End-to-end flows

**Prompt → work:** client `prompt.submit` → `_app` handler →
`engine.handle_prompt_submit` (enqueues) → worker → `__run_guide_with_input`
→ `__run_agent_turn` streams the Guide LLM → tool calls dispatch through
the guide's `tools.ToolDispatcher` → `run_subagent`/`run_author_critic_iteration`
call back into the engine (via the injected `EngineServices`), which spawns leaf
agents — each with its own `ToolDispatcher` — that write real files directly
under `specs/`/`src/`/`test/` via `filesystem`/`edit_file`, tracked by a
`.jsonl` evolution log per file (§7), not an artifact store. This is the
**guided** workflow.

**Prompt → work (problem-solving):** when `workflow_mode == "problem_solving"`,
the worker routes the same prompt to `__run_problem_solver_with_input` instead.
The standalone `problem_solver` agent runs one `__run_agent_turn` with its own
dispatcher, reading/writing the project's real files via the file-I/O and
`run_command` tools and talking to the user directly (`ask_user`, plus
`<kodo_info>` progress callouts in its message text) — no Guide, no sub-agents,
no critics, no artifacts.

**Mode toggles (both apply to the *next* prompt):** the VSIX sidebar has two
toggles. *Autonomous/Interactive* → `toggle_autonomous` → `mode.set {autonomous}`
→ `handle_mode_set` sets `SessionState.autonomous` (and persists it); it does
**not** touch the in-flight prompt — `__run_worker` copies it into
`effective_autonomous` only when the next prompt is dequeued, so the sidebar
shows a "applies to your next prompt" notice. *Guided/Problem-Solving* →
`toggle_workflow_mode` → `workflow.set {mode}` → `handle_workflow_set` sets
`SessionState.workflow_mode`, which the worker reads at the next dequeue to pick
the entry agent. Both emit `EVT_STATE`; the Guide can also drop autonomous
mid-run via the `disable_autonomous_mode` tool (engine `__disable_autonomous`
clears both `autonomous` and `effective_autonomous` immediately and emits
`EVT_AUTONOMOUS_CHANGED`).

**Document acceptance:** a critic calls `document_feedback(path, accept=True,
concerns=[])` → `tools/_document_feedback.handle` appends a `feedback` jsonl
entry (§7) and returns `{"status": "recorded"}` → the engine's post-dispatch
hook in `__finalize_tool_result` sees `accept: true` and calls
`__finalize_document(path)`: autonomous mode immediately appends an `accepted`
entry; interactive mode fires the approval gate, then appends `review_result`
(+ `accepted` on agreement). There is no promotion step — the file was already
real.

**User gate:** any `ask_user`/`escalate_blocker`, or the document-review gate
inside `__finalize_document` → `GateOrchestrator.fire_*` sends a `kind=request`,
registers a future, persists `pending_prompt`, and awaits the client's
`kind=response`. The autonomous mode in force is
`SessionState.effective_autonomous`, frozen by the worker when it dequeues the
prompt; a user toggle mid-prompt updates `autonomous` (UI-facing) but only
takes effect at the next prompt. In autonomous mode `ask_user` is withheld
entirely and document review auto-accepts.

**Restart:** `GuideMarker` + `TransientStore` resume the session; an unanswered
`pending_prompt` is re-surfaced. There is no index to rebuild — every
document's state is read from its own `.jsonl` log on demand (§7).

**Rollback:** Guide `rollback` → `tools/_rollback.handle` →
`EngineServices.rollback` → engine `__run_rollback` → directly
`RootMirrorManager.rollback` (the same primitive Problem Solver's
checkpoint-card "Rollback to this state" control uses) → engine resets the
in-memory conversation and starts fresh.

**Per-tool-call checkpointing + undo/rollback (both workflow modes, §10b/§12.1):**
a `filesystem`/`edit_file`/`run_command` dispatch, in **either** mode, is
bracketed by `__checkpoint_prepare` (baselines the enclosing root's
`RootMirrorManager` mirror, scaffolding `.kodo/`+`kodo.md` lazily on first touch)
and `__checkpoint_commit` (commits the real tree, surfacing `{root, sha, parent}`
on `EVT_AGENT_TOOL_CALL_DETAIL` and `checkpoint_sha` in the tool's own result).
For `filesystem`/`edit_file`, that same checkpoint also drives
`__record_guided_revision` (§7) when the affected path is tracked. The
WebView renders an **"↩ undo this change"** link next to that tool call and a
**"⟲ Rollback to this state"** control below its params box whenever a checkpoint
rode along. Clicking either sends `checkpoint.undo`/`checkpoint.rollback`
`{root, sha}` → `_app._handle_checkpoint_undo`/`_rollback` → engine
`handle_checkpoint_undo`/`handle_checkpoint_rollback` → `RootMirrorManager.undo`/
`.rollback` → `ShadowMirror.undo`/`.rollback`, each producing a **new** append-only
commit (`undo` restores only the files the target commit touched; `rollback`
restores the whole tree to that commit). Neither path touches conversation
history — files-only, agent-loop-agnostic. **Known limitations:** a
`run_command` that writes into a root other than its `cwd` is only captured if
that other root has already been touched at least once (no global "first ever
write" sweep across every possible root); a cross-root move/copy surfaces an
undo/rollback control only on the destination root's checkpoint, not the
source's.

---

## 16. Implementation-state summary

| Subsystem | State |
|---|---|
| `common`, `transport`, `project`, `guided_state` (per-document evolution log, §7), `state/_transient` | ✅ Complete, well-tested |
| `llms` (Anthropic + llama.cpp, incl. merged local-inference utilities) | ✅ Complete |
| `toolspecs` catalog, `subagents` loader/registry, `tools` dispatch | ✅ Complete — every dispatchable spec has a handler |
| `runtime` engine / gates / rollback | ✅ Functional |
| `mirror`/`shellparser` (§10b) + `runtime/_checkpoints.RootMirrorManager` — generic checkpoint/undo/rollback | ✅ Implemented; now drives **both** workflow modes (§12.1) — there is no longer a second Guided-only mirror. Two documented limitations (§15). |
| Toolchain agent tools (`toolchain_build`/`toolchain_deps`) | ✅ Implemented. `toolchain_build` executes the toolchain-setup agent's generated `scripts/<step>` (§8). `toolchain_deps` spawns the `toolchain_depsmgr` sub-agent (via the ungated `run_dependency_manager` service) to execute the project's `DEPENDENCIES.md` for one add/remove/update op; a missing `DEPENDENCIES.md` comes back as a remediation message pointing at the toolchain-setup agent (§8). |
| `disable_autonomous_mode` | ✅ Implemented and dispatched (guide) |
| `create_new_project` | ✅ Implemented and dispatched (guide + problem_solver); scaffolds a new project dir + checkpoint mirror and adds it to the workspace |
| Native file-IO / `run_command` / `read_file` tools | ✅ Implemented; granted to authoring sub-agents and `problem_solver` |
| Two workflows (`guided` Guide / `problem_solving` Problem Solver) | ✅ Implemented; selected by `workflow.set` → `SessionState.workflow_mode` |
| `security/*`, `state/_memory` | ⛔ Stubs |
| `project/_manifest` | ◽ Parsed by `kodo.md`'s `## Toolchain` heading; purely informational now (no engine-side toolchain selection) |

---

## 17. Cross-cutting observations

1. **No in-memory index at all.** A document's state is the last line of its
   own `.jsonl` evolution log (`kodo.guided_state`, §7) — read fresh on every
   query (`guided_dev_status` re-walks the directory each call). There is
   nothing to construct at bootstrap, nothing to rebuild on rollback, and
   nothing shared across `ToolDispatcher` instances.
2. **One tool-dispatch surface, one generic loop.** `__run_agent_turn` is
   agent-agnostic; the only difference between the Guide and a leaf agent
   is the `tools` list (from each agent's frontmatter via `tools_for_agent`). Both
   route through the same `tools.ToolDispatcher`; per-run state
   (`stop_requested`/`returned_output`) lives on each run's `ToolContext`, so
   tools never bleed across agent types.
3. **Stateless LLM calls.** Tool specs are re-sent on every `stream_query`; the
   `messages` list (with `tool_use`/`tool_result` blocks) is the only memory.
4. **Structural protocols decouple the seams.** `MessageSink`
   (`WebSocketDispatcher`), `ApiKeyProvider` (`KeyBroker`) — no inheritance,
   just shape-matching, which keeps `runtime` independent of `transport`/`server`.
5. **The prompt ↔ surface gaps** in §6/§11 are the highest-signal place to look
   when an agent "can't call a tool it was told about."
