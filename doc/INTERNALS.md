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
| `findings` | *(nothing)* |
| `state` | *(nothing)* |
| `security` | `common`, `toolspecs`, `shellparser` |
| `mirror` | *(nothing)* |
| `shellparser` | *(nothing)* |
| `binutils` | *(nothing)* |
| `transport` | `common` |
| `toolspecs` | *(nothing — pure data)* |
| `tools` | `common`, `findings`, `guided_state`, `project`, `toolspecs` |
| `llms` | `common`, `transport`, `toolspecs` |
| `subagents` | `toolspecs` |
| `titling` | `project`, `llms` |
| `runtime` | `common`, `transport`, `toolspecs`, `tools`, `findings`, `guided_state`, `project`, `state`, `subagents`, `llms`, `titling`, `mirror`, `shellparser`, `binutils` |
| `server` | `common`, `transport`, `project`, `state`, `subagents`, `llms`, `titling`, `runtime`, `binutils` |

`toolspecs` is now a true leaf: the old `toolspecs → workspace` edge (importing
`ArtifactType` for `list_artifacts`'s schema) is gone along with the artifact
system. The critic finding-item shape lives once, in
`kodo.subagents.specs._shapes`, since the tool that used to duplicate it
(`document_feedback`) is gone.

`findings` (§7a) is a second leaf alongside `guided_state`, and deliberately
*not* an edge between them: `guided_state` is project-scoped, `findings` is
session-scoped, and a project-scoped store must not depend on session state.
They meet in exactly one place — `kodo.tools.document_status()` — which is why
that function lives in `tools` (which may import both) rather than inside
either package.

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
> The local-inference utilities (installer, llama-server manager) remain
> merged into `llms/llamacpp/` (`_installer.py`, `_llama_server.py`,
> `_manager.py`), re-exported from `kodo.llms.llamacpp` — unrelated to this
> change, noted here only because it was the other half of the historical
> "two packages were merged" note this section used to carry. The old
> `_downloader.py` (a `huggingface_hub.hf_hub_download` wrapper) has since
> been deleted; downloads now go through `kodo.llms.local.LocalModelManager`
> directly, reached via `_manager.py`'s `get_local_model_manager` (see
> `kodo/doc/LOCAL_MODEL_MANAGER.md`).

### 2.2 Layered diagram

Lowest tier = packages that import nothing from `kodo`; each tier above imports
only from tiers below it. Lines are imports (`▼` points from importer to
imported); the annotation on each line names the packages pulled in.

```text
 T5  ┌──────────┐
     │  server  │  ▼ runtime · llms · titling · subagents ·
     └────┬─────┘    state · project · transport · common
          │
          ▼
 T4  ┌──────────┐
     │ runtime  │  ▼ tools · llms · titling · subagents · toolspecs ·
     └────┬─────┘    findings · guided_state · state · project · transport · common
          │
 T3a ┌─────────┐
     │ titling │  ▼ llms (only) — sits above llms/T3 (its GGUF download +
     └─────────┘    llama.cpp-install lookup), below runtime; not a peer of
                    subagents/llms/tools (T3), none of which it imports.
          │
   ┌──────┴───────┬───────────────┐
   ▼              ▼               ▼
 ┌───────────┐   ┌────────┐   ┌───────────┐
 │ subagents │   │  llms  │   │   tools   │              T3   (llms ⊇ llamacpp utils;
 └─────┬─────┘   └───┬────┘   └─────┬─────┘                    tools imported only by runtime)
       │ toolspecs   │ toolspecs    │ toolspecs · guided_state · project · common
       │ skills      │              │ skills
       │             │ transport    │
       │             │ common       │
       ▼             ▼              ▼
 ┌───────────┐   ┌──────────┐
 │ toolspecs │   │ security │                           T2   (toolspecs: pure data, imports nothing;
 └───────────┘   └────┬─────┘                                 security: ▼ common · toolspecs · shellparser,
                      │ common · toolspecs · shellparser      imported ONLY by runtime)
                      ▼
 ┌───────────┐
 │ transport │                                          T1
 └─────┬─────┘
       │ common
       ▼
 ┌────────┬─────────┬──────────────┬───────┬────────┬─────────────┬──────────┬───────────┬────────┐
 │ common │ project │ guided_state │ state │ mirror │ shellparser │ binutils │ websearch │ skills │   T0  ← import nothing from kodo
 └────────┴─────────┴──────────────┴───────┴────────┴─────────────┴──────────┴───────────┴────────┘
```

`runtime` is the sole importer of `mirror`, `security`, and (`security` aside)
`shellparser` (via `runtime/_checkpoints.py` §10b and `runtime/_engine/`) —
none of the three is reachable from `tools`, `subagents`, or `llms`. `tools`
sees the security layer only through the `SecurityLike` structural protocol
(doc/SECURITY.md §4).

`titling` moved out of T0/T1 (2026-07-18): it used to import only `project`
(a self-contained `transformers`/`torch` model, doc/INTERNALS.md §10c); now
that session titling runs its own dedicated llama-server, it needs
`kodo.llms.llamacpp.find_installed` (to locate the shared llama.cpp binary)
and `kodo.llms.local.LocalModelManager` (to download its own GGUF) — both
leaf utilities of `llms`, not `LlamaServer` itself (see §10c for why). This
puts `titling` one tier above `llms`, not alongside `subagents`/`tools` in
T3 — it does not import (or get imported by) any of those three.

(`runtime` and `server` also reach past the tier directly below them — e.g.
`runtime → toolspecs`/`guided_state`/`common` — as the matrix in §2.1 lists in full;
only the principal lines are drawn above to keep the figure readable.)

- **T0 — leaf packages** (`common`, `project`, `guided_state`, `state`,
  `mirror`, `shellparser`, `binutils`, `websearch`, `skills`): import nothing
  from `kodo`.
  `state/_memory.py` is a **stub** (see §13); `mirror`/`shellparser`
  are the checkpoint/parse primitives consumed by `runtime` (§10b) — and
  `shellparser` also by `security`; `binutils` is the
  third-party util manager (§10a); `guided_state` is the per-document evolution
  log (§7) that replaced `kodo.workspace`; `websearch` is the Playwright- and
  `curl_cffi`-backed fetch engine behind `query_search_engine`/`web_search`
  (doc/WEB_SEARCH.md) and the single-page fetch behind `read_webpage`
  (doc/READ_WEBPAGE.md), consumed only by `tools`; `skills` is the
  user-installed Agent Skills store under `~/.kodo/skills` (doc/SKILLS.md),
  taking its root as a constructor argument — which is what keeps it a leaf —
  and consumed by `tools` (the `use_skill` handler), `subagents` (the
  `{SKILLS}` prompt catalog) and `server` (`skills.list`/`skills.delete`).
- **T1**: `transport` (wire framing over `common`).
- **T2**: `toolspecs` (tool catalog) — now a true leaf, importing nothing from
  `kodo` (the old `toolspecs → workspace` edge for `ArtifactType` is gone) —
  and `security` (the per-call allow/ask judgement engine over the catalog +
  the shell parse, doc/SECURITY.md). `security` left T0 when it was
  implemented; it is consumed only by `runtime` and never by `tools`. `security`
  also imports `common` (`system_temp_roots()` — the OS-temp-directory helper
  shared with `tools`'s path resolvers, doc/SECURITY_RULES_PLAN.md's "OS temp
  directory carve-out").
- **T3**: `subagents` (prompt renderer over `toolspecs`), `llms` (LLM streaming;
  its `llamacpp` subpackage also holds the local-inference lifecycle utilities
  merged from the former `llm_utils`), and `tools` (the **dispatch
  implementation** of every tool in the catalog — one `Tool` subclass per tool).
  `tools` has a hard import ceiling of T0/T1/T2 (`guided_state` + `project` +
  `skills` + `toolspecs` + `common`, the last for the same `system_temp_roots()` helper
  `security` uses — routed through `common` rather than a direct
  `tools → security` import so the two stay decoupled); the collaborators it
  needs from higher tiers — the gate, the session, the sub-agent launcher —
  are inverted via structural Protocols and injected by `runtime`. It is
  imported only by `runtime`, never by `subagents` or `llms`.
- **T3a**: `titling` (the session-title summarizer — a dedicated,
  llama.cpp-hosted Qwen3-0.6B chat model, doc/INTERNALS.md §10c). Imports
  only `llms` (`find_installed`, `LocalModelManager`) and `project` — one
  tier above `llms`/T3, since it needs those two leaf utilities, but not a
  peer of `subagents`/`tools` (neither imports it, and it imports neither).
  Imported only by `runtime` (`SessionTitler`) and `server` (startup/install/
  update lifecycle, §10).
- **T4 — `runtime`**: the engine; composes nearly every domain service and
  builds a per-run `tools.ToolDispatcher` for each agent (guide or leaf).
- **T5 — `server`**: the composition root; builds the object graph and registers
  handlers.

---

## 3. `common/` — wire envelope, protocols & shared platform facts

| Module | Defines | Notes |
|---|---|---|
| [_envelope.py](../src/kodo/common/_envelope.py) | `Envelope` (frozen dataclass), `MessageKind` (Literal) | The atomic WS frame `{kind, id, correlation_id?, payload}`. Factory classmethods: `make_response`, `make_event`, `make_stream_chunk`, `make_thinking_chunk`, `make_stream_end`; plus `to_json`/`from_json`. |
| [_protocols.py](../src/kodo/common/_protocols.py) | `ApiKey` (frozen dataclass), `MessageSink` (Protocol), `ApiKeyProvider` (Protocol) | `MessageSink.send(env)` and `ApiKeyProvider.get_key(vendor)` are the two seams that decouple the engine from the transport and the key broker. |
| [_tempdir.py](../src/kodo/common/_tempdir.py) | `system_temp_roots() -> tuple[str, ...]` | Candidate OS temp-directory roots — `tempfile.gettempdir()` and, on POSIX, the literal `/tmp` — each included both as-is and `realpath`-resolved (covers macOS's `/tmp` → `/private/tmp` symlink without dropping the literal spelling a command might use directly). The single source of truth for "is this path system-temp scratch space" — consumed by both `security._analysis` (the `run_command` workspace-escape check, purely lexical, needs the literal spelling) and `tools._paths` (`resolve_within`, compares against `Path.resolve()`'s already-symlink-resolved form) — so the two independently-gated codepaths agree without importing each other (doc/SECURITY_RULES_PLAN.md). |

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
| [_ws.py](../src/kodo/transport/_ws.py) | `WebSocketDispatcher`, `HandlerFn`, `APP_STATE_KEY`, `get_state()` | **Superseded** by `_connection.py` (`Connection`/`SessionChannel`) + `kodo.server.ConnectionRegistry`/`SessionManager`, the live multi-session production path — not wired into `_app.py`. Kept only for its own test coverage. Single-connection model: composes one `Outbox`; two dispatch paths: client `kind=request` → registered `HandlerFn` by `payload.type`; client `kind=response` → resolves an `asyncio.Future` by `correlation_id`. On disconnect it cancels all pending futures — the live path deliberately does not (SECURITY.md §7b). |

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
input_schema, output_schema, security_impact,
input_visibility, output_visibility, autonomous_mode: str | None = None,
requires_project: bool = False
```

Only `name`, `description`, and `input_schema` are visible to the model — an LLM
tool definition has no other fields. `description` is therefore the single prose
channel and must also carry the tool's **when-to-use** guidance;
`tool_description()` (below) appends the dense `output_schema` sketch to it.
`external_name` and `security_impact` are UI/engine-facing only, and
`autonomous_mode` containing `"unavailable"` drives per-mode tool filtering.

[\_\_init\_\_.py](../src/kodo/toolspecs/__init__.py) exposes one catalog:

- **`ALL_TOOLS: tuple[ToolSpec, ...]`** — all specs (tool names are unique),
  including the terminal `return_result` every sub-agent uses to return its
  typed result (§11).
  Consumed by `subagents/_registry` to *validate* each agent's `tools:`
  frontmatter at load time. (Which of these specs are
  actually *dispatchable* is a `tools/` concern — see
  `tools.DISPATCHABLE_TOOLS_BY_NAME`, §6A.)
- **`tool_description(spec) -> str`** ([_describe.py](../src/kodo/toolspecs/_describe.py))
  — the description actually sent to the model: the spec's prose plus
  `Returns: {…}`, a **dense** rendering of `output_schema` in which every
  property collapses to its `description` string (no `type`/`properties`/
  `required` scaffolding), followed by a one-line note naming the fields that
  may be absent. Types are omitted deliberately — they are self-evident in the
  returned data — and the engine-owned `schema_compliance` field is excluded, so
  its long explanation is not repeated under all ~30 tools. Called by
  `ClaudePlugin`, `LlamaPlugin`, and `LoggingLLMPlugin` so all three send and log
  byte-identical tool definitions. `dense_output_schema()` /
  `optional_output_paths()` are exposed for tests and tooling.

[_ask_user.py](../src/kodo/toolspecs/_ask_user.py) (`ASK_USER`) carries
`autonomous_mode="unavailable …"`. It takes a **question batch** — `questions:
[{question, kind: single_choice|multi_choice, options: [str, …]}]`, the agent's
top-choice answer always first, the client appending the free-text option
itself — and returns `answers: [{selected: [str], free_text: str|null}]` in
question order. The batching discipline (think first, one call per topic,
derived candidate answers, best assumption first) lives in the spec's own
`description`. It used to be an "Asking the User Questions" section of
a shared block sent to *every* agent — ~380 words about a tool only
four of them hold (`guide`, `problem_solver`, `narrative_author`,
`toolchain_builder`), duplicating half of what the spec already said and
cross-referencing the other half. On the spec it is self-gating: it reaches
exactly the holders, and there is one copy. (`ask_user` was once split into a
leaf spec and a separate guide spec; they were collapsed into one — the
runtime contract was identical and the guide-only guidance already lives in
the guide prompt body.)

**Implementation state of the specs:** every spec in the catalog now has a
matching dispatch handler in `tools/` (§6A); there are no spec-only
placeholders.

| Spec | Role |
|---|---|
| `read_file` | Read a file whole, by one or more 1-based line ranges, or by regex `pattern` (ripgrep-backed, with `context_before`/`context_after`). The general-purpose read tool — granted to authors and critics alike. |
| `guided_dev_status` | Scans `.kodo/guided_dev_state/` and reports every tracked document's status, derived from its log's last entry. The replacement for the old artifact-index `query_frontier`. Guided-mode only; the handler errors if called from any other workflow mode. |
| `ask_user` | ✅ implemented. (`escalate_blocker` was **deleted** 2026-07-30: a blocked author now escalates through its own `return_result` — a non-empty `reason` + the blocking `summary` + `options` — see TOOLS.md §5A.) |
| `filesystem`/`edit_file`/`create_file`/`create_directory`/`run_command` | ✅ implemented; granted to authoring sub-agents and the `problem_solver` agent. `filesystem` is **one tool** whose mandatory `operation` field selects among six delete/copy/move ops — `delete_file`/`delete_dir`/`copy_file`/`copy_dir`/`move_file`/`move_dir` (dir ops are recursive: `copytree`/`rmtree`; `copy_dir`/`move_dir` fail if the destination exists). `create_file`, `create_directory`, and `edit_file` stay separate tools: `create_file` writes `content` verbatim at `path` and fails if a file is already there (never overwrites); `create_directory` creates a directory including any missing parents (`mkdir -p`) and succeeds if it already exists — split out of `filesystem`'s former `create_dir` operation so a purely additive, LOW-impact action doesn't share `filesystem`'s HIGH security posture; `edit_file` is a **targeted string-match edit** of an existing file (`old_string` → `new_string`; must match exactly and uniquely or it fails without writing), the **preferred** way to change a file's contents; pass the whole new content as `new_string` to regenerate a file end to end. These five are exactly `runtime/_engine/_checkpointing.py:_MUTATING_TOOLS` — the engine checkpoints around every call to them in **both** workflow modes (§12.1) and each one's `output_schema` carries an **optional `checkpoint_sha`** field the engine fills in when a commit happened. `filesystem`/`edit_file`/`create_file`/`create_directory` calls additionally earn a `new_revision` entry in a tracked document's `.jsonl` log (§7) when the checkpoint commit landed under `specs/`/`src/`/`test/`. |
| `get_root_paths`, `find_files`, `find_text_in_files` | ✅ implemented (workspace search). `get_root_paths` returns the mode-aware root list (bound project in Guided; every workspace folder in Problem Solver) from `ToolContext.root_paths` — unlike almost every other native tool it does **not** set `requires_project` (2026-07-21b): with no workspace bound yet it just returns an empty `roots` list rather than `NO_PROJECT_ERROR`, itself the signal to call `scaffold_new_project` first (`temporary: true` never needed a project either, keyed only by session id). `find_files`/`find_text_in_files` resolve `root` through the active resolver then shell out to the bundled `fd`/`rg` (§10a) via `ToolContext.util_paths`. Granted to `guide` + `problem_solver` + the shared `investigator` + the Problem Solver's `developer` and (since 2026-08-02) `planner`. |
| `web_search` | ✅ implemented (`WebSearchTool`, doc/WEB_SEARCH.md) — a thin wrapper over the **`web_search` agent** (medium capability): validates/clamps `query`/`max_results`/`timeout` (≤600s) and delegates to `EngineServices.run_web_search_agent`, which drives the agent through a new silent, multi-round, non-subsession tool loop (`_run_silent_tool_loop_turn`). The agent itself plans discovery (`query_search_engine`, one engine per call), reads pages (`read_webpage`), paces itself (`get_web_search_state`/`update_web_search_state`/`wait`), watches its own clock (`remaining_time`), and returns `{themes, note}` via `return_result` — replacing the old deterministic discover-all-four-in-parallel → scrape → silent-`web_summarizer`-synthesis pipeline (and its 30-min-per-engine `CooldownStore`) entirely. `max_results` caps the theme count (default 5, max 10). Granted only to the shared `investigator` sub-agent (spawnable by both entry agents). Security impact `MODERATE`; available in autonomous mode. |
| `query_search_engine` | ✅ implemented (`QuerySearchEngineTool`, doc/WEB_SEARCH.md) — the `web_search` agent's discovery primitive: query one of Google/Bing/DuckDuckGo(HTML)/English-Wikipedia(full-text) and return its organic hits (ads/engine-internal links skipped), one engine per call. `browser` picks the fetch backend (`firefox` default, `chrome`/`edge`/`webkit`/`chromium`, or `curl` — `curl_cffi` TLS/HTTP2 fingerprint impersonation, no browser process, backed by a from-scratch `selectolax` port of the per-engine extraction logic for the pages with no live DOM to evaluate). A wall is a compliant `{"error": ...}`, distinct from a legitimate empty `hits` list. Security impact `LOW`; available in autonomous mode. |
| `read_webpage` | ✅ implemented (`ReadWebpageTool`, doc/READ_WEBPAGE.md) — fetch **one caller-given URL** and return its `content`, shaped by `content_filter` (`off`/`html`/`text`, default `text` — the tool's original Markdown-conversion behavior, content-root selected and chrome-stripped). `browser` picks the fetch backend, same choices as `query_search_engine`; `BrowserSession` (`kodo/websearch/_browser.py`) launches exactly the requested kind with **no cascade** — errors immediately if unavailable, since the caller chose deliberately. Since the URL comes straight from the agent, it's SSRF-guarded: non-http(s) schemes and hosts resolving to a private/loopback/link-local/reserved address raise before any request is made. A captcha/anti-bot wall, an HTTP 403/429/503, or (in `text` mode) too-thin residual content raises the same way and the tool returns `{"error": "..."}` advising against retrying the same URL with the same browser — **no cooldown state**. Granted only to the shared `investigator` sub-agent and the `web_search` agent. Security impact `LOW`; available in autonomous mode. |
| `use_skill` | ✅ implemented (`UseSkillTool`, doc/SKILLS.md) — returns one user-installed skill's full `SKILL.md` body plus its absolute directory path, the load half of a progressive-disclosure pair whose other half is the `{SKILLS}` catalog `AgentRegistry` expands into the prompt of every agent granted this tool. `~/.kodo/skills` is re-scanned per call (no cache), so a skill installed or deleted mid-session takes effect on the next turn. The grant *is* the opt-in — `__validate_skills` rejects the tool without the token and the token without the tool — and it is held today by `problem_solver` plus every sub-agent that itself writes code or documents (`coder`, `architect`, `developer`, `e2e_test_coder`, `e2e_test_designer`, `functional_designer`, `narrative_author`, `requirements_author`, `test_coder`, `test_designer`); critics, `judge`, toolchain agents, and read-only/investigative agents opt out (doc/SKILLS.md §7). Security impact `MINIMAL`; available in autonomous mode; grants nothing beyond the text it returns. |
| `get_web_search_state` / `update_web_search_state` | ✅ implemented — the `web_search` agent's persistent key-value pacing memory (`kodo.websearch.WebSearchStateStore`, `~/.kodo/websearch/agent_state.json`, 12h TTL per entry refreshed on write). `update_web_search_state`'s special `<time_mark>` value records `time.time()` under a key instead of a literal string; reading it back returns the elapsed seconds, recomputed fresh every call. Exclusive to the `web_search` agent by convention. Security impact `NONE`; available in autonomous mode. |
| `wait` / `remaining_time` | ✅ implemented — the `web_search` agent's anti-burst pacing lever (a clamped sleep, ≤30s/call, never sleeping past `ToolContext.deadline`) and timeout countdown (seconds left before the run's deadline, set from the tool's `timeout`). Exclusive to the `web_search` agent by convention. Security impact `NONE`; available in autonomous mode. |
| `run_subagent`, `rollback`, `finalize_project` | ✅ implemented. `run_subagent` is never offered as-is: each caller gets one `run_subagent_<name>` tool per invocable sub-agent, carrying that sub-agent's own `input_schema`, and the engine folds such a call back to the canonical form before dispatch (doc/TOOLS.md §5A). When the target declares a `critic:`, one call runs the whole author/critic loop. `rollback` now delegates to the same shadow-git mirror Problem Solver uses (§7/§10b). |
| `disable_autonomous_mode` | ✅ implemented (`DisableAutonomousModeTool`, in `_TOOL_CLASSES`). Declared by `guide`; resolved by `tools_for_agent` and dispatched. (Progress reporting is no longer a tool — agents emit `<kodo_info>` callouts in their message text; see `shared_callouts.md`, which only the two entry agents include.) |
| `scaffold_new_project` | ✅ implemented (`ScaffoldNewProjectTool`). Granted to `guide` + `problem_solver`. Merges the former `create_new_project`/`init_project` tools into one; a thin shim that dispatches on the agent's input to `_EngineServices.create_project(name)` / `.bootstrap_project(name)` / `.init_project(path)` (unchanged engine primitives): **no `path`, no workspace bound yet** → `bootstrap_project` — resolves a workspace-home folder (interactive folder-picker dialog, or under `~/kodo-projects/<name>` in autonomous mode) regardless of whether `name` was given, then delegates to `create_project`; **no `path`, workspace already bound** → `create_project`, requiring a non-empty `name` — the engine slugifies it, makes a fresh directory under the session workspace root (auto-suffix `-2`/`-3`… on collision), scaffolds `.kodo/`+`kodo.md`+checkpoint mirror via `RootMirrorManager.prepare`, records it in the logical-root map, and pushes `EVT_WORKSPACE_ADD_FOLDER` so the extension adds it to the open workspace (WS_PROTOCOL §5.9c); **`path` given** → `init_project` — *path* must already exist (`ProjectLayout.init_existing` raises `ProjectLayoutError` otherwise), and the directory is judged empty when it holds no entries besides dotfiles/dot-directories (`.git/`, `.gitignore`, ...), in which case — and only then — `specs/`/`src/`/`test/` are laid out, exactly like `create_project`; either way `.kodo/`+`kodo.md`+checkpoint mirror are scaffolded via the same `RootMirrorManager.prepare` (mandatory baseline commit before the call returns), and `EVT_WORKSPACE_ADD_FOLDER` is pushed only when *path* isn't already one of the session's registered workspace folders; **`path` given and it already has a `.kodo/`** (already a Kodo project) → `ProjectLayout.init_existing` no longer raises — it returns `(scaffolded=False, already_scaffolded=True)`, a no-op success (nothing on disk touched, but the directory is still idempotently registered/locked/mirror-prepared), and the tool reports `already_scaffolded: true` instead of erroring. In every no-`path` branch an absolute path is never LLM-suppliable — only ever the engine's own bootstrap placement or a real user action (the native "Create Project" folder-picker) — closing the path-injection surface for *creation*; `path` is only ever used to point at something that must already exist. With no `path` **and** no workspace bound yet, `ToolDispatcher` also skips `__security_gate` entirely for this call (doc/SECURITY.md §4) — there is no agent-chosen location for it to judge; a `path`-driven call goes through the gate normally even with no workspace bound, since `path` *is* an agent-chosen location. |
| `toolchain_build`/`toolchain_deps` | ✅ implemented. `toolchain_build` executes a project's generated `scripts/<step>.{sh,ps1}` pair (the toolchain-setup agent's output, §8/§11) in canonical order; its mandatory `project_path` names the project root to build (the dir holding `.kodo/`) — supplied by the caller, since Problem Solver runs have no bound project and any kodo project on disk is buildable (absolute paths as-is, relative paths through the run's resolver) — format → build → static_analysis → test — stopping at the first failure; a missing script returns a clear error directing the caller to run the toolchain-setup agent first. `toolchain_build` is also the validator's `judge` agent's one non-read-only tool (doc/VALIDATOR.md §9.2) — an RVP can ask the judge to call it for real, executed build/test evidence rather than an inferred read-only verdict. `toolchain_deps` performs **one** add/remove/update dependency op: it does not touch manifests itself but spawns the `toolchain_depsmgr` sub-agent (via the dedicated ungated `_EngineServices.run_dependency_manager`, **not** `run_subagent` — holding the tool is the authorization, so the sub-agent is never in any caller's allow-list/roster) which follows the project's `DEPENDENCIES.md`. When that sub-agent reports `status: "dependencies_md_missing"`, the tool returns the same status plus a remediation `message` telling the caller to run the toolchain-setup sub-agent (`toolchain_builder`, which covers every language) first — error-forwarding via the matched tool/sub-agent schemas. |

**Intent:** every **first-degree mutator** (`filesystem`, `edit_file`,
`create_file`, `create_directory`, `run_command`, `scaffold_new_project`,
`rollback`) requires a mandatory `intent`
string — one sentence stating what this specific call changes and why — as the
**first** `input_schema` property, `"always"` visible (the top row of the
tool-call detail box). The property is defined once in `toolspecs/_intent.py`
(`INTENT_PROPERTY`) and embedded per spec; `ToolDispatcher.dispatch` rejects a
call missing a non-blank `intent` before the handler runs (`requires_intent`).
Second-degree mutators (`run_subagent`,
`toolchain_deps`) and `toolchain_build` are exempt. The security layer judges
each SMART-mode HIGH-impact call by its declared intent (allow / ask the user)
— see [doc/SECURITY.md](SECURITY.md) and [TOOLS.md §8A](TOOLS.md).

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
| [_context.py](../src/kodo/tools/_context.py) | `ToolContext`, `RootPath`, `GateLike`, `SessionLike`, `EngineServices`, `QuestionLike`, `ApprovalLike` | The injected per-run context (collaborators + mutable `stop_requested`/`returned_output`) and the structural Protocols runtime satisfies. `EngineServices` is one protocol covering every engine-side operation a tool can trigger (sub-agent launch, **dependency-manager launch** (`run_dependency_manager`, the ungated `toolchain_depsmgr` spawn behind `toolchain_deps`), author/critic iteration, rollback, mode disable, project creation). `runtime.GateOrchestrator`/`SessionState` and the engine's `_EngineServices` adapter match them by shape. The mode a tool honours is read live from `SessionLike.effective_autonomous` (frozen per prompt), never snapshotted onto the context. Also carries `mode: str` (`"guided"`/`"problem_solving"`/`"judge"`, frozen per prompt — gates `guided_dev_status` and tags `new_revision` jsonl entries; every non-`"guided"` value is treated alike, so `"judge"` needs no extra branching here) and `root_paths: tuple[RootPath, ...]` (every bound root, mode-agnostic — there is no singular `project_root` any more; a caller that needs to know which bound root a specific resolved path falls under uses `kodo.tools.root_for(root_paths, path)`, see §7) plus `util_paths: dict[str, Path]` for the search tools. |
| [_tool.py](../src/kodo/tools/_tool.py) | `Tool` (ABC) | Binds one run's `ToolContext` (read-only `context` property) and declares the abstract `handle(self, tool_input) -> str`. |
| `_<tool_name>.py` (one module per dispatchable tool) | one `Tool` subclass each | e.g. `ReadFileTool`, `DocumentFeedbackTool`, `GetRootPathsTool`; implements `handle` reading `self.context`. Mirrors the `toolspecs` one-file-per-tool convention. |
| [_dispatch.py](../src/kodo/tools/_dispatch.py) | `ToolDispatcher`, `tools_for_agent`, `DISPATCHABLE_TOOLS_BY_NAME` | The `_TOOL_CLASSES` table pairs each dispatchable `ToolSpec` with its `Tool` subclass; `dispatch` instantiates the class bound to the run's context and calls `handle`; exposes per-run `stop_requested`/`returned_output`. `tools_for_agent(frozenset[str])` resolves an agent's declared names to specs (skipping spec-only placeholders — none today). |
| [_paths.py](../src/kodo/tools/_paths.py) | `resolve_within`, `resolve_logical`, `LogicalPathResolver`, `root_for` | Logical-workspace path resolution (`LogicalPathResolver`/`resolve_logical`, shared by both workflow modes since the 2026-07-24 multi-project rework — there is no separate project-confined resolver any more), the standalone `resolve_within` used only by `Tool.resolve_path`'s `temporary=True` scratch-directory confinement, and `root_for` (longest-matching-root lookup, "which bound root does this resolved path belong to"). |
| [_search.py](../src/kodo/tools/_search.py) | `run_util`, `UtilTimeout` | Shared subprocess launcher for `find_files`/`find_text_in_files`/`read_file`'s pattern mode and `toolchain_build`'s script execution: runs the util with stdin closed under a bounded timeout, killing the whole process tree on POSIX. Holds no tool dispatch. |

**Links:** `runtime/_engine/` builds one `ToolDispatcher` per agent run via
`_make_dispatcher`, injecting `GateOrchestrator`, `SessionState`, and one
`_EngineServices` adapter (wrapping the engine's `_run_subagent` /
`_run_dependency_manager` / `_run_rollback` /
`_disable_autonomous`). The dispatcher takes **no**
`autonomous` flag — tools read `SessionState.effective_autonomous`, which the
worker freezes once per prompt, so a mid-prompt mode toggle never rebuilds the
dispatcher or splits the prompt's mode. Autonomous filtering of `ask_user`
happens once, in `subagents/_registry`. `_make_dispatcher` also passes `mode`,
`root_paths` (computed mode-agnostically from `SessionWorkspace.folders` — synced
by the extension's `workspace.folders` frames — or, when locked and
disconnected, the bound-directories fallback; see WS_PROTOCOL.md §7.1c) and
`util_paths` (resolved from `binutils.find_util(kodo_user_dir(), "fd"/"ripgrep")`).

**State:** Complete.

---

## 7. `guided_state/` — per-document evolution log (replaces the artifact workspace)

**Authors and critics work directly on real files** under `specs/`/`src/`/
`test/` via the native `filesystem`/`edit_file`/`create_file`/`create_directory`/`read_file` tools — there is no
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
are tracked by git" split the design requires. `<root>` here is always **one
specific bound root**, never the workspace/session root — a session may have
several bound projects (WS_PROTOCOL.md §7.1c), each with its own independent
`.kodo/guided_dev_state/` tree; every caller resolves the agent-supplied
folder-prefixed logical path down to a real absolute path and the specific
bound root it falls under (`kodo.tools.root_for`) *before* calling into this
package, which itself never changed — it always took an explicit `project_root`
argument, one call per document, regardless of how many roots exist upstream.

| Module | Defines | Role |
|---|---|---|
| [_paths.py](../src/kodo/guided_state/_paths.py) | `shadow_path()`, `is_tracked()` | The real-path ↔ `.jsonl`-path mapping above. |
| [_records.py](../src/kodo/guided_state/_records.py) | `Status`, `new_revision_entry()`, `review_result_entry()`, `accepted_entry()`, `derive_status()`, `last_revision_timestamp()` | The three entry-type constructors (pure dict builders) and the status-derivation rule. `derive_status` takes the *findings* half (`reviewed`, `outstanding`) as plain arguments, so this package keeps importing nothing — see §7a. |
| [_store.py](../src/kodo/guided_state/_store.py) | `append_new_revision()`, `append_review_result()`, `append_accepted()`, `read_history()`, `read_document_state()`, `read_jsonl()` | Append/read the `.jsonl` log for one document. `append_new_revision` is a no-op outside the tracked roots; the other two raise `ValueError` for an untracked path (they should never be called for one). `append_accepted` reads the log's most recent `new_revision` to reuse its `commit_hash` — acceptance never produces a new commit. `read_document_state` returns `{last_entry, last_revision_ts, last_event}` and stops short of a status, because deriving one needs the other store. |
| [_scan.py](../src/kodo/guided_state/_scan.py) | `scan_tracked_files()` | Walks `.kodo/guided_dev_state/` and returns `{path, last_entry, last_revision_ts, last_event}` per tracked document — raw inputs, not a status, for the same reason. Backs the `guided_dev_status` tool (§6). |

**The three jsonl entry types** (one append-only line each, see the records
module above for exact fields):

1. **`new_revision`** — engine-written, immediately after a `filesystem`/
   `edit_file`/`create_file`/`create_directory` call's checkpoint commit lands under a tracked root (§12.1).
   Carries the commit `sha`, the agent name, the tool used, and a
   `workflow: "guided"|"problem_solving"` tag — **fired in both workflow
   modes**, so the Guide can reconcile state after a Problem-Solver session
   touched a tracked file. This is the *only* entry type Problem Solver ever
   produces.
2. **`review_result`** — engine-written only, never via a dispatched tool:
   the user's `approve`/`reject` decision from the interactive review gate.
3. **`accepted`** — engine-written only: the final marker, `commit_hash`
   copied from the preceding `new_revision`.

> **There is no `feedback` entry any more.** A critic's verdict used to land
> here as `{accept, concerns}`. Concerns became **findings** — identified,
> stateful, and session-scoped (§7a, doc/FINDINGS.md) — so this project-scoped
> log no longer carries review content at all, and a document's status is a
> function of both stores. A legacy `feedback` line left by an older build is
> ignored rather than interpreted.

**State:** Complete; high test coverage (`test_guided_state.py`,
`test_engine_document_flow.py`).

---

## 7a. `findings/` — the shared author/critic backlog (session-scoped)

A **finding** is one defect a critic raised against one document, with an `id`
that survives the round it was raised in and a `state` of `outstanding` or
`fixed`. Both halves of an author/critic loop read the backlog through the
`get_findings` tool; the **critic alone** writes to it, through its own
`return_result`, and the engine applies the result. **Full design:
[doc/FINDINGS.md](FINDINGS.md)** — this section is the module map.

Storage is under the **session** directory
(`~/.kodo/sessions/<id>/findings/<logical document path>.jsonl`), not the
project's `.kodo/`: two sessions may review the same tree under different models
and settings, so a backlog is a fact about a session's review rather than about
the project. The trade-off is explicit — a document reviewed in session A shows
no outstanding findings in session B; what crosses sessions is the document's own
evolution log (§7).

| Module | Defines | Role |
|---|---|---|
| [_records.py](../src/kodo/findings/_records.py) | `Finding`, `RoundSummary`, `FINDING_FIELDS`, `finding_entry()`, `review_round_entry()`, `merge_finding()` | The two entry types and the merge rule. `RoundSummary.stalled` (closed nothing *and* opened nothing) is the loop's no-progress signal. |
| [_paths.py](../src/kodo/findings/_paths.py) | `findings_log_path()` | Logical path → session log path. The logical path is agent-supplied, so segments are validated (traversal/absolute → `None`) and sanitised (the first segment is a workspace-folder display name). |
| [_store.py](../src/kodo/findings/_store.py) | `read_findings()`, `apply_findings()`, `record_user_feedback()`, `last_round_timestamp()`, `outstanding_findings()` | Append/replay one document's log. `apply_findings` mints ids for updates with no `id`, patches the rest, and always closes with a `review_round` line. |

**The two jsonl entry types:**

1. **`finding`** — `{type, timestamp, id, reported_by, …changed fields…}`. The
   first line for an id *creates*; every later line for it *patches*. Fields
   absent from a line are unchanged — the "omitted fields remain the same" rule
   is enforced by the storage layer, not by the engine remembering to preserve
   them. A finding the round never mentions is untouched: **silence closes
   nothing.**
2. **`review_round`** — `{type, timestamp, reviewer, outstanding, opened, closed}`,
   one per completed critic round, written whether or not anything moved.

**Status is a two-store merge.** `kodo.tools.document_status()`
([tools/_document_status.py](../src/kodo/tools/_document_status.py)) is the only
implementation of the rule, used by both `guided_dev_status` and the engine's
review loop; `guided_state.derive_status` takes the findings half as plain
`reviewed`/`outstanding` arguments so neither leaf package imports the other.
See doc/FINDINGS.md §6 for the table.

**State:** Complete (`test_findings.py`, `test_document_status.py`,
`test_engine_document_flow.py`).

---

## 8. Toolchain setup — generated build scripts (no plugin package)

There is **no `toolchains/` package anymore.** The former `ToolchainPlugin`
ABC + `PythonPlugin`/`NodePlugin` subclasses existed only to (a) decide
`source_filename`/`test_filename` for artifact promotion (§7, now gone — agents
choose their own paths) and (b) implement `build`/`test`/`add_dependency`
directly in Python, which the `toolchain_*` tools never actually dispatched.
Both reasons are gone.

The project's build model instead lives in **agent-generated scripts and docs**: the
single, language-agnostic toolchain-setup sub-agent (`toolchain_builder`, which
carries the five-script contract in its own body and shares `shared_dependencies.md`
with `toolchain_depsmgr`, §11) generates five
per-platform script pairs — `scripts/{build,format,static_analysis,test,full_build}.{sh,ps1}`
— plus two root docs: `DEVELOPMENT.md` (build/check/test how-to) and
`DEPENDENCIES.md` (the machine-followable **dependency contract** —
manager, kinds, and command-level add/remove/update steps). The
`toolchain_build` tool (§6, `tools/_toolchain_build.py`) is a thin,
language-agnostic executor: its mandatory `project_path` names the project
root to build (the directory holding the project's `.kodo/` dir — the caller
supplies it, since a Problem Solver run has no bound project and any kodo
project on disk is buildable; absolute paths are taken as-is, relative ones
get the run's normal resolver). It runs the enabled steps' scripts in canonical
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
| [_cloud_registry.py](../src/kodo/llms/_cloud_registry.py) | `CloudLLMEntry` (frozen), `get_cloud_registry()`, `get_cloud_entry()`, `get_cloud_vendor_module()` | Hardcoded two-tier vendor→model tree (Anthropic today). See LLM_REGISTRY.md §3. |
| [_local_registry.py](../src/kodo/llms/_local_registry.py) | `LocalLLMEntry` (frozen), `get_local_registry()`, `add_local_entry()`, `remove_local_entry()`, llama-server override getters/setters | Hardcoded GGUFs merged with the external `~/.kodo/etc/local-llm-registry.json` collection (4 entry kinds — see LLM_REGISTRY.md §4). No `residence` field; every entry here is local. |
| [_context.py](../src/kodo/llms/_context.py) | `get_context_window()` | Cross-registry lookup — checks cloud (`model_id`) then local (`name`) so callers with just a resolved key don't need to know which registry it came from. |
| [_logger.py](../src/kodo/llms/_logger.py) | `LoggingLLMPlugin(LLMPlugin)` | **Decorator** wrapping any `LLMPlugin`; writes `NNNN_request.json`/`NNNN_response.json`. Process-wide counter. |
| [_tool_logger.py](../src/kodo/llms/_tool_logger.py) | `ToolCallLogger` | Writes per-tool invocation/result JSON; turn counter. Used by the engine, not a plugin. |
| [_sanitize.py](../src/kodo/llms/_sanitize.py) | `strip_kodo_callouts` | Regex-strips `<kodo_info>`/`<kodo_warn>`/`<kodo_crit>`/`<kodo>` callout tags (incl. their content) from assistant text. These tags are a one-way notification to the human user (§ `shared_callouts.md`), so their content is never replayed back into the model's own context. Called only by the wire-format builders below and by `runtime/_engine/`'s `render_transcript` (compaction input) — never by anything that persists or renders history, so `session.jsonl`/the WebView still see the tags verbatim. |
| [anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | `ClaudePlugin(LLMPlugin)`, `UnrecoverableError` | **Subclasses** ABC. Uses `anthropic.AsyncAnthropic`; composes `_cache` (breakpoints) + `_retry` (`with_retry_iter`). Enables extended thinking on every call (`thinking={"type": "enabled", "budget_tokens": 4096}`); yields `ThinkingDelta` from the SDK's raw thinking delta and `ThinkingSignature` from its `signature_delta`. Cancellation via per-`stream_id` `asyncio.Event`. |
| [anthropic/_cache.py](../src/kodo/llms/anthropic/_cache.py) | `build_system_blocks`, `build_message_params`, `_drop_unsigned_thinking`, `_strip_callout_text` | Prompt-cache breakpoint construction. `_drop_unsigned_thinking` strips any persisted `"thinking"` block lacking a `signature` (e.g. one originated by llama.cpp in a mixed-provider session) before it reaches Claude, which rejects unsigned thinking blocks. `_strip_callout_text` runs `_sanitize.strip_kodo_callouts` over every assistant `"text"` block (and bare string content) before it is sent. |
| [anthropic/_retry.py](../src/kodo/llms/anthropic/_retry.py) | `with_retry`, `with_retry_iter`, `UnrecoverableError`, `RetryExhaustedError` | Exponential backoff (2/8/32s); classifies auth/billing as unrecoverable. |
| [anthropic/_usage.py](../src/kodo/llms/anthropic/_usage.py) | `compute_cost` | Per-model USD pricing table. |
| [llamacpp/_llama.py](../src/kodo/llms/llamacpp/_llama.py) | `LlamaPlugin(LLMPlugin)`, `ThinkingStreamParser` | **Subclasses** ABC. OpenAI-compatible client against `llama-server`; converts Anthropic-style content blocks ↔ OpenAI chat messages; parses `<think>` tags into `ThinkingDelta`. `_expand_assistant` re-wraps any persisted `"thinking"` content block (this provider's or a Claude-origin signed one) back into `<think>...</think>` text, dropping any signature, since llama.cpp has no use for it; it also runs assistant `"text"` blocks (and `_expand_message`'s bare string case) through `strip_kodo_callouts`. **Composes** `MessageSink` (to emit `EVT_LLAMA_STATE`) and calls its sibling `_manager.ensure_llama_running`. |

**Links:** Every plugin is wrapped in `LoggingLLMPlugin` by the engine's
`_resolve_plugin`. `LlamaPlugin` reaches *up* into `transport` (for state
events) and *sideways* into its sibling local-inference utilities (§10).

**State:** Complete for both providers.

---

## 9a. `__main__.py` — CLI diagnostics entry point

| Module | Defines | Links |
|---|---|---|
| [__main__.py](../src/kodo/__main__.py) | `main()` — `python -m kodo` / the packaged `kodo` console script (`pyproject.toml`'s `[project.scripts]`) | Diagnostic CLI, two mutually exclusive commands, each taking a single `AGENT` name: `--system-prompt`/`-p` and `--tools`. `--system-prompt` prints the exact system prompt kodo would send, by calling `AgentRegistry.get(agent, autonomous=False)` — the real render path, not a reimplementation — so every `{SHARED:…}` block is substituted; tools are *not* in the prompt at all (TOOLS.md §7). `--tools` prints the agent's `tools=[...]` payload exactly as submitted to the OpenAI-compatible client, via `kodo.tools.tools_for_agent(agent.tools)` → `kodo.llms.llamacpp.build_openai_tools()` — the same function `LlamaPlugin.__raw_stream` calls to talk to `llama-server`, factored out so the CLI reproduces production output byte for byte rather than a hand-built copy of the wire shape. A separate `--model`/`-m LLM_ID` (local registry `name` or cloud `model_id`, checked in that order) selects the model for either command; when omitted, `_first_installed_local_model` picks the first installed entry in the local registry (same "installed" definition as `kodo/server/_app.py`'s `_local_entry_installed`), erroring if nothing is installed. `--model` must resolve but changes nothing today for either command: no plugin appends model-specific text to the prompt, and the OpenAI tools shape is the one `LlamaPlugin` builds regardless of which vendor `LLM_ID` resolves to. It exists for planned per-LLM variation. Unknown agent or model → exit 2; the agent is resolved first. No `--autonomous` flag by design. Tests: `test/test_main.py`. |

---

## 10. `llms/llamacpp/` — local inference lifecycle (merged from `llm_utils`)

These modules were a standalone top-level `llm_utils` package that formed an
import cycle with `llms`; they were moved under `llms/llamacpp/` (only llama.cpp
inference uses them) and are re-exported from `kodo.llms.llamacpp`. They are
imported by `LlamaPlugin` (siblings) and by `server/_app.py` (install/start/stop
handlers) — via `kodo.llms.llamacpp`, never from the private modules.

| Module | Defines | Links |
|---|---|---|
| [_installer.py](../src/kodo/llms/llamacpp/_installer.py) | `LlamaInstall`, `install/uninstall/update_llamacpp`, `check_llamacpp_update`, `build_exists`, `fetch_latest_build_number`, `find_installed`, `server_executable` | Platform-aware llama.cpp binary install into `~/.kodo/llama.cpp/bN/`. `fetch_latest_build_number` handles two shapes of `GET /releases/latest` from `ggml-org/llama.cpp`: the common case is a rolling `bNNNN` tag parsed directly; ggml-org also occasionally publishes a semver "stable" release (e.g. `v0.2.0`, see <https://github.com/ggml-org/ggml/discussions/1579>) that carries no platform binaries of its own — only a `nightly-tag.txt` asset whose content is the real `bNNNN` tag to resolve and use instead. A tag matching neither shape, or a semver release with no `nightly-tag.txt` asset, raises `RuntimeError`. `install_llamacpp`/`update_llamacpp` take an optional `version` (a build number) to pin an explicit release instead of latest. `build_exists` HEAD-probes a given build's release assets — `server/_app.py`'s `llamacpp.update` handler calls it for a pinned `version` *before* touching the current build, so a nonexistent build number fails without touching the existing install. **`update_llamacpp` installs first and deletes the superseded build afterwards** (each build has its own `bN` directory, so the new one never needs the old one's space): a failed download leaves the previous build working, `llama-meta.json` only moves once the new build has passed `--version`, and the delete becomes pure cleanup whose failure costs a log line rather than the update. **It never reinstalls a build in place** — targeting the installed build falls through `install_llamacpp`'s already-installed early return with no superseded directory to clean up, so nothing is deleted and nothing re-downloaded; the `llamacpp.update` handler short-circuits that case earlier still (§10c). A genuine reinstall is only ever the user's explicit Uninstall-then-Install pair. Directory removal goes through `_rmtree_retrying`, which retries with backoff and clears read-only bits — Windows releases a stopped process's mapped image handles asynchronously, so a delete issued right after stopping llama-server can still hit a sharing violation for a moment. `find_installed` verifies the executable named by `llama-meta.json` still exists and reports "not installed" when it doesn't, so a half-deleted build self-heals via a plain Install instead of stranding every caller on a build that cannot run. No `kodo` imports. |
| [_llama_server.py](../src/kodo/llms/llamacpp/_llama_server.py) | `LlamaServer`, `LlamaServerConfig`, `RunningServer`, `find_running_server` | PID-managed `llama-server` subprocess; class-level singleton via `get_active_llama_server()`; `adopt()` reclaims a survivor after restart. |
| [_manager.py](../src/kodo/llms/llamacpp/_manager.py) | `ensure_llama_running`, `get_local_model_manager` | Composes installer + `kodo.llms.local.LocalModelManager` + server: ensures the right model server is up for a `LocalLLMEntry` (not valid for `custom_server_url` — see LLM_REGISTRY.md §4), honoring the llama-server binary override if set. `get_local_model_manager` resolves the models directory and caches one `LocalModelManager` per directory for the process lifetime; also called directly from `server/_app.py`'s `local_llm.*` WS handlers (no more `_downloader.py` adapter — see LOCAL_MODEL_MANAGER.md §9). |

**Links:** Consumed by `llms/llamacpp/_llama.py` (runtime) and `server/_app.py`
(install/start/stop handlers). Self-contained otherwise. Also consumed by
`kodo.titling` (§10c) — but only `find_installed` (to locate the shared
llama.cpp binary) and `kodo.llms.local.LocalModelManager` (to download its own
GGUF), never `LlamaServer`/`get_active_llama_server()`: those track the *one*
running server for the main chat model as a class-level singleton, and the
titler runs its own, separate llama-server process concurrently — reusing
`LlamaServer` for it would silently steal that singleton slot out from under
the chat model. `kodo.titling._server.TitlerServer` is therefore a small,
self-contained duplicate of `LlamaServer`'s spawn/health-check/stop plumbing,
not a subclass or a second consumer of the same class.

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
| [shellparser/_parser.py](../src/kodo/shellparser/_parser.py) | `parse_command(str) → ParsedCommand`, `ParsedCommand`/`Segment`/`Redirection` (frozen) | **Parse-only, judgement-free** — splits a shell command line into pipeline `Segment`s (on `\| \|\| && ; &`) via `shlex`, each with its `executable`/`args`/`redirections`; never raises (falls back to a naive split on malformed input). It does **not** decide whether a command mutates the filesystem — that heuristic is caller-side (`runtime/_checkpoints.py:command_may_mutate`, below) by design, so the parser stays reusable by other callers — the security layer (`kodo.security._analysis`) applies its own workspace-target classification over the same parse — without inheriting checkpoint-specific judgment calls. `parse_powershell_command` (`_powershell.py`) is the PowerShell/Windows dialect producing the same dataclasses (doc/SECURITY.md §5). |

**State:** Complete; covered by `test/test_shadow_mirror.py` and `test/test_shellparser.py`.

---

## 10c. `titling/` — dedicated-llama-server session-title summarizer (+ greeter)

Names a session from its first prompt via a guardrailed chat-completion call
to a small, **dedicated** llama-server — its own process, on its own fixed
port (8043), running concurrently with (and completely independent of)
whatever model the main chat session is using. *Which* small model it runs
is user-selectable (added 2026-08-09) — a "housekeeper LLM" catalog,
`kodo.titling.HOUSEKEEPER_LLM_OPTIONS` (a `dict[str, HousekeeperLlmOption]`
keyed by `model_id`, each entry a HuggingFace `repo_id`/`filename` to
download plus a customer-facing `display_name`/`description`), defaulting to
`DEFAULT_HOUSEKEEPER_LLM_ID` (`"qwen35-4b-titler"`, Qwen3.5 4B). The Kōdo
Settings panel's "General" section lists every catalog entry as a radio
button (`housekeeper_llm.get`/`.set`, WS_PROTOCOL.md §7.6f, doc/SETTINGS.md
§2.7) — picking a different one persists the choice and silently restarts
the titler's `llama-server` on the newly selected model.

This whole dedicated-llama-server design replaced (2026-07-18) the previous
design: an in-process `transformers`/`torch` encoder-decoder
(`Falconsai/text_summarization`, a tiny extractive T5) called directly via
`AutoModelForSeq2SeqLM.generate()`. That model's ceiling was extractive-only
("Implement A Game Of Tic Tac Toe Where" — a clipped echo, never a crafted
label); a real instruction-tuned chat model produces meaningfully better
titles, and running it through the same llama.cpp binary already used for the
main chat model drops the `torch`/`transformers` dependency (and their large
transitive closure — `numpy`, `tokenizers`, `sympy`, …) from the project
entirely — see `pyproject.toml`'s trimmed `dependencies` list.

Before this replaced the sub-agent-based `session_titler` (a full turn
through the main chat model's `LLMGateway`, 10-15s) — still true here: this
module's whole point is to keep titling off that critical path. Generation
is genuinely async I/O now (an HTTP chat completion, not a CPU-bound
`torch` forward pass), so `SessionTitler` awaits `generate_title` directly
rather than via `asyncio.to_thread` (§12).

Two independent capabilities ride the same server, same process, same port —
neither is tied to titling per se, they just reuse whatever `start_titling`
already brought up: `generate_project_name` (a short project name from a
description) and `generate_greeting` (added 2026-08-01 — a short, varied
opening greeting for a brand-new session, `runtime._engine._greeting.
SessionGreeter`, WS_PROTOCOL.md §5.9i; replaces kodo-vsix's own previously-
hardcoded empty-state placeholder). Unlike `generate_title`/
`generate_project_name`, `generate_greeting` takes no input text — a theme is
picked at random from `_greeting_themes.GREETING_THEMES` (72 entries: mood,
industry, historical invention, an unsolved CS/math problem, a
quantum-mechanics paradox, a corner of the cosmos, ...) on every call, with
`temperature=0.9` (not `0.0`) so consecutive brand-new sessions don't open
with the same line. No
injection-guardrail delimiter framing is needed for it — unlike the other
two, it never takes any untrusted user text as input.

**Tier: T3a** (§2.2) — imports `kodo.llms.llamacpp.find_installed` (locate the
shared llama.cpp binary) and `kodo.llms.local.LocalModelManager` (download its
own GGUF, rooted at its own directory — never the chat-model registry's
`LocalModelManager`/`~/.kodo/llama.cpp/models`), plus `project`
(`kodo_user_dir`). Deliberately does **not** import or reuse
`kodo.llms.llamacpp.LlamaServer`: that class tracks the *one* running server
for the main chat model via a class-level singleton
(`get_active_llama_server()`), consumed throughout `server/_app.py` and
`LlamaPlugin`; instantiating a second one for titling would silently steal
that slot. `kodo.titling._server.TitlerServer` is instead a small,
self-contained copy of the same spawn/health-check/stop plumbing (PID file,
`/health` poll, SIGTERM-then-SIGKILL stop), tracked by this module's own
module-level singleton (`_active`) — see `_llama_server.py`'s docstring
cross-reference in §10.

| Module | Defines | Role |
|---|---|---|
| [_server.py](../src/kodo/titling/_server.py) | `HousekeeperLlmOption`, `HOUSEKEEPER_LLM_OPTIONS`, `DEFAULT_HOUSEKEEPER_LLM_ID`, `TitlerServer`, `start_titling`, `stop_titling`, `generate_title`, `generate_project_name`, `generate_greeting`, `titler_home_dir` | `titler_home_dir()` is `~/.kodo/titler` — both the titler's own `LocalModelManager` root (its GGUF cache) *and* its runtime-state file (`llama-server.json`, PID+port+`model_id` — mirrors `_llama_server.py`'s `find_running_server`/`adopt()` pattern so a kodo restart re-adopts a surviving titler process instead of orphaning it or failing to rebind its port; the recorded `model_id` lets a restart tell "survivor already running the requested model" from "survivor running a *different* one" — the latter is terminated rather than adopted). `start_titling(kodo_dir, housekeeper_llm_id=None)` — best-effort and idempotent: no-op if the requested option is already running; resolves an absent/unrecognised id to `DEFAULT_HOUSEKEEPER_LLM_ID` (`_resolve_housekeeper_option`); if a *different* option is currently running **and the requested one is already downloaded**, stops it first, then proceeds the same as a cold start — if llama.cpp isn't installed, logs and returns; checks `LocalModelManager.get_model_path` before downloading anything (so, unlike the old `transformers` design, a cached model is never even re-listed from the Hub, let alone re-downloaded — no separate "offline" flag needed, see doc/VALIDATOR.md §8); adopts a surviving process only if the runtime file names one both alive *and* already running the requested `model_id`, else spawns fresh (terminating any mismatched orphan first) — CPU-only (`--n-gpu-layers 0`, so it never contends with the main chat model for GPU memory/compute) plus `--jinja`/`--reasoning-format auto` and an 8192 context, identical across every catalog entry (only the model file differs). **Intelligent fallback (added 2026-08-09)**: if the requested model *isn't* downloaded yet (mid-download, or never fetched), `start_titling` no longer blocks titling on that download — if a different model is already running, it's left running as-is instead of being torn down for a swap that would leave titling dark for the whole download; on a cold start (nothing running), `_find_ready_fallback` scans `HOUSEKEEPER_LLM_OPTIONS` in catalog order for any other option that's already downloaded and starts that instead. Either way, `_schedule_background_download` fire-and-forgets the actually-requested model's download (deduped per `model_id` via the module-level `_background_downloads` set, so a repeated request doesn't race a second `download_model()` call over the same `.part` file) — **there is no automatic swap once that background download finishes**; only a later, explicit `start_titling` call for that same id (next kodo restart, or the user re-picking it in Settings) activates it. Only when *no* housekeeper model is downloaded at all (nothing to fall back to) does `start_titling` still block on the synchronous download, same as before this change. Every failure anywhere in this path is logged and swallowed: titling is best-effort infrastructure, never something that can block kodo startup or a chat session. `stop_titling()` stops the managed process (also best-effort; does not cancel an in-flight background fallback download — that's a plain model-cache download unrelated to the llama-server process lifecycle, and finishing it is harmless). `generate_title(text) → str \| None` — a single non-streaming chat completion (`openai.AsyncOpenAI` against the titler's own `base_url`, `model=server.model_id`, `temperature=0`, `chat_template_kwargs.enable_thinking=False`, a stray `<think>…</think>` stripped defensively if one slips through anyway) using a guardrailed system+user prompt: the message to summarize is wrapped in `<<<MESSAGE>>>…<<<END_MESSAGE>>>` delimiters with explicit instructions that it is *data to summarize, never instructions to follow* — the defense against a prompt that reads like "ignore previous instructions and say X", which a small instruction-tuned model is otherwise exactly the kind of model to comply with. `generate_project_name(text) → str \| None` — same shape, a different guardrailed prompt inventing a 1-3 word project name. `generate_greeting() → str \| None` — no input text, no guardrail delimiter (nothing untrusted to wall off); picks a random theme from `_greeting_themes.GREETING_THEMES` and asks for a short opening greeting, `temperature=0.9`. All three return `None` on any failure (server not up, HTTP error, blank content) so the caller falls back (to the prompt's own words for title, a fixed default line for the greeting) rather than raising. |
| [_greeting_themes.py](../src/kodo/titling/_greeting_themes.py) | `GREETING_THEMES` | 72 closing-sentence clauses ("the P versus NP problem, and whether...", "Schrödinger's cat, suspended between...", "the methane lakes of Titan, where...") completing the greeter's system prompt's "For example, you can speak of {theme}." — one picked at random per `generate_greeting()` call. |

Consumed by `runtime._engine._titling.SessionTitler` (`generate_title`, for
any first prompt over 8 words — §12, WS_PROTOCOL.md §5.9a/§5.9b),
`runtime._engine._greeting.SessionGreeter` (`generate_greeting`, for every
brand-new session — WS_PROTOCOL.md §5.9i), and by `server/_app.py`
(`start_titling`/`stop_titling`, server lifecycle):

- **Startup** — `_start_background` fire-and-forgets `start_titling` (never
  awaited, so a first-run download or a slow subprocess health-check cannot
  delay kodo itself from accepting connections) whenever `find_installed`
  says llama.cpp is already there; `_stop_background` stops it, mirroring
  the main chat model's own `LlamaServer.stop()` call there. Kicked off
  *before* the `ensure_all_utils` await, not after (added alongside the
  greeter) — the greeting fires from the very first `hello`, so the earlier
  the titler starts loading, the better the odds it's warm by then; still
  only a best-effort head start, which is exactly why `SessionGreeter` falls
  back to a fixed default line rather than assuming this always wins the
  race. Passes `_current_housekeeper_llm_id()` (reads the raw `housekeeper_llm`
  key off settings.json, defaulting like `_valid_housekeeper_llm_id` does) so
  a previously-selected housekeeper LLM survives a kodo restart.
- **Install** — `_handle_llamacpp_install` (`llamacpp.install`) schedules
  `start_titling` after a successful install, same `_current_housekeeper_llm_id()`
  resolution as startup.
- **Update** — `_handle_llamacpp_update` (`llamacpp.update`
  WS command, `server/_app.py`, §14) stops **both** llama-server processes
  that run off the install `update_llamacpp` is about to replace — the
  titler's (`stop_titling`) *and* kodo's own chat server
  (`LlamaServer.get_active_llama_server()`, followed by an `EVT_LLAMA_STATE`
  push so the sidebar's running indicator stays honest) — then calls
  `update_llamacpp`, then schedules `start_titling` again on success, same as
  a fresh install. The chat server is deliberately **not** restarted here:
  `ensure_llama_running` brings it back on the next engine run, against
  whatever model the user has selected by then.

  **Update never reinstalls a build in place**, on either entry path: an
  unpinned request whose installed build is already `>=` latest, and a pinned
  request naming the installed build, both short-circuit to a single
  `percent: 100, up_to_date: true` frame *before* anything is stopped or
  probed (the pinned case is checked ahead of `build_exists` — the build is on
  disk, so whether GitHub still serves it is irrelevant). The only route to a
  genuine reinstall is the user explicitly clicking "Uninstall llama.cpp" and
  then "Install llama.cpp" in the Kōdo Settings panel. A pinned *older* build
  is a deliberate downgrade and proceeds normally.
  `update_llamacpp`/`check_llamacpp_update` (`_installer.py`, §10) existed
  but had no caller anywhere before this; `llamacpp.update` (WS_PROTOCOL.md
  §7.6) is a new, minimal WS command with no kodo-vsix UI yet.

  > **Fixed 2026-08-12 — stopping only the titler here was a Windows-fatal
  > bug.** `update_llamacpp` calls the same `uninstall_llamacpp` the uninstall
  > handler does, and Windows refuses to delete an `.exe`/`.dll` that a live
  > process has mapped as an image, so the delete raised a sharing violation
  > against the still-running chat server *before* `install_llamacpp` emitted
  > its first progress frame. POSIX allows unlinking a running binary, which
  > is why this only ever reproduced on Windows. Three separate defects had to
  > be fixed together; see §10's `update_llamacpp` note for the installer half
  > and the `_stream_llamacpp_progress` paragraph below for the reporting half.

- **Progress streaming** — `_stream_llamacpp_progress` (shared by the install
  and update handlers) **always terminates the stream with a `percent` of 100
  or -1**, synthesizing a `-1` frame if the work raised before reporting one
  itself, and logs the exception. Both halves matter: kodo-vsix's
  `onLlamaProgress` dismisses its progress notification *only* on a terminal
  frame, and the `llamaInstallingState` busy flag it clears there gates every
  retry — so a swallowed failure used to leave the toast and the disabled
  buttons stuck until the window was reloaded, with nothing in the server log
  either. The synthesized frame is suppressed when the work already sent its
  own (`install_llamacpp`'s `_fail` emits `-1` and then raises), so a normal
  install failure still yields exactly one error toast.
- **Housekeeper LLM selection** (added 2026-08-09) — `housekeeper_llm.get`/
  `.set` (WS_PROTOCOL.md §7.6f, doc/SETTINGS.md §2.7) back the Kōdo Settings
  panel's "General" section radio group. `.get` reads the persisted
  `housekeeper_llm` settings.json key (`_valid_housekeeper_llm_id`, same
  defensive-default shape as `stuck_detection`'s own settings key) and the
  full `HOUSEKEEPER_LLM_OPTIONS` catalog shaped for the wire
  (`_housekeeper_llm_options_payload`). `.set` persists the new selection
  (`_persist_housekeeper_llm`, the same raw-file read-modify-write shape as
  `_persist_stuck_detection`) and fire-and-forgets `start_titling(kodo_dir,
  option_id)` — the reply does not wait for the swap, since a first pick of a
  not-yet-downloaded model can take a while; `start_titling` itself handles
  "stop whatever's running if it's a different model, then start the
  requested one" (see the `_server.py` table row above).
- **Uninstall** — `_handle_llamacpp_uninstall` (`llamacpp.uninstall` WS
  command, `server/_app.py`) calls `stop_titling` and stops kodo's own
  chat `LlamaServer` if running (both can be running off the binary files
  `uninstall_llamacpp` is about to delete), then `uninstall_llamacpp` — run
  via `asyncio.to_thread`, since its delete now retries with backoff and
  `ConnectionRegistry.run_ws` awaits handlers one at a time (blocking here
  blocks every other request from that window). Plain request/response, no
  progress stream. A delete that still fails is caught, not raised —
  `__dispatch` has no handler-level `except`, so an escaping exception would
  propagate out of `run_ws` and drop the connection; instead an `EVT_ERROR`
  (`llamacpp_uninstall_failed`, surfaced as a toast by kodo-vsix's
  `control-channel.ts`) goes out and the ack reports what is *actually* on
  disk rather than the requested outcome. Also called internally by
  `update_llamacpp` — but only on the same-build-reinstall path now (see §10)
  — which is why `_handle_llamacpp_update` independently stops/restarts
  titling around its own call rather than relying on the uninstall handler's
  stop (they're never both in the request path at once).
- **Version query** — `_handle_llamacpp_version_info` (`llamacpp.version_info`
  WS command) reports `find_installed`'s build alongside
  `fetch_latest_build_number()` (renamed from the former private
  `_fetch_latest_build_number`, now in `_installer.py`'s public surface) —
  a GitHub-fetch failure is caught and reported via the response's `error`
  field rather than raised, since an unreachable/rate-limited GitHub API
  should degrade the "Kōdo Settings" panel's latest-version display to
  "unknown", not fail the whole request. Both this and `.uninstall` back the
  panel's "Llama.cpp" section (kodo-vsix `kodo-settings-panel.ts`), added
  2026-07-19 alongside `install_llamacpp`/`update_llamacpp`'s new optional
  `version` parameter (WS_PROTOCOL.md §7.6).

**State:** Complete; see `test/test_titling.py`
(`kodo.titling._server`), `test/test_engine_titling.py`
(`SessionTitler`'s 8-word short/LLM fork and fallback behavior), and
`test/test_engine_greeting.py`/`test_engine_history.py` (`SessionGreeter`'s
generate/persist/push/fallback behavior and the `greeting` marker's history
round-trip).

---

## 11. `subagents/` — agent files & prompt rendering

| Module | Defines | Links |
|---|---|---|
| [_loader.py](../src/kodo/subagents/_loader.py) | `SubAgent` (frozen: `name`, `tools: frozenset[str]`, `system_prompt`, `source_path`, `capability`, `display_name`, `subagents`, **`subagent_order: tuple[str, ...]`**, **`purpose`**, **`role`**, **`critic`**, **`standalone: bool`**), `AgentLoadError`, `load_agent()` | Parses `subagent_<name>.md` frontmatter + body. Extracts the **`## Purpose`** body section, which becomes the agent's `run_subagent_<name>` tool description (hence caller-agnostic, third person); reads the `role`/`critic`/`standalone` frontmatter; keeps the `subagents:` allow-list in declaration order as `subagent_order`. No `bases:`/`callouts:` — shared text is included by `{SHARED:<name>}` in the body instead. |
| [_subagentspec.py](../src/kodo/subagents/_subagentspec.py) + [specs/](../src/kodo/subagents/specs/) | `SubAgentSpec` (frozen: `name`, `input_schema`, `output_schema` — **no `description`**; the prose is the agent's `## Purpose`) + one literal per agent in `specs/_<name>.py`, aggregated as `ALL_SUBAGENTS` | The typed input/output contract of a sub-agent — "a tool with agentic behavior". Every sub-agent **except** the entry agents (`guide`, `problem_solver`) has one. `specs/_shapes.py` holds declarative schema builders (`pipeline_input`/`author_output`/`critic_output`). |
| [_registry.py](../src/kodo/subagents/_registry.py) | `AgentRegistry`, `shared_token()`, `SHARED_FILE_PREFIX` | Loads all `subagent_*.md` / `agent_*.md` plus every `shared_*.md` block. **Expands** each agent's `{SHARED:<name>}` tokens in one pass — the only prompt-assembly step there is. **Validates** at construction: `tools:` resolve against `ALL_TOOLS`; every `{SHARED:…}` name exists; `working_rules` + `security` are present; a `modifies_files` grant implies `{SHARED:editing}`; `{SHARED:task_input}` implies a `SubAgentSpec`; a shared file contains no token of its own; every `subagents:` entry resolves and (unless a critic) carries a `## Purpose`. Filters `autonomous_mode == "unavailable"` tools when `autonomous=True`, and **auto-grants `return_result`** to any agent with a `SubAgentSpec` (`SUBAGENT_SPECS_BY_NAME`, `spec_for()`). It does **not** describe tools in the prompt (that is the `tools` argument's job, §6) — including sub-agents: `run_subagent_specs()` builds each callee's tool from that callee's own `## Purpose`. |

**Links:** `_registry` imports `ALL_TOOLS` from `toolspecs`. `get(name,
autonomous)` returns a `SubAgent` whose prompt is its own body with every
`{SHARED:<name>}` token replaced by the contents of `shared_<name>.md`. That is
the entire assembly step — nothing is prepended, nothing is appended.

**One mechanism for shared prompt text.** `{SHARED:<name>}` ⇄
`shared_<name>.md`, and that is the whole rule. It replaced three mechanisms
that all did the same job by different means: `bases:` frontmatter (prepended),
*preambles* (appended, two of them gated on a tool grant and a frontmatter
flag), and a Python constant substituted into a bespoke `{PLACEHOLDER:…}`.
None of the three let an author see, from the file they were editing, what the
prompt would actually contain. `SubAgent.bases` and the `callouts:` flag are
both **gone**: including a block *is* the declaration.

The shipped blocks, and the convention agents follow for placing them:

| Block | Rendered heading | Placement convention |
|---|---|---|
| `{SHARED:task_input}` | *(no heading — one paragraph)* | right after the opening identity paragraph |
| `{SHARED:dependencies}` | `# Dependency Contract` | after `## Purpose`, before the body that executes it |
| `{SHARED:escalation}` | `# Escalating a Blocker` | first of the closing blocks |
| `{SHARED:editing}` | `## Changing Files` | closing blocks |
| `{SHARED:callouts}` | `## Drawing the User's Attention` | closing blocks |
| `{SHARED:working_rules}` | `## How You Work` | second-to-last |
| `{SHARED:security}` | `## Absolute Rules` | **always last** |

The rule blocks close a prompt rather than opening it, so a prompt starts on
the agent's identity and role and ends with the rules that bind them, with
security both highest-precedence and in the position a long prompt can least
afford to have skimmed.

**Nothing is auto-appended, so three checks stand in for it.** An agent that
forgot `{SHARED:security}` would ship with no injection resistance and the only
symptom would be bad model behavior. So: `AgentRegistry.__validate_shared`
raises `AgentLoadError` at construction when a prompt omits a block in
`_REQUIRED_SHARED` (`working_rules`, `security`), when an agent granted a
`ToolSpec.modifies_files` tool omits `{SHARED:editing}`, when a block name has
no `shared_*.md`, or when a schema-less agent includes `{SHARED:task_input}`;
`test_agents.py` re-runs the same rules over every shipped `agent_*.md` /
`subagent_*.md` so the failure lands at build time; and shared files may not
contain tokens themselves (checked at load), which makes the single
substitution pass provably sufficient and rules out include cycles.

Two blocks are deliberately **not** everywhere. `{SHARED:editing}` names
`edit_file`/`create_file` and belongs only to agents granted such a tool —
roughly half never write a file (every critic, `investigator`, `compactor`,
`planner`, `web_search`), and telling a critic how to keep a diff minimal
contradicts the "use only your granted tools" rule in `{SHARED:security}`.
`{SHARED:callouts}` reaches only `guide` and `problem_solver`: a sub-agent's
text is buried in a collapsed subsession block whose open/close callouts the
*client* synthesizes from the `subsession_start`/`subsession_end` markers
(`SessionEntryView.tsx`), not the agent.

Because the system prompt is rebuilt on every turn, every block is present
regardless of context compaction (compaction rewrites only the message
history). No schema ever appears in this prompt — the concrete task (with
per-field descriptions pulled from the schema, and the sub-agent's only
remaining explanation of `return_result`) is rendered fresh per call into the
first user turn instead
(`kodo.runtime._engine._subagents._render_task_input`, doc/SESSIONS.md
"Typed sub-agent interface"). Consumed only by `WorkflowEngine`.

**`{SHARED:task_input}`** is the pointer to where a sub-agent's real task
lands. Including it is what *opts an agent into* the note, which fixed a
standing inaccuracy: it used to be injected into every schema-bearing agent,
`compactor` included, even though `_generate_compaction_summary` seeds that one
with a bare `"Conversation transcript to compact: …"` message rather than a
rendered `# Task` + `## Input Parameters` turn. `compactor` therefore includes
no such block and documents its real input in its own `## Your Input` section.
A schema-**less** agent including it is a load-time error (it would promise a
first message that never arrives).

**The two shared contracts.** `shared_dependencies.md` is the `DEPENDENCIES.md`
format spec, shared by its *writer* (`toolchain_builder`) and its *reader*
(`toolchain_depsmgr`). `shared_escalation.md` is when and how to hand a blocker
back through `return_result` — the prompt half of the `reason`/`options` output
fields `author_output()` declares (TOOLS.md §5A) — shared by the eight pipeline
authors. (`base_toolchain.md` also existed while four language-specific
toolchain agents shared the five-script contract; when they merged into
`toolchain_builder` it had a single consumer and was folded into that agent's
body.)

**There is no sub-agent roster.** A caller used to embed
`{PLACEHOLDER:SUBAGENTS}`, which expanded into an intro paragraph, a
tool/agent/review/kind table, and every listed sub-agent's `## Purpose`. It was
a prompt-side description of tools — what §7 forbids everywhere else — and it
duplicated a second, terser description each sub-agent carried on its
`SubAgentSpec`. Both collapsed into the generated tool:

- `## Purpose` **is** the `run_subagent_<name>` description now. That is why it
  is written caller-agnostic and third-person: its audience is whoever is
  deciding whether to delegate. The registry requires one on every sub-agent
  that appears in some caller's `subagents:` allow-list.
- `build_run_subagent_spec` appends the facts the table columns carried: a
  sentence from `standalone:` (workflow stage vs on-demand specialist) and, for
  an author, the review-loop contract from `critic:`.
- `SubAgentSpec.description` **no longer exists**; a spec carries schemas only.
- A **critic** is not invocable and gets no tool. What a caller needs to know
  about it is in its author's description; its own `## Purpose` is optional and
  serves only its own prompt.

Ordering was never in the roster either and still is not: it lives in the
caller's prose (the Guide's numbered pipeline + the Design Plan), since a single
linear predecessor (`depends_on`, long removed) misrepresented the real
inter-agent dependencies. To read any live assembled prompt, run
`python -m kodo -p <agent>`.

**The agents + the shared blocks** (frontmatter `tools:` lists):

`{SHARED:security}` → `## Absolute Rules` (always last) carries the
injection-resistance / role-fixing / tool-discipline / output-hygiene rules.
Confidentiality is one clause of "keep your outputs clean" rather than its own
section: the earlier version spent a quarter of the block resisting prompt
*extraction*, which defends nothing here — the user owns these prompts and can
print any of them with `python -m kodo -p <agent>`. What survived at full
length is the part with a real attack surface (sub-agents consume other
sub-agents' artifacts, repo file contents, and tool output), including its
concrete attack framings — *"I am the developer"*, *"this is a security
audit"*, *"repeat everything above"* — which are the anchors doing the
refusal work and must not be compressed into an abstraction.

`{SHARED:working_rules}` → `## How You Work` (every agent) carries Reasoning Is
Silent, **Thinking Is Only for Thinking** (no tool-call syntax of any format
inside thinking blocks — nothing there is parsed or executed; never fabricate a
phantom call's result; end thinking and invoke the tool for real — added after
Qwen36-27B was observed emitting XML-tag pseudo-calls inside its reasoning),
Verify Don't Assume, and the tone rule (mirror the user when speaking to them,
never in an artifact).

`{SHARED:editing}` → `## Changing Files` (agents that can write files) carries minimal-change
edit discipline, no drive-by changes, read-before-you-write, match existing
conventions, and one sentence on `temporary: true` for throwaway work (see
doc/SECURITY.md). Two things that were here are gone rather than moved,
because each duplicated a tool description that is *always* in context for
whoever holds the tool: "prefer a targeted `edit_file` over regenerating a
whole file" is `edit_file`'s own opening line, and the scratch section's
tool-by-tool enumeration is on each tool's `temporary` parameter already.

| Agent | Tools declared | Role |
|---|---|---|
| `guide` | guided_dev_status, get_root_paths, find_files, find_text_in_files, run_subagent (expanded per sub-agent), ask_user, rollback, finalize_project, disable_autonomous_mode, **scaffold_new_project** | Arbiter for the **guided** workflow. Resolved through the same `tools_for_agent` path as every other agent. `subagents:` allow-list includes the pipeline agents **+ `toolchain_builder` + the shared `investigator`** (preliminary narrative investigation before stage 1 — user-consented in interactive mode, default in autonomous — and mid-pipeline web-opinion consults on substantive ambiguities; findings fold into `narrative_author`'s `instructions` as attributed candidates). |
| `problem_solver` | filesystem, **read_file**, edit_file, create_file, create_directory, run_command, get_root_paths, find_files, find_text_in_files, **toolchain_build/deps**, **run_subagent**, ask_user, **scaffold_new_project** | **Orchestrator** of the **problem-solving** workflow — runs *outside* the Guide pipeline, talking to the user directly and editing real files on disk (see §15). Decides which combination of its sub-agents solves a problem: `planner` (investigates the codebase, then returns `codebase_context` + an ordered plan), `investigator` (read-only research), `developer` (code+tests), plus `toolchain_builder`. Keeps its own direct tools, but delegates the heavy lifting: **one routing question** — *what would a sub-agent's session hold that mine would otherwise have to?*, weighed as what the work forces the coordinator to **read, work out, or build** (**reworked 2026-08-15**, replacing the old size gate "one file / ~300 LOC"; see the `planner` row). Substantial on any of the three → delegate; near zero on all three → do it directly. Building → Developer; web research and documentation reports → Investigator. It deliberately does **not** scope work before handing it over, and must not run a code investigation as a warm-up for the Planner (which would read the same files and defeat the compression). It carries the Planner's `codebase_context` into *every* downstream sub-agent call, since each starts cold. Direct tools are for the **one cheap look** that settles routing, for **trivial retrieval** (a single `read_file`/`find_files`/`find_text_in_files` call answers the question — no cross-source synthesis needed, so the Investigator round-trip buys nothing), and for work it already knows how to do; it owns writing documentation deliverables from an Investigator `report`. **Non-change asks** (a question about the code, a report, a commit message, "run the tests and tell me what fails") never go to the Planner, which plans implementations — they route between the coordinator itself and the Investigator. Its sub-agent roster is written by hand in the prompt body: there is no `{PLACEHOLDER:SUBAGENTS}` expansion anywhere any more (retired — each sub-agent's `## Purpose` is now the `run_subagent_<name>` tool description instead, see `_registry.py`). |
| `investigator` | read_file, find_files, find_text_in_files, get_root_paths, web_search, **read_webpage** | **Read-only researcher**, shared by both entry agents (`solo` + `standalone`; in `problem_solver`'s and `guide`'s allow-lists). Explores existing code and/or searches the web (`web_search`, now backed by a dedicated research agent that plans its own discovery/read/synthesis loop — doc/WEB_SEARCH.md; `read_webpage` to fetch one known URL's content — doc/READ_WEBPAGE.md) to answer specific `questions` (`mode: qa`) or produce a full investigative `report` (`mode: report`). Changes nothing; returns answers/report + `sources`. |
| `planner` | **read_file, find_files, find_text_in_files, get_root_paths** (+ auto-granted `return_result`) | **Investigate-then-plan** agent for Problem Solver (`solo` + `standalone`) — **reshaped 2026-08-02 from a tool-less pure reasoner into "an Investigator that ends with a plan instead of a report."** It reads the real code itself, then returns **`codebase_context`** (a thorough briefing on the code the work touches — layout, structures and wiring, conventions, blast radius, hazards, build/test story) plus an ordered `tasks` list, each naming the sub-agent (`toolchain_builder`/`investigator`/`developer`), how Problem Solver should build its input, the concrete `files`, and the `acceptance` criteria that close the step. Writes nothing. **Why the reshape:** as a tool-less reasoner it saw only what Problem Solver wrote, so it absorbed nothing the caller didn't already hold and **failed the system's own delegation test** (`agent_problem_solver.md`: "delegation is justified only by compression"). Giving it read tools makes the whole code study stay in its sub-session. Note it **cannot** delegate to `investigator` — `_transient.active_subsession` is a single pointer, not a stack (§12.1), so subsessions don't nest; the tools had to be granted directly. Its tool set is deliberately `investigator` **minus `web_search`/`read_webpage`** — web research stays Problem Solver's Investigator call. **Invocation is "a code change whose shape Problem Solver would have to work out"** (`agent_problem_solver.md` Step 3; **reworded 2026-08-15**, previously "anything past the small-ask fast path"): no size threshold, no step count, and explicitly **no caller-side scoping** — Problem Solver must *not* work out the approach or study the code first, since that is the delegated work. `plan_warranted: false` means the work is indivisible (only *coding* steps count; toolchain/test/investigation are supporting work) and is **not** a wasted call: `codebase_context` is required in the schema either way, and Problem Solver passes it as the single Developer task's `context`. A `toolchain_builder` step, when the project has no working build model, is **always task 1**, ahead of every development step; the Planner now determines this by looking rather than emitting a conditional step. |
| `developer` | filesystem, edit_file, create_file, create_directory, read_file, run_command, find_files, find_text_in_files, get_root_paths, toolchain_build, toolchain_deps | **Coder + Test Coder in one** for Problem Solver (`solo` + `standalone`). From free-form `instructions` works out the target behavior, writes production code and behavioral tests, and keeps the project building. No upstream artifacts, no critic loop. It does **not** set up a missing toolchain (that would require a nested subsession spawn, which the single-level subsession model doesn't support); instead it returns `verification` starting with the token `toolchain_not_set_up` and the Problem Solver sets the toolchain up and re-runs it. |
| `toolchain_builder` | run_command, filesystem, edit_file, create_file, create_directory, find_files, find_text_in_files, get_root_paths, ask_user | **Toolchain-setup** agent (`bases: [dependencies]`), the single, **language-agnostic** replacement for the former `toolchain_python`/`toolchain_cpp`/`toolchain_rust`/`toolchain_typescript` family (merged 2026-07-28; `base_toolchain.md` was folded into its body at the same time). Spawnable by `guide` and `problem_solver` (both entry agents — never by a sub-agent, to avoid nested subsessions). Runs six ordered phases: **detect** the state on disk → **choose** the toolchain → **create** it when absent → write the **five scripts** → write the **docs** → **verify and report**. The task's `mode` is a caller *hint*; the agent classifies bootstrap-vs-convert from disk and returns `mode_used`. On bootstrap it takes the ecosystem's industry-standard stack from a compact **Ecosystem Defaults** table (Python, TypeScript, JavaScript, Rust, C/C++, Go, Java/Kotlin, C#/.NET, Ruby, Swift — anything outside it is still in scope via that ecosystem's most widely adopted equivalents); when two standards genuinely compete it makes **one** batched `ask_user` call with its default listed first, and in autonomous mode takes the default and records it. Outputs the five per-platform script pairs (`scripts/{build,format,static_analysis,test,full_build}.{sh,ps1}`) + `DEVELOPMENT.md` + `DEPENDENCIES.md`, executed by `toolchain_build`/`toolchain_deps` (§8). Key contract rule: **`build` is never a no-op** — a four-rung ladder (distributable package → executable/bundle → compile every target → whole-program syntax/import check) closes the "this language needs no build" trap that the per-language prompts each had to close separately. The old family's deliberate divergences survive as *Ecosystem notes*: C++ bakes warnings-as-errors into `CMakeLists.txt` so every build fails on a warning and `static_analysis` has three mandatory parts; Rust deliberately keeps `build` lenient with all strictness in `static_analysis` (`-D warnings`); TS/JS makes `scripts/` the single source of truth on bootstrap and wraps `package.json` `"scripts"` on convert, and never migrates a JavaScript project to TypeScript on its own initiative (JS is now set up **as JS**, no longer reported out of scope); Python requires a type checker in `static_analysis`; and each manager's dependency-kind collapse is stated honestly rather than invented. Suggest-then-confirm invocation. |
| `toolchain_depsmgr` | get_root_paths, find_files, read_file, run_command, edit_file, create_file | **Dependency-management** agent (`{SHARED:dependencies}`). The acting force behind the `toolchain_deps` tool — **not** spawnable via `run_subagent` by anyone (no agent lists it; the tool drives it through the ungated `run_dependency_manager` service). Per run it performs one add/remove/update op by reading and executing the project's `DEPENDENCIES.md`; returns `status: completed/failed/dependencies_md_missing`. Toolchain-agnostic: all language specifics come from `DEPENDENCIES.md`. |
| `narrative_author` | filesystem, edit_file, create_file, create_directory, read_file, ask_user | Solo, user-facing intake. Writes the Narrative and Tech Stack documents directly. |
| `architect`, `requirements_author`, `functional_designer`, `e2e_test_designer`, `test_designer` | filesystem, edit_file, create_file, create_directory, read_file | Authors (paired with a critic), all carrying `bases: [escalation]` — they escalate a blocker through `return_result`'s `reason`/`summary`/`options`, not through a tool. `coder` and `e2e_test_coder` additionally hold `toolchain_build`/`toolchain_deps`. |
| `architect_critic`, `requirements_critic`, `functional_design_critic`, `test_design_critic`, `e2e_test_design_critic`, `code_critic`, `e2e_test_code_critic` | read_file (+ the auto-granted `return_result`) | Critics (`role: critic`) — their `return_result` payload *is* the verdict (`{path, accept, concerns, summary}`), which the engine records; the engine alone drives the accept/review flow (§7/§12.1). `test_design_critic` reviews the per-component Test Plan, holding every test to behavior over implementation; `e2e_test_code_critic` reviews the end-to-end suite *as code*, enforcing opaque-box, behavior-and-side-effect assertions over implementation details. |
| `test_coder` | filesystem, edit_file, create_file, create_directory, read_file (+ `{SHARED:escalation}`) | Solo author of test code + stubs from the accepted Test Plan (no longer a critic — plan review moved to `test_design_critic`). |
| `e2e_test_coder` | filesystem, edit_file, create_file, create_directory, read_file, toolchain_build, toolchain_deps (+ `{SHARED:escalation}`) | Author (paired with `e2e_test_code_critic`) of the product-level end-to-end integration suite (stage 9). Assembles the whole system as a black box behind local mock servers + injected configuration, runs it via `toolchain_build`, and iterates to a clean state before the critic; a genuine system-behavior mismatch is escalated to the guide (`reason: "system_behavior_mismatch"`), not papered over. |

**State:** Loader/registry complete (incl. `{SHARED:<name>}` inclusion **and per-agent `run_subagent_<name>` tools built from `## Purpose` + `critic`/`standalone` frontmatter**); agent roster present (guided pipeline + `problem_solver` as an orchestrator over its `planner`/`investigator`/`developer` trio + the language-agnostic `toolchain_builder` toolchain-setup agent, which the `planner` may now also schedule as the first task of a plan); every declared tool now has a dispatch handler.

---

## 12. `runtime/` — the engine and tool dispatch

This is the orchestration core. [\_\_init\_\_.py](../src/kodo/runtime/__init__.py)
re-exports the public surface.

### 12.1 `WorkflowEngine` ([_engine/](../src/kodo/runtime/_engine/))

The single-worker substrate. **Constructor-injected dependencies** (all from the
server composition root):

```
sink: MessageSink            gate: GateOrchestrator
key_provider: ApiKeyProvider get_settings: Callable[[], dict]
transient: TransientStore    workspace_layout: WorkspaceLayout
registry: AgentRegistry      gateway: LLMGateway
session_workspace: SessionWorkspace | None
```

(The engine is workspace-scoped: there is no per-session `ProjectLayout`
injected or bound at all — since the 2026-07-24 multi-project rework, Guided
mode addresses its bound roots exactly the way Problem Solver always has, via
`SessionWorkspace`/`root_paths()` (WS_PROTOCOL.md §7.1c). `ProjectLayout` itself is still used,
just per-root and on demand — e.g. by `scaffold_new_project` to
lay a root out, or by the checkpoint mirror to compute a root's paths.)

It **internally constructs**: a `SessionState`, one `_EngineServices` adapter,
and a **`CheckpointCoordinator.mirrors: RootMirrorManager`** (§12.4/§10b — the *single*
shadow-git mirror coordinator now shared by both workflow modes; there is no
separate Guided-only mirror anymore, see §7). It builds a
`tools.ToolDispatcher` **per agent run** (via `_make_dispatcher`). A document's
state is never reconstructed at bootstrap — `kodo.guided_state` reads each
file's `.jsonl` log on demand. It owns `_main_messages` (the shared
entry-agent running `list[Message]`, agent-agnostic across Guide/Problem
Solver) and cumulative USD.

**Package layout** — the former single 4000-line module is a package split
along its concern seams, assembled from **mixins** (behaviour slices sharing
the one engine instance's state; every mixin method annotates
`self: EngineHost`, the explicit protocol in
[_proto.py](../src/kodo/runtime/_engine/_proto.py) that is the single map of
the state and cross-module methods mixins may touch — a mixin needing
something new must add it there first, keeping the coupling visible) and
**collaborators** (objects that own their state and reach back through
narrow per-collaborator host protocols living next to each collaborator):

| Module | Kind | Contents |
| ------ | ---- | -------- |
| [_core.py](../src/kodo/runtime/_engine/_core.py) | class | `WorkflowEngine(LLMPlumbingMixin, WorkerMixin, TurnLoopMixin, SubagentMixin, ResumeMixin)` — constructor wiring, `start()`, every public `handle_*` WS entry point, project create/init, `_root_paths`/`_has_workspace`/`_make_resolver`, `_run_rollback`, `_finalize_document`, `_disable_autonomous`. |
| [_proto.py](../src/kodo/runtime/_engine/_proto.py) | protocol | `EngineHost` — the typed mixin seam. |
| [_worker.py](../src/kodo/runtime/_engine/_worker.py) | mixin | `WorkerMixin` — `_run_worker` (the single queue-driven coroutine) + `_handle_input_no_agent`. |
| [_llm.py](../src/kodo/runtime/_engine/_llm.py) | mixin | `LLMPlumbingMixin` — `_resolve_plugin`/`_resolve_model_key`, `_run_silent_return_turn`, `_run_silent_tool_loop_turn` (a silent, multi-round, non-subsession tool-calling turn for the `web_search` agent — deadline- and round-capped, doc/WEB_SEARCH.md), `_security_judge`. |
| [_turns.py](../src/kodo/runtime/_engine/_turns.py) | mixin | `TurnLoopMixin` — `_run_entry_agent` (+ the two entry wrappers below), `_run_agent_turn` (the generic LLM tool loop), `_dispatch_tool_calls`, `_finalize_tool_result`, `_make_dispatcher`, attachment storing. |
| [_subagents.py](../src/kodo/runtime/_engine/_subagents.py) | mixin | `SubagentMixin` — `_run_subagent`/`_spawn_subagent` + the `_assert_can_spawn` gate, subsession lifecycle/replay, `_run_dependency_manager`, `_run_web_search_agent` (drives the `web_search` agent via `_run_silent_tool_loop_turn`, doc/WEB_SEARCH.md), `_run_review_loop`/`_run_review_round`/`_record_findings`, plus the findings scope threading (`_findings_dir`/`_findings_snapshot`/`_document_status`, doc/FINDINGS.md). |
| [_resume.py](../src/kodo/runtime/_engine/_resume.py) | mixin | `ResumeMixin` — Stop folding (`_persist_interrupted_turn`) + cold-restart resume (`_resume_main_turn`, `_build_replay_ledger`). |
| [_events.py](../src/kodo/runtime/_engine/_events.py) | collaborator | `EngineEmitters` — every client event emitter + cumulative cost. |
| [_compaction.py](../src/kodo/runtime/_engine/_compaction.py) | collaborator | `ContextCompactor` (+ `CompactorHost`) — context gauge, in-place compaction, `render_transcript`/`estimate_tokens`. `render_transcript`'s four headers (`## USER` / `## ASSISTANT` / `## TOOL RESULTS` / `## PRIOR COMPACTED CONTEXT`) are load-bearing: they split the three kinds of `role="user"` message the engine emits, and the compactor's verbatim-user-prompt rule keys off `## USER` meaning *only* a real prompt (STATE_AND_LIFECYCLE.md §4.5). |
| [_titling.py](../src/kodo/runtime/_engine/_titling.py) | collaborator | `SessionTitler` (+ `TitlerHost`) — fire-and-forget session titling; runs `kodo.titling.generate_title` in a background thread and never blocks the worker. |
| [_checkpointing.py](../src/kodo/runtime/_engine/_checkpointing.py) | collaborator | `CheckpointCoordinator` (+ `CheckpointHost`) — `_MUTATING_TOOLS`, prepare/commit around mutating dispatches, `record_guided_revision`, owns the `RootMirrorManager` (`.mirrors`). |
| [_history.py](../src/kodo/runtime/_engine/_history.py) | collaborator | `HistoryProjector` — `session.jsonl` read-back: feed rebuild (`history_entries`) + context rehydration (`load_main_messages`). |
| [_services.py](../src/kodo/runtime/_engine/_services.py) | adapter | `_EngineServices` — adapts engine callbacks to the `tools.EngineServices` protocol. |
| [_shared.py](../src/kodo/runtime/_engine/_shared.py) | helpers | Shared agent-name constants, `_slugify_project_name`, `_unique_child_dir`. |

The package's public surface is unchanged by the split:
[\_\_init\_\_.py](../src/kodo/runtime/_engine/__init__.py) exports
`WorkflowEngine` (plus the two project-dir helpers `runtime/__init__` re-uses).

**Composition / call graph:**

- `start()` → `TransientStore.attach_session` → spawns `_run_worker` task. If
  resumed, loads messages and may re-fire a pending prompt; bound roots need no
  re-binding step — they come from the next `workspace.folders` push (§14) and
  the persisted `workspace_folders`/`workspace_locked_paths` fallback
  (WS_PROTOCOL.md §7.1b/§7.1c) exactly like Problem Solver always worked. No
  index to rebuild either way.
- **Public client entry points** (registered as WS handlers in `_app`, §14):
  `handle_prompt_submit(text, request_id)` enqueues a prompt;
  `handle_mode_set(autonomous)` sets the **Autonomous/Interactive** mode
  (`SessionState.autonomous`, user-facing) and persists it; `handle_workflow_set(mode)`
  sets the workflow (`SessionState.workflow_mode`, normalised to `"guided"` |
  `"problem_solving"` | `"judge"` — the last is validator-only, never sent by
  kodo-vsix); `stop()` cancels the worker. Both setters emit `EVT_STATE` and
  never interrupt an in-flight prompt.
- `_run_worker()` — dequeues one task at a time. **First it freezes the
  per-prompt autonomous mode** (`effective_autonomous = autonomous`), then
  **routes by `workflow_mode`**: `"problem_solving"` →
  `_run_problem_solver_with_input` (if the `problem_solver` agent is present,
  else `_handle_input_no_agent`); `"judge"` → `_run_judge_with_input` likewise
  gated on `judge` agent availability (validator-only — see §15); otherwise →
  `_run_guide_with_input`. Exits the loop once `phase == "done"`.
- `_resolve_plugin(capability)` → reads fresh settings → `get_llm_registry()` →
  builds `ClaudePlugin` (via `ApiKeyProvider.get_key`) or `LlamaPlugin`, wrapped
  in `LoggingLLMPlugin`.
- `_run_agent_turn(...)` — the **generic LLM tool loop**, shared by guide
  and leaf agents: streams events → emits `EVT_LLM_TURN_START`, stream chunks,
  `EVT_AGENT_TOOL_CALL`, `EVT_USAGE_UPDATE` (persisted as a `usage` marker in
  whichever log — main or the active subsession — is currently active,
  carrying `usd_cost`/`stop_reason`/`agent` alongside the token counts; there
  is no separate per-agent audit log) → logs via `ToolCallLogger` →
  dispatches each tool via an injected `tool_dispatch` callback → loops until
  no tool calls (or `stop_after_tools`).
- `_run_guide_with_input` / `_run_problem_solver_with_input` /
  `_run_judge_with_input` → thin wrappers; all three delegate to
  `_run_entry_agent(agent_name, text, attachments)`.
- `_run_entry_agent(agent_name, ...)` → the shared entry-agent driver: loads
  the agent, resolves its plugin, stores prompt attachments and appends an
  `<ATTACHMENT>` tag manifest after the prompt (`read_attachment` tool fetches
  content on demand), appends
  the seed user message to the shared `_main_messages` (persisted immediately
  to `session.jsonl`), then builds a per-run `ToolDispatcher` and runs
  `_run_agent_turn` with `tool_dispatch = dispatcher.dispatch`,
  `tools = tools_for_agent(agent.tools)` (the registry already filtered the
  agent's tools for `effective_autonomous`, so `ask_user` is withheld in
  autonomous mode for both entry agents),
  `stop_after_tools = lambda: dispatcher.stop_requested`,
  `persist = _persist_main_messages(agent_name)` and
  `flush_before_dispatch=True`. The two entry agents differ only in system
  prompt and tool set — switching workflow mode continues one conversation.
- `_run_subagent` → builds a per-run `ToolDispatcher`, `tools =
  tools_for_agent(agent.tools)`, `tool_dispatch = dispatcher.dispatch`,
  `stop_after_tools = lambda: dispatcher.stop_requested`. Returns the
  sub-agent's structured `return_result` output (or a bare
  `{schema_compliance: False}` fallback if it never called it — there is no
  artifact index to recover a partial result from).
- `_run_dependency_manager` (exposed via `_EngineServices.run_dependency_manager`,
  the callback the `toolchain_deps` tool invokes) → drives the fixed
  `toolchain_depsmgr` agent straight through `_spawn_subagent` (the ungated
  primitive), **bypassing the `_assert_can_spawn` allow-list gate** that
  `_run_subagent` applies. Possession of the `toolchain_deps` tool is the
  authorization; the agent is deliberately absent from `_DIRECT_ONLY_AGENTS`
  (which would make `_spawn_subagent` short-circuit it) and from every
  `subagents:` list, so the only path to it is the tool.
- `_run_review_loop` → the author/critic loop `_run_subagent` enters whenever
  the target sub-agent declares a `critic:`. Each round spawns the author with
  **identical `instructions`** (only `for_revision_path` is added, from round
  two) — findings are never rendered into the task; both halves read them
  through `get_findings`, whose scope this loop binds (doc/FINDINGS.md §4). It
  reads back `author_output.primary_path`, spawns the critic against that path,
  then derives the status via `_document_status` — **the stores, not the
  critic's `return_result`, are what the loop acts on**, because the user's own
  review decision lands there too. The round's `opened`/`closed` counters come
  from diffing `_findings_snapshot` before and after the critic's subsession
  (rather than threading a value out of `_record_findings`, which runs several
  frames down and does not run at all for a replayed subsession). Emits
  `EVT_REVIEW_STARTED`/`EVT_REVIEW_VERDICT` per round. Stops on acceptance, on an
  author **escalation** (a non-empty `reason` on its result — the critic is not
  spawned and no further round is spent; `outcome: "escalated"`), on
  `max_rounds` (caller-sized, default 5, capped at 10), or when a round closes
  nothing and opens nothing (`not_converging` — the stall detector that replaced
  the old concern-count heuristic).
- `_record_findings` → called from `_drive_subsession` for every agent declaring
  `role: critic`: applies the round's findings to that document's session-scoped
  backlog (`kodo.findings.apply_findings`), closes the round with a
  `review_round` entry, then drives `_finalize_document` once **nothing is left
  outstanding**. There is no `accept` field to consult — the verdict is derived.
- `_finalize_document(path)` (called from the post-dispatch hook below, not
  exposed via `EngineServices` — there is no tool indirection) → autonomous
  mode — and Edit Control `allow_all` — immediately `append_accepted`s;
  otherwise it fires the same approval gate `request_user_review_artifact` used
  to, then records `append_review_result` (+ `append_accepted` on agreement, or
  `kodo.findings.record_user_feedback` on rejection). Replaces the old
  `__complete_artifact`/promotion path entirely — there is nothing to
  materialize, since the document was already a real file.
- `CheckpointCoordinator.record_guided_revision(...)` (also called from the post-dispatch hook) →
  after a `filesystem`/`edit_file`/`create_file`/`create_directory` checkpoint commit, if the affected path is
  tracked under `specs`/`src`/`test` of the root the checkpoint landed in
  (`checkpoint.root` — already resolved by `RootMirrorManager`, so no separate
  lookup is needed even with N bound roots), appends a
  `new_revision` jsonl entry carrying that exact commit's sha — in **both**
  workflow modes (§7).
- `_run_rollback(root, target_sha)` (exposed via `_EngineServices.rollback`) →
  *root* is a resolved absolute path (the `rollback` tool resolves the agent's
  `root` input — a `get_root_paths` name — before calling here); delegates
  directly to `RootMirrorManager.rollback` (the same primitive the checkpoint
  UI's `checkpoint.rollback` uses) and resets the in-memory conversation. No
  index to rebuild.
- `_disable_autonomous` (exposed via
  `_EngineServices.disable_autonomous_mode`) backs the
  guide's `disable_autonomous_mode` tool.
- `_create_project` (exposed via `_EngineServices.create_project`) backs the
  `scaffold_new_project` tool's no-`path` creation branch: slugify name → `_unique_child_dir` under the
  session physical root → `mkdir` → add to the logical-root map →
  `RootMirrorManager.prepare` (scaffolds `.kodo/`+mirror) → push
  `EVT_WORKSPACE_ADD_FOLDER`. The logical-root map update is synchronous and
  in-process (`self._session_workspace.set_folders(...)`, before the
  WS round trip even starts), and `_EngineServices` also exposes
  `has_workspace()`/`root_paths()` as **live** reads of
  `EngineHost._has_workspace`/`_root_paths` — `ToolContext`
  calls them fresh on every access rather than caching a value from when the
  dispatcher was built, so a project created (or a folder added by the user
  directly in VS Code) partway through a turn is visible to that same turn's
  very next tool call, not just the next turn (doc/TOOLS.md §5).
- **Per-tool-call checkpointing (both workflow modes)** — `CheckpointCoordinator._enabled()`
  is now unconditional (Guided mode drives the same mirror Problem Solver
  always has — there is no separate Guided checkpoint system to collide with,
  see §7). Inside `_dispatch_tool_calls`, around each of `_MUTATING_TOOLS =
  {"filesystem", "edit_file", "create_file", "create_directory", "run_command"}`: `CheckpointCoordinator.prepare(tool_name,
  tool_input)` resolves the affected path(s) (`CheckpointCoordinator.mutation_paths` — `edit_file`'s/`create_file`'s/`create_directory`'s
  `path`; `filesystem`'s `destination`/`path`/`source`; `run_command`'s `cwd`,
  gated by `command_may_mutate(parse_command(cmd))`, §10b) and calls
  `CheckpointCoordinator.mirrors.prepare(path)` **before** dispatch, so the baseline commit
  captures pre-change state. After dispatch, `CheckpointCoordinator.commit(...)` calls
  `CheckpointCoordinator.mirrors.commit_for_path(path, label)` (`run_command` additionally
  `sweep_initialized`s every other already-initialised mirror, to catch writes
  outside the command's `cwd`). `_finalize_tool_result` injects the resulting
  `checkpoint.sha` into the LLM-visible result as `checkpoint_sha` (declared
  optional in each of the 5 tools' `output_schema`, so `normalize_output` keeps
  it without flagging non-compliance), rides `{root, sha, parent}` out-of-band
  on `EVT_AGENT_TOOL_CALL_DETAIL` as a `"checkpoint"` key (`null` when no commit
  happened), and — for `filesystem`/`edit_file`/`create_file`/`create_directory` only — drives
  `CheckpointCoordinator.record_guided_revision` (above). New public `handle_checkpoint_undo(root,
  sha)` / `handle_checkpoint_rollback(root, sha)` delegate straight to
  `RootMirrorManager.undo`/`.rollback` — **files-only**, they never touch
  conversation history (deliberately distinct from the Guide's
  conversation-rewinding `rollback` tool, which now calls the same
  `RootMirrorManager.rollback` primitive but additionally resets
  `_main_messages`).

**The engine injects into every `ToolDispatcher`:** `GateOrchestrator`,
`SessionState`, and one `_EngineServices` adapter wrapping `_run_subagent` /
`_run_rollback` /
`_disable_autonomous`. The per-prompt autonomous mode is read
from `SessionState.effective_autonomous` rather than passed in.

### 12.2 Tool dispatch (`tools.ToolDispatcher`)

Dispatch no longer lives in `runtime`; see §6A. The engine builds one
`tools.ToolDispatcher` per agent run (guide and leaf alike) and passes its
`dispatch` as the `tool_dispatch` callback into `_run_agent_turn`. After the run
it reads `dispatcher.returned_output` and uses `dispatcher.stop_requested`
as the `stop_after_tools` predicate. There is one unified surface — no
guide-vs-leaf split.

### 12.4 Supporting runtime modules

| Module | Defines | Role / links |
|---|---|---|
| [_bootstrap.py](../src/kodo/runtime/_bootstrap.py) | `locate_guide_session()` | Workspace-tier session location only: locate/create the Guide session marker + `sessions/` dir. There is no project-tier bootstrap anymore — a document's state lives entirely in its own `.jsonl` evolution log (§7), read on demand. |
| [_guide.py](../src/kodo/runtime/_guide.py) | `GuideMarker` | Reads/writes `.kodo/guide.session`. Used by `locate_guide_session`. |
| [_checkpoints.py](../src/kodo/runtime/_checkpoints.py) | `RootMirrorManager`, `CheckpointRef` (frozen), `command_may_mutate()` | The **single** shadow-git mirror coordinator, now driving both workflow modes (§12.1) — there is no longer a second, Guided-only mirror at the same path to collide with. Bridges the path-agnostic `mirror.ShadowMirror` to Kōdo's conventions: every root a session may touch gets its own independent mirror at `<root>/.kodo/checkpoints`, created **lazily** the first time a file-mutating tool writes under that root (scaffolding `<root>/.kodo/` + `kodo.md` via `ProjectLayout.scaffold_kodo_dir()`, §5, at that moment). `_root_for(path)` maps a path to its enclosing root by longest-prefix match. `_KODO_EXCLUDES` (node_modules/.venv/`__pycache__`/dist/build/egg-info/caches + always `.kodo/`+`.git/`) seed each mirror's `info/exclude` **on top of** the project's own `.gitignore` — this is *why* `.kodo/guided_dev_state/*.jsonl` (§7) is never committed by this same mirror. One `asyncio.Lock` serialises `prepare`/`commit_for_path`/`sweep_initialized`/`undo`/`rollback`. The free function `command_may_mutate(parsed: ParsedCommand) -> bool` is the caller-side mutation heuristic the parser (§10b) deliberately omits: `True` if any redirection is an output redirect (`> >> >\| &> &>> <>`), else `True` unless every executable's basename is on a small read-only allow-list (`ls cat grep find rg fd pwd wc diff …` — notably **not** `git`, since even read-only-looking git subcommands can touch `.git/` state) — **defaults to `True` (mutating) whenever uncertain**, so a missed checkpoint is never the failure mode; an unnecessary no-op commit is. |
| [_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator`, `ApprovalResponse`, `PermissionResponse` | **Composes** `ResponseChannel` (production: `SessionChannel`) + `TransientStore`. `fire_approval`/`fire_questions`/`fire_permission` send `kind=request`, register a future, and await. Approvals persist `pending_prompt`; permission prompts persist `pending_security_alert` (just the `tool_call_id`, for the duration of the wait) — both for process-restart re-surface, mirroring each other's cleared-on-resolve/kept-on-cancel pattern; a question batch has neither and is re-driven from scratch (SESSIONS.md "Resume", SECURITY.md §7a). None of the three needs anything special for a *live* disconnect/reconnect — the future and its request envelope live on `SessionChannel`, which survives that regardless (SECURITY.md §7b). `fire_questions(questions, tool_call_id)` carries the whole `ask_user` batch plus the calling tool_use id and returns normalized `{selected, free_text}` answers. `fire_permission(...)` carries one gated tool call's preview (tool/risk/intent/reason/params) and returns the user's allow/deny + optional feedback (`prompt.permission`, doc/SECURITY.md §6); malformed actions coerce to deny. `fire = fire_approval` alias. Satisfies `tools.GateLike`; reached by `_finalize_document` (§12.1) for the interactive document-review gate. |
| [_agenttools.py](../src/kodo/runtime/_agenttools.py) | `agent_tool_specs(registry, agent)` | The **only** place an agent's LLM-facing tool list is assembled, and the join between `kodo.subagents` and `kodo.tools` — sibling T3 packages that may not import each other. Resolves the agent's declared `tools:` through `tools_for_agent`, with two names expanded from the registry instead of the static catalog: `run_subagent` → one `run_subagent_<name>` tool per invocable sub-agent (each carrying that sub-agent's own `input_schema`, plus a `max_rounds`/`review` loop contract when it declares a `critic:`), and `return_result` → the same tool with `result` bound to this agent's own `output_schema`. Every caller goes through it — the live turn loop, crash-resume, subsessions, the silent engine-driven turns, and `kodo --tools` — so the surface a model sees cannot differ by code path (doc/TOOLS.md §5A). |
| [_session.py](../src/kodo/runtime/_session.py) | `SessionState` | Mutable `phase`/`agent`/`component` plus the two mode fields: `autonomous` (user-facing Autonomous/Interactive, set by `handle_mode_set`, reported in `to_dict()`/`EVT_STATE`) and `effective_autonomous` (frozen per prompt by `_run_worker`; what tools/registry actually read), and `workflow_mode` (`"guided"`/`"problem_solving"`/`"judge"`, in `to_dict()`; the last is validator-only). Shared by the engine; satisfies `tools.SessionLike` (`finalize_project` writes `phase`; tools read `effective_autonomous`). |
| [_session_log.py](../src/kodo/runtime/_session_log.py) | `SessionLog` | Append-only JSONL per session. |

**State:** Engine, dispatch (now in `tools/`), gates, rollback are
implemented and exercised by the guide/author-critic flow.

---

## 13. `state/` & `security/`

| Module | State |
|---|---|
| [state/_transient.py](../src/kodo/state/_transient.py) `TransientStore` | ✅ Per-session dir under `.kodo/sessions/<id>/`: `meta.json`, `transient.json` (stage/prompt/autonomous/pending_prompt/security_rules), `session.jsonl` (guide messages + markers, incl. per-call `usage`), `subsessions/*.jsonl`. Injected into engine + gate. |
| [state/_memory.py](../src/kodo/state/_memory.py) | ⚠️ **Stub** (`__all__ = []`). |
| [security/_layer.py](../src/kodo/security/_layer.py) `SecurityLayer` | ✅ **Implemented** — judges every tool call per the live `command_control` posture (permissive/defensive/smart); `ask` verdicts fire `prompt.permission` from the dispatcher. `run_command` delegates to `_rules.evaluate_command`. Full design in [doc/SECURITY.md](SECURITY.md). |
| [security/_analysis.py](../src/kodo/security/_analysis.py) | ✅ Static `run_command` workspace-target analysis over the `shellparser` parse (outside paths / substitutions / read-only fast path). |
| [security/_classify.py](../src/kodo/security/_classify.py) `NormalizedSegment` | ✅ Wrapper-peeled/alias-resolved per-segment view the rule engine matches on (executable/subcommand/flags, nested-command/opaque, heredoc-body-as-code detection). |
| [security/_rules.py](../src/kodo/security/_rules.py) `evaluate_command` | ✅ The deterministic heuristic rule ladder (no LLM — the former SMART-mode LLM intent judge, `_judge.py`, was deleted when this replaced it). Also computes the Phase 2 `rule_offer`/known-rule silencing. |
| [security/_defaults.py](../src/kodo/security/_defaults.py) | ✅ The built-in POSIX/PowerShell `CommandRule` tables `_rules.py` evaluates against. |
| [security/_store.py](../src/kodo/security/_store.py) | ✅ The global (user-wide) Phase 2 rule store (`~/.kodo/etc/security_rules.json`). Session-scoped rules live outside this package entirely — see `state/_transient.py` below. |

---

## 14. `server/` — composition root

| Module | Role |
|---|---|
| [__main__.py](../src/kodo/server/__main__.py) | CLI → `Config.from_args` → `Lifecycle.check_and_write_pid` → `create_app` → aiohttp `TCPSite` on `127.0.0.1`. |
| [_config.py](../src/kodo/server/_config.py) | `Config` (frozen) — layered settings (project > user > defaults). `reload_settings()` is the `get_settings` callable injected into the engine (read fresh per dispatch). |
| [_lifecycle.py](../src/kodo/server/_lifecycle.py) | `Lifecycle` — PID file + signal handlers. |
| [_key_broker.py](../src/kodo/server/_key_broker.py) | `KeyBroker` — **implements `ApiKeyProvider`** (structural) over `ResponseChannel` (production: `SessionChannel`, not `WebSocketDispatcher` — that class is the superseded single-connection path, see the `_ws.py` row below). A live disconnect no longer cancels a pending key request (§883 row); only genuine session teardown does. |
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
`engine.handle_prompt_submit` (enqueues) → worker → `_run_guide_with_input`
→ `_run_agent_turn` streams the Guide LLM → tool calls dispatch through
the guide's `tools.ToolDispatcher` → `run_subagent` (via a `run_subagent_<name>` call)
call back into the engine (via the injected `EngineServices`), which spawns leaf
agents — each with its own `ToolDispatcher` — that write real files directly
under `specs/`/`src/`/`test/` via `filesystem`/`edit_file`/`create_file`/`create_directory`, tracked by a
`.jsonl` evolution log per file (§7), not an artifact store. This is the
**guided** workflow.

**Prompt → work (problem-solving):** when `workflow_mode == "problem_solving"`,
the worker routes the same prompt to `_run_problem_solver_with_input` instead.
The `problem_solver` agent runs `_run_agent_turn` with its own dispatcher,
reading/writing the project's real files via the file-I/O and `run_command`
tools and talking to the user directly (`ask_user`, plus `<kodo_info>` progress
callouts in its message text). Unlike the Guide it has no fixed pipeline, but it
**is** an orchestrator: it decides which of its own sub-agents to run —
`planner` (investigates the codebase, then returns `codebase_context` + an
ordered plan; the route for a code change whose shape it doesn't already
know), `investigator` (web research and documentation reports), `developer`
(code+tests), and `toolchain_builder` (build setup, any language) — via `run_subagent`
(subsessions, §12.1), keeping direct tools for work it already knows how to
do, for the one cheap look that settles routing, and for trivial retrieval. While executing a plan it re-posts the whole plan with per-step
done/in-progress/pending state in a `<kodo_info>` callout after every completed
step (callout text is stripped from history, so it keeps its own working copy of
the plan in ordinary message text). No critics, no artifact index.

**Prompt → work (judge):** when `workflow_mode == "judge"`, the worker routes
to `_run_judge_with_input` instead. The `judge` agent (`agent_judge.md`) runs
`_run_agent_turn` with an **almost entirely read-only** dispatcher —
`read_file`, `find_files`, `find_text_in_files`, and the terminal
`submit_evaluation` tool; no editing, no general `run_command`, no
sub-agents, no `ask_user`. Its one exception is `toolchain_build` — the same
tool `problem_solver`/`toolchain_builder` runs use to execute a project's
generated `scripts/<step>` build/format/static-analysis/test pair — granted
so a scenario's RVP can ask the judge to get real, executed build/test
evidence instead of only inferring correctness from a read-only pass
(doc/VALIDATOR.md §9.2); `agent_judge.md`'s own prose gates it to RVP-directed
use only. This mode is **validator-only**:
`kodo.validator._evaluate.run_evaluation` is the sole caller, opening a second
session over the already-finished LUT run and submitting one turn (the RVP +
workspace/prompt/interaction-log context) for the judge to read and score;
`submit_evaluation` ends the turn. kodo-vsix's workflow picker never offers or
sends `"judge"`, so this flow is unreachable from the extension.

**Mode toggles (both apply to the *next* prompt):** the VSIX sidebar has two
toggles. *Autonomous/Interactive* → `toggle_autonomous` → `mode.set {autonomous}`
→ `handle_mode_set` sets `SessionState.autonomous` (and persists it); it does
**not** touch the in-flight prompt — `_run_worker` copies it into
`effective_autonomous` only when the next prompt is dequeued, so the sidebar
shows a "applies to your next prompt" notice. *Guided/Problem-Solving* →
`toggle_workflow_mode` → `workflow.set {mode}` → `handle_workflow_set` sets
`SessionState.workflow_mode`, which the worker reads at the next dequeue to pick
the entry agent. Both emit `EVT_STATE`; the Guide can also drop autonomous
mid-run via the `disable_autonomous_mode` tool (engine `_disable_autonomous`
clears both `autonomous` and `effective_autonomous` immediately and emits
`EVT_AUTONOMOUS_CHANGED`).

**Document acceptance:** a critic returns `{path, findings, summary}` via
`return_result` → `_drive_subsession` sees the callee declares `role: critic`
and calls `_record_findings`, which applies the findings to the session backlog
(§7a) and then, once nothing is outstanding, calls `_finalize_document(path)`:
autonomous mode — or Edit Control *Allow All* — immediately appends an
`accepted` entry; otherwise the approval gate fires, then `review_result`
(+ `accepted` on agreement, or a minted user-feedback finding on rejection).
There is no promotion step — the file was already real, and no `accept` field —
the verdict is derived from an empty backlog.

**User gate:** any `ask_user`, or the document-review gate
inside `_finalize_document` → `GateOrchestrator.fire_*` sends a `kind=request`,
registers a future, and awaits the client's `kind=response` (approvals also
persist `pending_prompt`; question batches don't — they re-drive from the
flushed `tool_use` on restart). `ask_user`'s batch renders as an in-feed
question panel in the client (the engine suppresses its `agent.tool_call` /
`agent.tool_call_detail` events); once confirmed the panel freezes and is
rebuilt on reload from the persisted call + result alone. The autonomous mode in force is
`SessionState.effective_autonomous`, frozen by the worker when it dequeues the
prompt; a user toggle mid-prompt updates `autonomous` (UI-facing) but only
takes effect at the next prompt. In autonomous mode `ask_user` is withheld
entirely and document review auto-accepts.

**Restart:** `GuideMarker` + `TransientStore` resume the session; an unanswered
approval `pending_prompt` is re-surfaced, while an unanswered question batch is
re-asked from scratch via the dangling-tool-use resume path. There is no index to rebuild — every
document's state is read from its own `.jsonl` log on demand (§7).

**Rollback:** Guide `rollback` → `tools/_rollback.handle` →
`EngineServices.rollback` → engine `_run_rollback` → directly
`RootMirrorManager.rollback` (the same primitive Problem Solver's
checkpoint-card "Rollback to this state" control uses) → engine resets the
in-memory conversation and starts fresh.

**Per-tool-call checkpointing + undo/rollback (both workflow modes, §10b/§12.1):**
a `filesystem`/`edit_file`/`create_file`/`create_directory`/`run_command` dispatch, in **either** mode, is
bracketed by `CheckpointCoordinator.prepare` (baselines the enclosing root's
`RootMirrorManager` mirror, scaffolding `.kodo/`+`kodo.md` lazily on first touch)
and `CheckpointCoordinator.commit` (commits the real tree, surfacing `{root, sha, parent}`
on `EVT_AGENT_TOOL_CALL_DETAIL` and `checkpoint_sha` in the tool's own result).
For `filesystem`/`edit_file`/`create_file`/`create_directory`, that same checkpoint also drives
`CheckpointCoordinator.record_guided_revision` (§7) when the affected path is tracked. The
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
| `scaffold_new_project` | ✅ Implemented and dispatched (guide + problem_solver); merges the former `create_new_project`/`init_project` tools. No `path`: scaffolds a new project dir + checkpoint mirror and adds it to the workspace. `path` given: augments an *existing* directory with `.kodo/`+checkpoint mirror, laying out `specs/`/`src/`/`test/` only when it's empty, and adds it to the workspace if not already there — or, if `path` already has a `.kodo/`, no-ops as `already_scaffolded` instead of erroring |
| Native file-IO / `run_command` / `read_file` tools | ✅ Implemented; granted to authoring sub-agents and `problem_solver` |
| Two user-facing workflows (`guided` Guide / `problem_solving` Problem Solver), plus the validator-only `judge` (Judge, almost entirely read-only plus one scoped `toolchain_build` exception — never sent by kodo-vsix) | ✅ Implemented; selected by `workflow.set` → `SessionState.workflow_mode` |
| `security` layer (allow/ask gate over every tool call, `prompt.permission`, PowerShell parser dialect, heuristic rule engine + Phase 2 persistent "always allow" rules) | ✅ Implemented (doc/SECURITY.md) |
| `state/_memory` | ⛔ Stub |
| `project/_manifest` | ◽ Parsed by `kodo.md`'s `## Toolchain` heading; purely informational now (no engine-side toolchain selection) |

---

## 17. Cross-cutting observations

1. **No in-memory index at all.** A document's state is the last line of its
   own `.jsonl` evolution log (`kodo.guided_state`, §7) — read fresh on every
   query (`guided_dev_status` re-walks the directory each call). There is
   nothing to construct at bootstrap, nothing to rebuild on rollback, and
   nothing shared across `ToolDispatcher` instances.
2. **One tool-dispatch surface, one generic loop.** `_run_agent_turn` is
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
