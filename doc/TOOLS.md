# Kōdo Tools — How Agent Tools Work, End to End

> How a tool goes from a static specification, to something an LLM can see and
> call, to executed Python, and back to the model as a result — and how every
> piece is wired together.

This document is the companion to [INTERNALS.md §6/§6A/§12](INTERNALS.md). It
covers the **tool subsystem** specifically: the split between a tool's
*specification* and its *implementation*, the dispatch machinery, and the full
request/response lifecycle through the LLM.

---

## 1. The two halves of a tool

Every tool is two things living in two packages:

| Half | Package | What it is | Example file |
|---|---|---|---|
| **Specification** (`ToolSpec`) | `kodo.toolspecs` | Pure data: the tool's name, the JSON Schema for its inputs, the description the model reads, and prompt-rendering metadata. No logic. | [_finalize_project.py](../src/kodo/toolspecs/_finalize_project.py) |
| **Implementation** (`Tool` subclass) | `kodo.tools` | One `Tool` subclass whose `handle(self, tool_input) -> str` does the work and returns a JSON string, reading collaborators off `self.context`. | [_finalize_project.py](../src/kodo/tools/_finalize_project.py) |

There is **one module per tool in each package**, and they share the tool's
short name (`finalize_project`). The two are bound together in exactly one
place — the dispatch table in [tools/_dispatch.py](../src/kodo/tools/_dispatch.py)
— so adding a tool is "add a spec, add a `Tool` subclass, add one row."

> **Why split them?** The spec is consumed by two unrelated readers: the LLM
> (which needs the schema + description) and the prompt renderer (which needs
> the human-facing metadata). The handler is consumed by the dispatcher. Keeping
> the spec as inert data lets `subagents` and `llms` depend on the catalog
> without dragging in dispatch logic (gates, `guided_state`, the engine).

---

## 2. The layering — where `kodo.tools` sits

`kodo.tools` is a dedicated import tier **between** `toolspecs` (T2) and
`subagents`/`llms` (T3):

```text
 T4  runtime              ← builds a ToolDispatcher per agent run; injects collaborators
        │  imports
        ▼
 T3  subagents · llms · tools      ← tools may import only ↓; imported only by runtime
        │
        ▼
 T2  toolspecs · security ← the ToolSpec catalog (pure data) and the security
        │                    layer over it (kodo.security → toolspecs + shellparser;
        ▼                    consumed only by runtime — doc/SECURITY.md)
 T1  transport
        │
        ▼
 T0  common · project · guided_state · state · websearch
```

**Hard rule:** `kodo.tools` may import only from T0/T1/T2 — in practice
`kodo.guided_state`, `kodo.project`, `kodo.websearch` (the Playwright- and
`curl_cffi`-backed fetch engine behind `query_search_engine`/`web_search` —
doc/WEB_SEARCH.md — and the single-page fetch behind `read_webpage` —
doc/READ_WEBPAGE.md), and
`kodo.toolspecs`. It must **never**
import `subagents`, `llms`, or `runtime`. The collaborators it needs from
higher tiers (the gate, the session, every engine-side operation) are
inverted into **structural Protocols** defined inside `tools` and injected by
`runtime` (see §5). Verify the ceiling:

```bash
grep -rE "^\s*(from|import) kodo\.(subagents|llms|runtime|server)" src/kodo/tools   # must be empty
```

---

## 3. Anatomy of a `ToolSpec`

[toolspecs/_spec.py](../src/kodo/toolspecs/_spec.py) defines the frozen
dataclass. Using `finalize_project` as the example:

```python
FINALIZE_PROJECT: ToolSpec = ToolSpec(
    name="finalize_project",              # the model calls the tool by this name
    external_name="Finalize Project",     # human label for the UI only
    user_description="Mark the project as done",   # short UI label for tool-call events
    description=(                          # what the MODEL reads to decide to call it
        "Terminal call: the project is complete.  "
        "Transitions state.phase to 'done' and ends the Guide session.\n\n"
        # When-to-use guidance lives HERE — `description` is the only prose
        # channel to the model. There is no separate `when_to_use` field.
        "When to use: all product-level stages have completed and the workspace "
        "has nothing left in flight — the project is done."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},  # JSON Schema
    output_schema={                        # reaches the model as a dense sketch (§7)
        "type": "object",
        "properties": {"status": {"type": "string", "description": "Always 'done'."}},
        "required": ["status"],
    },
    security_impact=SecurityImpact.LOW,    # engine-side gating only (§8)
    input_visibility={}, output_visibility={"status": "always"},
    autonomous_mode=None,                  # per-mode behavior (see §8)
    modifies_files=False,                  # prompt assembly only (see below)
)
```

Crucially, **not all fields reach the LLM the same way:**

- `name`, `description`, `input_schema` → sent to the model **as a tool
  definition** (the API `tools` parameter — see §6). These are the *only* fields
  the model ever sees.
- `output_schema` → reaches the model **only** through `description`, as the
  dense sketch `tool_description()` appends (see §7). An LLM tool definition has
  no output-schema field.
- `external_name`, `security_impact`, `autonomous_mode`, `user_description`,
  `modifies_files` → never seen by the model.
  `external_name`/`user_description` label **UI events** (`agent.tool_call`);
  `security_impact` drives engine-side gating; `autonomous_mode` drives
  per-mode tool filtering; `modifies_files` says a successful call can create,
  change, or delete files in the project tree, and is read only by
  `AgentRegistry` to decide whether an agent's prompt gets the shared
  `## Changing Files` block (INTERNALS.md). It is **not** a security signal —
  that is `security_impact` — and it is declared per spec rather than inferred
  from a name list, so a new file-touching tool cannot silently miss the
  editing discipline.

Because `description` is the single prose channel, it must carry everything the
model needs to *route* to this tool over a neighbouring one — that is why the
when-to-use guidance is written into it rather than a field of its own.

---

## 4. Wiring: how a `Tool` class is bound to a `ToolSpec`

A tool is a subclass of the `Tool` ABC ([tools/_tool.py](../src/kodo/tools/_tool.py))
with a fixed shape, e.g.
[tools/_finalize_project.py](../src/kodo/tools/_finalize_project.py):

```python
class FinalizeProjectTool(Tool):
    async def handle(self, tool_input: dict[str, object]) -> str:
        self.context.session.phase = "done"
        return json.dumps({"status": "done"})
```

The `Tool` base binds the run's context and exposes it read-only:

```python
class Tool(ABC):
    def __init__(self, context: ToolContext) -> None:
        self.__context = context            # name-mangled → _Tool__context

    @property
    def context(self) -> ToolContext:       # subclasses read collaborators here
        return self.__context

    @abstractmethod
    async def handle(self, tool_input: dict[str, object]) -> str: ...
```

> Subclasses read the context through `self.context`, **not** `self.__context` —
> the latter would name-mangle to `_<Subclass>__context` and miss the base's
> `_Tool__context`. This is the project's standard private-member-plus-read-only-property
> pattern.

The binding happens in **one table** in
[tools/_dispatch.py](../src/kodo/tools/_dispatch.py) — the single source of
truth pairing each dispatchable `ToolSpec` with its `Tool` subclass:

```python
_TOOL_CLASSES: tuple[tuple[ToolSpec, type[Tool]], ...] = (
    (READ_FILE,           ReadFileTool),
    (DOCUMENT_FEEDBACK,   DocumentFeedbackTool),
    (ASK_USER,            AskUserTool),
    ...
    (FINALIZE_PROJECT,    FinalizeProjectTool),
)
```

From that table, two lookups are derived:

```python
# name → Tool subclass   (used at dispatch time)
_CLASSES_BY_NAME: dict[str, type[Tool]] = {spec.name: cls for spec, cls in _TOOL_CLASSES}

# name → spec            (used to build the LLM-facing tool list for an agent)
DISPATCHABLE_TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec, _ in _TOOL_CLASSES}
```

So a `ToolSpec` and its `Tool` class are connected **only** through their shared
`name` plus this one `_TOOL_CLASSES` row. There is no decorator registry, no
import-time side effect, no name-string magic scattered around — adding a row is
the entire wiring step.

> A spec with **no** row here is "spec only": it can be rendered into a prompt
> but is silently dropped from the LLM-facing tool list, because
> `tools_for_agent` (§7) only returns specs present in
> `DISPATCHABLE_TOOLS_BY_NAME`. There are no such placeholders today — every
> spec in the catalog has a dispatch row, including `toolchain_build`/
> `toolchain_deps`, which used to be spec-only. `filesystem` is one row:
> a single `FilesystemTool` dispatches its six directory/delete/copy/move
> operations on its `operation` field; `create_file` (whole-file creation),
> `create_directory` (directory creation), and `edit_file` (targeted
> existing-file edit) are separate tools, as is the read-only `read_file`
> (whole file, line ranges, or a regex `pattern` with context lines).

---

## 5. What a tool reads: `ToolContext` + Protocols

Each `Tool` instance is constructed with one `ToolContext`
([tools/_context.py](../src/kodo/tools/_context.py)) and reads it through
`self.context`. The context carries the collaborators a tool might need **plus**
the per-run mutable state, and it is the seam that keeps `tools` from importing
`runtime`.

```python
@dataclass
class ToolContext:
    resolver: PathResolver        # T0 — project-confined or logical path resolution
    gate: GateLike                # Protocol — ask_user / approval gates (impl in runtime)
    session: SessionLike          # Protocol — .phase (finalize) + .effective_autonomous
    services: EngineServices      # Protocol — every engine-side op a tool can trigger
    agent_name: str               # the running agent (jsonl author/reviewer field)
    session_id: str
    mode: str = "problem_solving" # "guided" | "problem_solving" — frozen per prompt
    stop_requested: bool = False                             # set by return_result
    returned_output: dict[str, object] | None = None         # set by return_result

    @property
    def project_root(self) -> Path | None: ...   # -> services.project_root(), live
    @property
    def has_workspace(self) -> bool: ...          # -> services.has_workspace(), live
    @property
    def root_paths(self) -> tuple[RootPath, ...]: ...  # -> services.root_paths(), live
```

Note what is **not** here: there is no `autonomous` field. The mode a handler
honours is read live from `session.effective_autonomous` (frozen per prompt by
the engine — see §8), so no per-run snapshot can drift from the session.

`project_root`/`has_workspace`/`root_paths` are likewise **not** plain fields —
they are properties that call `self.services.project_root()`/`.has_workspace()`/
`.root_paths()` fresh on every access. One `ToolDispatcher` (and the one
`ToolContext` it owns) serves an entire agent turn's multi-round tool-call
loop, not just a single call — so if these were snapshotted once at
dispatcher-creation time, a project bound *mid-turn* (`scaffold_new_project`
succeeding partway through the same turn, or the user adding a
folder to the VS Code window by hand) would stay invisible to every other
tool call in that turn, even though the engine's own workspace state had
already moved on. That was a real bug (fixed 2026-07-21, when the tool was
still named `create_new_project` — since merged into `scaffold_new_project`'s
no-`path` branch): the call
would scaffold the directory and genuinely register it in
`SessionWorkspace.folders`, but every subsequent `requires_project` tool call
in the same turn still saw the stale `has_workspace=False` snapshot and
rejected with `NO_PROJECT_ERROR` — including retries of the call
itself, which kept "succeeding" (each creating a new sibling directory) while
nothing else could ever run. Reading live closes that gap for both the
gate check in `ToolDispatcher.dispatch` and the resolver (see the
`LogicalPathResolver` note in §5a below).

The three things a tool needs from *above* its tier are **structural
Protocols**, also defined in `_context.py`:

- **`GateLike`** — `fire_questions(questions, tool_call_id)` /
  `fire_approval(...)`. Runtime's
  [`GateOrchestrator`](../src/kodo/runtime/_gates.py) satisfies it by shape (no
  inheritance). `fire_questions` takes one `ask_user` batch
  (`{question, kind, options}` per entry) plus the calling `tool_use` id
  (read from `ToolContext.current_tool_use_id`, set by the dispatcher before
  each call) and returns one `{selected, free_text}` answer per question;
  approvals return an object satisfying the read-only `ApprovalLike` protocol.
- **`SessionLike`** — a settable `phase: str` plus a read `effective_autonomous:
  bool`. Runtime's `SessionState` matches.
- **`EngineServices`** — **one** protocol covering *every* engine-side operation
  a tool can delegate upward: `run_subagent(caller, name, task_input, max_rounds)`
  (one pass, or the whole author/critic loop when the named sub-agent declares
  a `critic:` — §5A),
  `run_dependency_manager(task_input)` (ungated `toolchain_depsmgr` spawn for
  `toolchain_deps`), `run_web_search_agent(task_input)` (ungated *silent,
  multi-round tool-calling* `web_search` agent turn — no subsession, since
  `web_search` is typically called from a sub-agent; doc/WEB_SEARCH.md),
  `rollback(...)`, `disable_autonomous_mode(...)`,
  `create_project(name)`, `init_project(path)`, `bootstrap_project(name)`, and the three **live
  workspace-state reads** `has_workspace()`, `root_paths()`, `project_root()`
  — synchronous, called fresh on every access (never memoized) by
  `ToolContext`'s same-named properties, backing the `EngineHost.
  _has_workspace`/`_root_paths`/`_project_root` methods 1:1. Runtime injects a
  single `_EngineServices` adapter (built inline in `runtime/_engine/`)
  wrapping the engine's private `_run_*` / `_disable_autonomous` /
  `_create_project` / `_init_project` / `_has_workspace` / `_root_paths` /
  `_project_root` methods. There is deliberately **no** `complete_artifact`-
  style method: the accept/review flow (`_finalize_document`) is purely
  engine-internal, triggered by the engine when a critic round leaves the
  document's findings backlog empty (`_record_findings`) — never through a tool
  or a protocol indirection.
  All three back the single `scaffold_new_project` tool, dispatched on which
  input the agent gave: no `path` and no workspace yet → `bootstrap_project`
  (resolves a workspace-home folder — interactively or, in autonomous mode,
  under `~/kodo-projects/` — then delegates to `create_project`); no `path`
  with a workspace already bound → `create_project`, requiring a non-empty
  `name`; `path` given → `init_project`. `create_project` slugifies the
  requested name, makes a fresh directory under the session workspace root
  (auto-suffixing on collision), scaffolds its `.kodo/`+mirror via
  `RootMirrorManager.prepare`, and pushes `EVT_WORKSPACE_ADD_FOLDER` so the
  extension adds it to the open VS Code workspace. `init_project` is the
  "augment an existing directory" counterpart: *path* must already exist
  (`ProjectLayout.init_existing` raises `ProjectLayoutError` otherwise); if
  `.kodo/` is already there — already a Kodo project — it's a no-op success
  (`already_scaffolded: true` in the tool's output), never an error. The
  directory is judged empty when it holds no entries besides
  dotfiles/dot-directories (`.git/`, `.gitignore`, ...), in which case — and
  only then — `specs/`/`src/`/`test/` are laid out, exactly like
  `create_project`; a non-empty directory keeps its content untouched. Either
  way (including the already-scaffolded no-op) `.kodo/`+mirror are scaffolded
  via the same `RootMirrorManager.prepare` (with its mandatory baseline
  commit), and `EVT_WORKSPACE_ADD_FOLDER` is only pushed when *path* isn't
  already one of the session's registered workspace folders.

This is the dependency inversion that lets the tool layer sit *below* the engine
while still calling back into it. `runtime` constructs the concrete objects and
hands them in; `tools` only ever names the Protocols. A handler reaches an engine
op as, e.g., `await self.context.services.run_subagent(self.context.agent_name, ...)`.
The spawning tools pass `self.context.agent_name` (the **running** agent — not a
hard-coded guide) as the `caller`; the engine gates the spawn against that
caller's frontmatter `subagents:` allow-list and raises `PermissionError` (which
the tool returns to the LLM as `{"error": ...}`) when the target is not permitted.

Per-run state lives on the context, not on the tool instance:
`ReturnResultTool` sets `self.context.returned_output`,
`EscalateBlockerTool` sets `self.context.stop_requested`. The dispatcher exposes
both back to the engine after the run.

---

## 5a. The `temporary` argument: session-scoped scratch files

Six file tools — `create_file`, `create_directory`, `edit_file`,
`filesystem`, `find_files`, `find_text_in_files` — accept an optional
`temporary: true` input alongside their usual `path`/`root`/`source`/
`destination` arguments. Every one of them resolves its path(s) through the
shared `Tool.resolve_path(path, *, temporary=...)` helper
([tools/_tool.py](../src/kodo/tools/_tool.py)) instead of calling
`self.context.resolver.resolve(...)` directly:

```python
def resolve_path(self, path: str, *, temporary: bool = False) -> Path:
    if temporary:
        return resolve_within(session_temp_dir(self.context.session_id), path)
    return self.context.resolver.resolve(path)
```

`session_temp_dir(session_id)` ([project/_layout.py](../src/kodo/project/_layout.py))
is `~/.kodo/sessions/<session_id>/tmp` — one scratch directory per session,
outside every project root and workspace folder. `resolve_within` (the same
helper Guided mode uses to confine paths to the project root) confines
relative paths inside it and rejects absolute paths that would escape it,
exactly like the ordinary resolver — a `temporary` call gets no *less*
containment, just a different root.

This is a **tool-level** mechanism, not a `ToolContext`/resolver change: the
active `PathResolver` (Guided or Problem Solver) is untouched, and a call
without `temporary` behaves exactly as before. Two other layers special-case
the same flag:

- **Security** ([SECURITY.md](SECURITY.md) §3.0a) — a `temporary: true` call
  on one of the six tools is always allowed, in every Command Control
  posture, before the usual impact/threshold judgement runs.
- **Checkpointing** — `CheckpointCoordinator.prepare` (§10 below) skips its
  mirror snapshot/commit outright for a `temporary` call, so nothing written
  there ever earns a checkpoint, an undo/rollback entry, or a Guided
  `new_revision` attribution.

Agents are told when to reach for this in `shared_editing.md`'s closing
paragraph — throwaway notes, intermediate files, and working copies that should
never land in the project — which reaches only agents holding a
`modifies_files` tool. The per-tool detail lives on each tool's own
`temporary` parameter description, which is where an agent actually reads it.

**Discovering the directory itself.** `get_root_paths` also takes an optional
`temporary: true` input ([toolspecs/_get_root_paths.py](../src/kodo/toolspecs/_get_root_paths.py),
[tools/_get_root_paths.py](../src/kodo/tools/_get_root_paths.py)). Instead of
the usual per-project root list it returns one `{"name": "scratch", "path":
...}` entry for `session_temp_dir(self.context.session_id)` (created eagerly
via `mkdir(parents=True, exist_ok=True)` so the path is guaranteed to exist).

**Echoing the resolved path back.** `create_file`'s result normally echoes
`path` back exactly as given, but under `temporary: true` it instead returns
the resolved absolute filesystem path (`str(target)` in
[tools/_create_file.py](../src/kodo/tools/_create_file.py)) — the agent
supplied only a relative path and has no other way to learn where the
scratch directory actually lives on disk, so the relative echo it used to
get back was useless for reuse (e.g. handing the file to another tool or
`run_command`). The absolute path returned is safe to reuse as `path` on a
later `temporary: true` call too, since `resolve_within` accepts an absolute
path that already resolves inside the scratch root.
This is how an agent gets the scratch directory's *absolute path* — e.g. to
pass as `run_command`'s `working_dir`. No resolver special-case is needed for
this any more (as of the 2026-07-24 multi-project rework, WS_PROTOCOL.md
§7.1c): `LogicalPathResolver` — now shared by both workflow modes, there is no
separate Guided-mode resolver — already accepts any absolute path
unconditionally, including the scratch directory, so a leaf sub-agent's
`run_command` reaches its own subsession scratch directory (the path
`get_root_paths temporary: true` reported for that same `session_id`) with no
`extra_roots` plumbing involved.

**`LogicalPathResolver` liveness.** `LogicalPathResolver`
([tools/_paths.py](../src/kodo/tools/_paths.py)) holds the live
`SessionWorkspace` object itself (`kodo.project.SessionWorkspace`, an allowed
T1 import for `kodo.tools`) rather than a copy of its folder map, and reads
`.folders`/`.physical_root` fresh inside `resolve()`/`default_cwd` rather than
at construction time. `SessionWorkspace.folders` always returns a defensive
*copy* of its current internal dict, so a resolver built at the start of a
turn — via `EngineHost._make_resolver` — still sees a folder registered
*after* construction, whether that came from `_create_project`'s synchronous
in-process update or a genuine `workspace.folders` WS push reconciling a real
VS Code `onDidChangeWorkspaceFolders` event (WS_PROTOCOL.md §5.9c / §7). This
is the resolver-side half of the `has_workspace` liveness fix described in §5.

**No workspace at all.** `default_cwd` raises `NoWorkspaceError` when nothing
is bound, so a `run_command` with no `working_dir` on a homeless session has
no directory to spawn in. Callers must not read the property directly:
`ToolContext.command_cwd` ([tools/_context.py](../src/kodo/tools/_context.py))
returns `default_cwd` when a workspace is bound and the session's private
scratch directory (`~/.kodo/sessions/<id>/tmp`) when none is. Both
`run_command`'s handler and `ToolDispatcher`'s security gate go through it, so
the directory the command runs in is the same one the permission prompt was
judged against — see doc/SECURITY.md §3.1a for why the scratch directory
(and not `$HOME`, `physical_root`, or the OS temp directory) is the right
fallback.

---

## 5A. Sub-agents as tools: `run_subagent_<name>` and `return_result`

A sub-agent is "a tool with agentic behavior": its
[`SubAgentSpec`](../src/kodo/subagents/_subagentspec.py) declares an
`input_schema` (what the caller supplies) and an `output_schema` (what it
returns via `return_result`), exactly as a `ToolSpec` does. Both of those
schemas reach the model as **real JSON Schema on a real tool definition** —
never as prose a prompt has to restate.

### One tool per sub-agent

A caller does not get a generic `run_subagent(name, task_input)` with an opaque
`task_input: {"type": "object"}`. It gets one tool per sub-agent it may invoke,
each declaring that sub-agent's own fields, flattened to the top level:

```
run_subagent_coder(instructions, input_paths, responsibility_code,
                   project_code, for_revision_path, max_rounds)
```

`AgentRegistry.run_subagent_specs(caller)` mints them from the caller's
`subagents:` allow-list. A **critic** (`role: critic`) is skipped: no caller
ever spawns one, so no tool is minted for it.

### What the sub-agent itself receives

Everything above is the *caller's* view. The sub-agent being spawned never
sees its own `input_schema` — not as JSON, not as prose. Its system prompt
(`AgentRegistry.get`) carries only a short, fixed pointer
(`_INPUT_PARAMETERS_NOTE` in `_registry.py`) saying where the real task lands;
there is no `## Your Task Contract` section and no schema dump.

The concrete values live instead in the first user turn, rendered fresh per
call by
[`_render_task_input`](../src/kodo/runtime/_engine/_subagents.py):
`instructions` becomes a `# Task` heading, and every other field the caller
actually supplied is pretty-printed under a trailing `## Input Parameters`
section — schema property order, each labeled with its `description` when the
spec declares one, nested dicts/lists rendered as markdown bullets rather than
a Python repr. That section is the *last* thing in the message and (since a
local model's chat template concatenates system prompt and first user turn
into one flat string) the last thing in the whole prompt — deliberately, so
the model reads it right before it has to act. It ends with the sub-agent's
only remaining prose explanation of `return_result` (the tool's own
`description` still carries the same text independently; see
`_return_result.py`).

Why render values instead of baking them into the system prompt: the
`AgentRegistry.get` system prompt is agent-*type*-scoped and does not vary by
call, which lets a local `llama.cpp`-served model reuse the KV cache for the
whole rendered prompt across every spawn of the same sub-agent. Which
`{SHARED:…}` blocks an agent includes is fixed in its `.md`, so nothing about
the prompt varies per call. Only the first user turn — necessarily per-call, since it carries
this call's actual values — varies.

### The canonical form

`RUN_SUBAGENT` still exists in the catalog, but is never offered to a model. It
is what every variant call is folded back to by
[`canonical_tool_call`](../src/kodo/tools/_dispatch.py), once, at the top of
`_dispatch_tool_calls`:

```
("run_subagent_coder", {"instructions": "...", "max_rounds": 3})
  → ("run_subagent", {"name": "coder",
                      "task_input": {"instructions": "..."},
                      "max_rounds": 3})
```

Everything downstream of that line — the security gate, checkpoint
prepare/commit, the tool-call logger, the tool-call card and its detail rows,
and crash-resume's re-dispatch ledger — stays keyed on the single catalog
entry, so adding a sub-agent adds no work anywhere else. `max_rounds` is
lifted *out* of the flattened input: it is the engine's loop budget, not part
of the sub-agent's task.

**One exception: the schema used for `schema_compliance`.** `RUN_SUBAGENT`'s
own `output_schema` is a bare, propertyless placeholder (`{"type": "object",
"description": ...}` — the real shape varies per sub-agent, see above), and
`normalize_output` reports `compliant=True` unconditionally against a schema
with no declared properties or required fields. Validating a sub-agent's raw
result against that placeholder would silently launder a genuine
`{schema_compliance: False}` — a sub-agent that never called `return_result`
at all, see *An author's escalation* below and `_drive_subsession`'s fallback
— into a compliant-looking, content-free result by the time it reaches the
calling LLM, even though the `subsession_end` marker (and the red `<kodo_crit>`
"subagent failed to complete the task" banner it drives in kodo-vsix)
correctly recorded the subsession as failed. `_finalize_tool_result`
(`_turns.py`) special-cases `tool_name == RUN_SUBAGENT.name`: it looks up the
target's own `run_subagent_<name>` variant via
`AgentRegistry.run_subagent_specs(agent_name)` — the exact schema that
sub-agent's caller was shown, review-block-merged when it has a critic — and
validates against that instead, falling back to the canonical placeholder only
if the variant can't be found. (Traced in session `1785719012`: `toolchain_builder`
did all its real work correctly, then ended its turn with a plain-text summary
instead of calling `return_result`; the caller's tool result read
`{"schema_compliance": true}` with none of the actual data, even though the
subsession had already been marked `failed`. Fixed 2026-08-02; see also
doc/STUCK_DETECTION.md §2.8 for the companion hardening that gives a sub-agent
in exactly this situation one nudge to call `return_result` before the engine
gives up on it.)

### A call may be a whole review loop

Whether one call is one pass or a full author/critic loop is decided by the
**callee's** frontmatter, never by the caller. A sub-agent that declares
`critic: <name>` gets the loop contract: its tool takes an optional
`max_rounds` (default 5, hard cap 10) and its declared output carries a
`review` block. `_run_review_loop` then spawns the author, hands its
`primary_path` to the critic, and re-runs the author with **identical
`instructions`** and `for_revision_path` set — the findings themselves are never
rendered into the task; both halves read them through `get_findings`
(doc/FINDINGS.md) — until:

| `review.outcome` | Meaning |
| --- | --- |
| `accepted` | the log says `accepted`/`pending_acceptance`; the file is settled |
| `max_rounds` | budget spent with findings still outstanding (`review.outstanding` counts them) |
| `escalated` | the author returned a non-empty `reason`: a blocker no revision fixes. The critic is not spawned and no further round is spent (see *An author's escalation* below) |
| `not_converging` | a round closed nothing *and* opened nothing — exact no-progress, so the engine stopped early rather than orbit to the cap |
| `not_reviewed` | the author reported no `primary_path` to review |

The **stores are authoritative**, not the critic's return value: the user's own
review decision lands in them too (see *A critic's findings* below) and can turn
an accepted file back into one needing revision, which the loop then spends
another round on.

### An author's escalation

`return_result` is a sub-agent's **only** way out, so it carries both terminal
outcomes. An author blocked on something it cannot defensibly resolve — inputs
that leave a required decision undetermined, two documents that contradict each
other, an exhausted iteration cap — returns an **escalation** instead of a
normal result: a non-empty `reason` (a short identifier of the blocker), the
blocking `summary` in place of the usual "what I produced" line, and `options`
when the decision is between discrete alternatives.

The fields are built by
[`author_output()`](../src/kodo/subagents/specs/_shapes.py) and reach the model
on `return_result`'s `result` parameter like everything else. The prompt half is
the shared `shared_escalation.md` block, opted into per agent by including
`{SHARED:escalation}` in its body — the two must ship together, and a test
asserts they do.
Only `summary` is *schema*-required for such an author: one blocked before it
wrote anything has no `primary_path`, and a backfilled required field would mark
the escalation `schema_compliance: false` — the engine's "this sub-agent failed"
signal, which an escalation is not.

Nothing dispatches: the escalation *is* the sub-agent's return, so it reaches
the caller as that `run_subagent_<name>` call's result. `_run_review_loop`
checks it via `_escalation_reason()` (non-empty `reason` — emptiness is the
test, since `normalize_output` backfills with `""`) and stops on the spot with
`review.outcome: "escalated"`. Resolving it belongs to the **caller**, not the
engine: the Guide puts it to the user with `ask_user` (which self-resolves,
per §8, when nobody is there to answer) and sends the resolution back by
re-running the stage with it written into `instructions`.

> This replaces the retired `escalate_blocker` tool, which set `stop_requested`
> without ever setting a result — so a blocked sub-agent handed its caller
> nothing but a `{schema_compliance: false}` failure, and its interactive
> question gate asked the user directly from inside a subsession the caller
> could not see.

### A critic's findings

`return_result` is specialized per agent — `result` is bound to that agent's
`output_schema` — so a critic's terminal call *is* its round:
`{path, findings, summary}`. There is no separate reporting tool; the retired
`document_feedback` declared the old shape, and having a critic report the same
review twice was the redundancy this replaced.

**There is no `accept` field.** The verdict is *derived*: a document is accepted
when the round leaves nothing outstanding. A critic therefore returns evidence
and the engine draws the conclusion, which removes the whole class of "accepted,
concerns attached" results and makes the two impossible to disagree.

Each entry of `findings` is either a **new** finding (no `id` — the engine mints
one) or an **update** to an existing one (`id` plus only the fields that
changed; `state: "fixed"` closes it). A finding the round does not mention keeps
its state — **silence closes nothing**. Full protocol: doc/FINDINGS.md §3.

The engine applies them in `_drive_subsession`, gated on the callee's explicit
`role: critic` (never inferred from the result's shape), by calling
`_record_findings`: apply the updates to that document's session-scoped findings
log, close the round with a `review_round` entry, then — when nothing is left
outstanding — drive `_finalize_document`, which auto-accepts in autonomous mode
or under Edit Control *Allow All*, and otherwise fires the user's sign-off gate
(whose rejection comment is itself minted as a finding). Applying them at
subsession end rather than mid-run covers both a fresh spawn and one resumed
after a crash, while a *completed* subsession replayed from the ledger returns
its stored result without re-running and so cannot double-write.

Each critic's **concern vocabulary is prose** in its own prompt
(`### Concern vocabulary`), not a schema `enum`: the catalogue needs per-kind
explanations and routing rules a bare enum cannot carry, and nothing ever
enforced the enum anyway (`normalize_output` validates declared keys and
required fields, never value constraints). The `kind` field's description
points at that section.

### The other side: `get_findings`

The read half of the same protocol, granted to all 8 authors and all 7 critics.
It is **auto-scoped** — no path argument — because the engine binds the round's
target document to the run (`ToolContext.findings_path`, threaded from
`_run_review_loop` through `_spawn_subagent` → `_drive_subsession` →
`_make_dispatcher`). Outside a review round, or on an author's first pass, the
scope is empty and the tool returns `{"findings": []}`: a normal answer, not an
error, which is what lets one prompt be correct on pass 1 and pass N.

The findings *directory* is injected alongside the scope rather than derived by
the tool, because `ToolContext.session_id` holds the **subsession** id inside a
sub-agent run — no tool could resolve the session's own store path for itself.

The prompt half is two shared blocks, `{SHARED:findings_author}` and
`{SHARED:findings_critic}`, and the registry enforces the pairing exactly as it
does `{SHARED:editing}`: an agent granted `get_findings` must include exactly
one of them, and neither block may appear without the grant.

---

## 6. How tools reach the LLM, and how a call comes back

Tools are passed to the model **as a separate API parameter**, never embedded in
the message text. In [llms/anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py),
each `ToolSpec` is converted to an Anthropic tool definition:

```python
tool_defs = [
    {"name": t.name, "description": tool_description(t), "input_schema": t.input_schema}
    for t in tools
]
...
self.__client.messages.stream(model=..., system=..., messages=..., tools=tool_defs)
```

Only **three** fields cross the wire to the model: `name`, `description`,
`input_schema` — and this is the model's *only* source of tool knowledge, since
agent prompts describe no tools (§7). The description is built by
[`tool_description(spec)`](../src/kodo/toolspecs/_describe.py) rather than read
off `spec.description` directly, so the `output_schema` — which the API has no
field for — travels along as a dense sketch (§3). The llama.cpp plugin and the
request logger call the same helper, so all three agree byte for byte.

When the model decides to use a tool, it emits a `tool_use`
content block, which the plugin assembles into a provider-agnostic
[`ToolCallEvent`](../src/kodo/llms/_interface.py):

```python
ToolCallEvent(tool_use_id="toolu_…", tool_name="finalize_project", tool_input={...})
```

> Both providers (Anthropic and llama.cpp) are **stateless**: the `tools` list
> is re-sent on every `stream_query` call. The model has no memory of prior tool
> definitions — only the `messages` array (with `tool_use`/`tool_result` blocks)
> carries history.

---

## 7. Which tools an agent gets: frontmatter → `tools_for_agent`

There is **one unified tool surface** — no guide-vs-leaf split. Every
agent (the guide included) is granted exactly the tools its frontmatter
`tools:` list declares. Two consumers turn that list into reality:

**(a) The LLM-facing tool list.** The engine calls
[`agent_tool_specs(registry, agent)`](../src/kodo/runtime/_agenttools.py), which
resolves the declared names through
[`tools_for_agent`](../src/kodo/tools/_dispatch.py):

```python
def tools_for_agent(
    tool_names: frozenset[str],
    replacements: Mapping[str, Sequence[ToolSpec]] | None = None,
) -> list[ToolSpec]:
    ...  # a name in `replacements` expands to those specs; otherwise catalog lookup
```

It takes **tool names** (`frozenset[str]`), not a `SubAgent` — that would be an
upward import into T3. Names with no handler are skipped. Each surviving spec is
serialized by `tool_description()` (§3) into the API `tools` parameter.

Two names never resolve from the catalog, because their real schema is only
knowable once you know *which* agent is running (§5A):

| Declared name | Expands to |
| --- | --- |
| `run_subagent` | one `run_subagent_<name>` tool per sub-agent this agent may invoke, each declaring **that sub-agent's** `input_schema` inline |
| `return_result` | the same tool with `result` bound to **this agent's** `output_schema` |

Both expansions come from `AgentRegistry` (`run_subagent_specs` /
`return_result_specs`), which lives in `kodo.subagents` — a *sibling* of
`kodo.tools` at T3, so neither may import the other. `agent_tool_specs` is the
join, one tier up in `runtime`, and it is the **only** place any caller builds a
tool payload: the live turn loop, crash-resume, sub-agent subsessions, the
silent engine-driven turns, and `kodo --tools` all go through it, so the surface
a model sees cannot differ by code path.

**(b) Load-time validation.** Independently,
[subagents/_registry.py](../src/kodo/subagents/_registry.py) checks every
declared name against `ALL_TOOLS` when the registry is built, so a typo in
`tools:` fails fast rather than at first dispatch.

> **There is exactly one channel to the model.** Agent prompts do **not** contain
> a `## Tools` section — it was removed, along with the `{PLACEHOLDER:TOOLS}`
> token that filled it. Describing tools in both places duplicated
> `description`/`input_schema` for no benefit and cost 15–53% of every system
> prompt. The four things that section carried and the `tools` argument did not
> were resolved instead of dropped: `when_to_use` was merged into each spec's
> `description`; `output_schema` reaches the model as the dense sketch
> `tool_description()` appends; `security_impact` and `autonomous_mode` are
> engine-side concerns the model was never required to reason about. Net effect:
> agent system prompts shrank ~16.6% overall with no loss of routing guidance.
> **Do not reintroduce a prompt-side tool section** — put the guidance in
> `description`, where it reaches every agent granted the tool automatically.

To see for yourself what an agent's prompt does and does not contain, print the
real thing:

```bash
python -m kodo --system-prompt guide --model claude-opus-5
```

That CLI ([__main__.py](../src/kodo/__main__.py), INTERNALS.md §9a) renders
through `AgentRegistry.get` — the same call the engine makes — so its output is
the prompt byte for byte. `test/test_main.py` runs it over every packaged agent
and asserts none of them grew a `## Tools` section. The same CLI's `--tools`
command prints the other half — the agent's `tools=[...]` payload exactly as
submitted to the OpenAI-compatible client — via the same production plumbing
(`kodo.llms.llamacpp.build_openai_tools`).

---

## 8. Autonomous mode

Filtering for autonomous mode happens **once**, in the agent registry — not in
the tool layer. A spec whose `autonomous_mode` contains `"unavailable"` is
dropped from the agent's `.tools` set when `registry.get(name, autonomous=True)`
is called. Because the engine builds the LLM tool list from the
*already-filtered* `agent.tools`, the withheld tool simply never reaches the
model — there is no prompt-side tool list that could contradict it. No
packaged tool declares `"unavailable"` today (`_AUTONOMOUS_DISABLED` in
`subagents._registry` is currently empty) — the mechanism stays available for
a tool that genuinely has no synthesizable answer, but the one tool that used
to use it (`ask_user`) has since moved to the pattern below instead.

A tool can also declare `autonomous_mode="auto-accepted …"` for a spec whose
*handler* short-circuits on `ctx.session.effective_autonomous` and synthesizes
its response instead of blocking on the gate. `ask_user` is the example:
`AskUserTool.handle` checks `effective_autonomous` itself and, with no user to
answer, returns a synthesized answer per question — a `single_choice`
question's first option (the agent's own stated best guess), a `multi_choice`
question's `free_text` set to a fixed notice telling the agent nobody is there
and it should decide for itself — instead of firing `fire_questions` and
blocking. This is deliberate: agent prompts call `ask_user` unconditionally and
never branch on mode themselves (see `ASK_USER.description` and its `answers`
output description for how they're expected to read a synthesized answer). The former `request_user_review_artifact` used the same idea before
it moved into the engine outright: `_finalize_document` (triggered by
`_record_findings` when a critic round leaves nothing outstanding, not by a
dispatched tool) checks `effective_autonomous` — and Edit Control `allow_all` —
itself, and either auto-accepts or fires the gate.

> **Where `effective_autonomous` comes from.** The user-facing toggle sets
> `SessionState.autonomous`, but the engine *freezes* that into
> `effective_autonomous` once per prompt (when the worker dequeues it), so a
> mid-prompt toggle never splits a running prompt's mode. Every tool and the
> registry read `effective_autonomous`; the dispatcher therefore needs no
> `autonomous` argument at all.

---

## 8A. The `intent` parameter — mutating tools declare their purpose

Every **first-degree mutator** — a tool whose own dispatch changes content on
disk: `filesystem`, `edit_file`, `create_file`, `create_directory`,
`run_command`, `scaffold_new_project`, `rollback` — declares a
mandatory `intent` string as the **first** property of its `input_schema`: one sentence
stating what this specific call changes and why. The property (and the generic "how to state
your intent" guidance the model reads) is defined **once**, in
[toolspecs/_intent.py](../src/kodo/toolspecs/_intent.py) (`INTENT_PROPERTY`),
and embedded by each mutating spec, so the instructions can never drift
between tools.

- **Exempt:** tools that mutate only *through other agents* —
  `run_subagent` (in any of its `run_subagent_<name>` forms), `toolchain_deps` — because
  the spawned agent's own first-degree calls carry their own intents;
  `toolchain_build` (it only executes the project's generated build scripts);
  and everything read-only or session-state-only.
- **Enforcement:** `ToolDispatcher.dispatch` generically rejects a call to any
  spec that requires `intent` (`requires_intent(spec)`) when the field is
  missing or blank — the handler never runs; the model gets an `{"error": …}`
  telling it to state the intent and retry.
- **Visibility:** `intent` is declared `"always"` visible and, as the first
  schema property, renders as the top row of the WebView's tool-call detail
  box.
- **Consumer:** the security layer ([doc/SECURITY.md](SECURITY.md)). In SMART
  Command Control every HIGH-impact call goes through a one-shot LLM *intent
  judge* that matches the declared intent against the parameters: a clean
  match on a benign step auto-allows; anything else asks the user via
  `prompt.permission`. `run_command` is additionally analyzed statically
  first — a target provably outside the workspace always asks, a provably
  read-only in-workspace command always passes.

---

## 9. The dispatcher

[`ToolDispatcher`](../src/kodo/tools/_dispatch.py) is built **once per agent
run** by the engine. It owns the run's `ToolContext` and routes calls:

```python
class ToolDispatcher:
    def __init__(self, *, resolver, gate, session, services,
                 agent_name, session_id, security=None,
                 mode="problem_solving", project_root=None):
        self.__ctx = ToolContext(...)            # one context for the whole run

    @property
    def stop_requested(self) -> bool: ...        # read by the engine after each tool batch
    @property
    def returned_output(self) -> dict[str, object] | None: ...   # set by return_result

    async def dispatch(self, tool_name, tool_input, tool_use_id="") -> str:
        tool_cls = _CLASSES_BY_NAME.get(tool_name)
        if tool_cls is None:
            return json.dumps({"error": f"Unknown tool: {tool_name!r}"})
        # 1. intent presence (§8A)   2. security gate (doc/SECURITY.md)
        denial = await self.__security_gate(tool_name, tool_input, tool_use_id)
        if denial is not None:
            return denial                        # user denied — handler never runs
        return await tool_cls(self.__ctx).handle(tool_input)   # bind context, then run
```

`dispatch` is the single function the engine passes into its turn loop as the
`tool_dispatch` callback. Two generic gates run before the handler: a spec
that requires `intent` (§8A) never dispatches without a non-blank one, and the
**security layer** judges every call — an `ask` verdict fires the
`prompt.permission` gate and a user denial returns an error result without
executing the tool (doc/SECURITY.md). It then instantiates the matching
`Tool` subclass bound to this run's context and calls its `handle`. Whether
the caller is the guide or a leaf sub-agent, the routing is identical — only
the *contents* of the context and the *set* of tools differ.

---

## 10. The engine turn loop — putting it together

[runtime/_engine/](../src/kodo/runtime/_engine/) drives the generic loop
(`_run_agent_turn`), shared by the guide and every leaf agent. Per run:

1. Resolve the agent (`registry.get(name, autonomous)`), which yields its
   filtered `tools` and rendered system prompt.
2. Build the dispatcher: `dispatcher = self._make_dispatcher(agent_name, session_id)`
   — injecting the gate, session, the single `_EngineServices` adapter, and
   `mode`/`project_root` (read live from the current workflow mode and bound
   project, independent of each other — a Problem-Solver run still carries
   `project_root` if a project happens to be bound). No `autonomous` flag is
   passed; tools read `session.effective_autonomous`.
3. Call `_run_agent_turn(..., tools=tools_for_agent(agent.tools),
   tool_dispatch=dispatcher.dispatch, stop_after_tools=lambda: dispatcher.stop_requested)`.

Inside the loop:

```text
┌────────────────────────────────────────────────────────────────────────┐
│ while True:                                                             │
│   stream = llm.stream_query(system, messages, tools=<ToolSpec list>)   │
│   collect TokenDelta → text, ToolCallEvent → tool_calls, TurnEnd       │
│                                                                        │
│   if no tool_calls:  append assistant text to messages; BREAK          │
│                                                                        │
│   append assistant message (text + tool_use blocks) to messages        │
│   for each tool call:                                                   │
│       emit EVT_AGENT_TOOL_CALL  (UI: name + user_description)           │
│       result_text = await tool_dispatch(name, input)   ← ToolDispatcher │
│       collect {"type":"tool_result","tool_use_id":id,"content":result} │
│   append one user message carrying all tool_result blocks to messages  │
│                                                                        │
│   if stop_after_tools():  BREAK     ← e.g. return_result fired          │
└────────────────────────────────────────────────────────────────────────┘
```

So the handler's returned **JSON string becomes the `content` of a
`tool_result` block**, fed back to the model on the next iteration. The model
reads it, reasons, and either calls more tools or ends its turn. After the loop,
the engine reads `dispatcher.returned_output` (what a leaf returned via
`return_result`) and used `dispatcher.stop_requested` to decide early exit. A
mutating tool call (`filesystem`/`edit_file`/`create_file`/`create_directory`) is additionally bracketed by
`CheckpointCoordinator.prepare`/`CheckpointCoordinator.commit` (§12.1 in INTERNALS.md) — outside
this loop, around the `tool_dispatch` call — so every dispatch in this diagram
that touches a file also earns a mirror commit. The one exception: a call
made with `temporary: true` (§5a) is skipped by `prepare` outright and never
earns one, since it never touches the project at all.

---

## 11. Full end-to-end sequence

A concrete trace of the guide calling `run_subagent`, which spawns a leaf
author that writes a file:

```text
 LLM (guide)                  Engine / ToolDispatcher              tools/ Tool classes
 ──────────────────                  ───────────────────────              ───────────────────
   │  tool_use: run_subagent ──────────►  dispatch("run_subagent", …)
   │                                        └─► RunSubagentTool(ctx).handle(…)
   │                                              └─► self.context.services.run_subagent(name, …)
   │                                                    │  (_EngineServices adapter)
   │                                                    ▼
   │                                        engine._run_subagent: builds a NEW
   │                                        ToolDispatcher for the leaf, runs its turn
   │                                                    │
   │                          leaf LLM  tool_use: create_file(path, content) ─► dispatch(…)
   │                                                    └─► CreateFileTool(ctx).handle(…)
   │                                                          └─► writes the real file on disk
   │                                        (engine, outside the tool) commits the mirror,
   │                                        appends a new_revision jsonl entry (§7, INTERNALS.md)
   │                          leaf LLM  tool_use: return_result({"primary_path": "specs/a.md", …})
   │                                                    └─► self.context.returned_output = {...}
   │                                        leaf turn ends → returned_output = {"primary_path": …}
   │                                                    ▼
   │  tool_result: {"primary_path": "specs/a.md", …} ◄──  json.dumps(returned_output)
   │  …reasons, calls next tool…
```

`ask_user` follows the same path but its handler branches on
`ctx.session.effective_autonomous` first (§8): in an interactive session it
`await`s `ctx.gate.fire_*`, which sends a `kind=request` frame to the VS Code
client and blocks on a future until the user responds — see
[INTERNALS.md §15 "User gate"](INTERNALS.md) — while in an autonomous one it
never touches the gate at all and returns a synthesized answer immediately.

`ask_user` carries a **question batch** — every open question about the
agent's current topic in one call, each with the candidate answers the agent
derived itself (top choice first; the client appends the free-text option, so
specs never include an "Other"). The discipline lives in the spec's own
`description` — so it reaches exactly the four agents granted the tool, not
every agent — and not in per-agent prompts. The client renders the batch as an
interactive **in-feed question panel** rather than a tool-call card (the
engine suppresses `agent.tool_call`/`agent.tool_call_detail` for `ask_user`):
the user navigates the boxes, revises selections freely, and answers land
only on *Confirm and Send*. The confirmed panel freezes read-only and is
rebuilt after a reload purely from the persisted `tool_use` (questions) +
`tool_result` (answers) — only the tool call and its result ever reach LLM
context. A crash mid-answer re-drives the whole batch (SESSIONS.md).

---

## 12. Adding a new tool — checklist

1. **Spec** — create `src/kodo/toolspecs/_<tool_name>.py` exporting one
   `ToolSpec` (with `input_schema`, `output_schema`, `security_impact`, and a
   model-facing `description` that **ends with a `When to use: …` paragraph** —
   there is no `when_to_use` field, and `description` is the only prose the model
   sees; optional `autonomous_mode`). Add it to
   `toolspecs/__init__.py` imports / `__all__` / `ALL_TOOLS`. If the tool
   mutates content directly (a first-degree mutator, §8A), embed
   `INTENT_PROPERTY` from `toolspecs/_intent.py` as the **first**
   `input_schema` property, list `intent` first in `required`, and mark it
   `"always"` in `input_visibility` — the dispatcher's enforcement keys on
   the `required` entry.
2. **Tool class** — create `src/kodo/tools/_<tool_name>.py` with a
   `class <Name>Tool(Tool)` implementing
   `async def handle(self, tool_input: dict[str, object]) -> str`.
   Read collaborators via `self.context`; return a JSON string.
3. **Wire** — add one `(SPEC, <Name>Tool)` row to `_TOOL_CLASSES` in
   [tools/_dispatch.py](../src/kodo/tools/_dispatch.py), and export the class
   from `tools/__init__.py`. (The row is the *only* binding step — both
   `_CLASSES_BY_NAME` and `DISPATCHABLE_TOOLS_BY_NAME` derive from it.)
4. **Grant** — add the tool name to the relevant agent's frontmatter `tools:`
   list in `src/kodo/subagents/subagent_<agent>.md`.
5. If the handler needs a new collaborator from above its tier, add a **Protocol**
   to `tools/_context.py` and a field to `ToolContext`; inject the concrete
   implementation from the engine's `_make_dispatcher`.

Do **not** import `subagents`, `llms`, or `runtime` from the handler.

---

## 13. File reference

| File | Role |
|---|---|
| [toolspecs/_spec.py](../src/kodo/toolspecs/_spec.py) | The `ToolSpec` dataclass. |
| [toolspecs/_intent.py](../src/kodo/toolspecs/_intent.py) | The shared mandatory `intent` property for first-degree mutating tools + `requires_intent` (§8A). |
| [toolspecs/_<tool>.py](../src/kodo/toolspecs/) | One `ToolSpec` constant per tool (pure data). |
| [toolspecs/__init__.py](../src/kodo/toolspecs/__init__.py) | Re-exports specs + `ALL_TOOLS` (for prompt rendering). |
| [tools/_context.py](../src/kodo/tools/_context.py) | `ToolContext` + the injected Protocols (`GateLike`, `SessionLike`, `EngineServices`, `ApprovalLike`). |
| [tools/_tool.py](../src/kodo/tools/_tool.py) | The `Tool` ABC: binds a `ToolContext` (read-only `context` property), declares abstract `handle`, and provides `resolve_path` (§5a — the ordinary resolver, or the session scratch directory when `temporary`). |
| [tools/_&lt;tool&gt;.py](../src/kodo/tools/) | One `Tool` subclass per tool, with `handle(self, tool_input) -> str`. |
| [tools/_dispatch.py](../src/kodo/tools/_dispatch.py) | `_TOOL_CLASSES` table, `ToolDispatcher`, `tools_for_agent`, `DISPATCHABLE_TOOLS_BY_NAME`. |
| [tools/_paths.py](../src/kodo/tools/_paths.py) | `resolve_within` path guard (file-I/O + shell). |
| [tools/_document_status.py](../src/kodo/tools/_document_status.py) | `document_status()` — merges a document's project-scoped evolution log with its session-scoped findings backlog (doc/FINDINGS.md §6). Lives here because `tools` is the lowest tier that may import both leaf packages. |
| [findings/](../src/kodo/findings/) | The per-session author/critic findings backlog `get_findings` reads and the engine writes (doc/FINDINGS.md). A leaf package, so `tools` may import it. |
| [project/_layout.py](../src/kodo/project/_layout.py) | `session_temp_dir(session_id)` — `~/.kodo/sessions/<id>/tmp`, the `temporary` scratch root (§5a). |
| [skills/](../src/kodo/skills/) | `SkillStore`/`load_skill`/`render_catalog` — the user-installed Agent Skills `use_skill` reads (doc/SKILLS.md). A leaf package, so `tools` may import it. |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | Validates each agent's `tools:` frontmatter against `ALL_TOOLS`; autonomous filtering. Renders no tool text into the prompt. |
| [toolspecs/_describe.py](../src/kodo/toolspecs/_describe.py) | `tool_description()` — prose + dense `output_schema` sketch; the only tool text the model reads. |
| [llms/anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | Converts `ToolSpec` → API `tools` param; parses `tool_use` → `ToolCallEvent`. |
| [llms/_interface.py](../src/kodo/llms/_interface.py) | `Message`, `ToolCallEvent`, `TurnEnd`, the `stream_query` contract. |
| [runtime/_engine/](../src/kodo/runtime/_engine/) | `_make_dispatcher`, `_run_agent_turn` (the tool loop), the `_EngineServices` adapter. |
| [runtime/_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator` (satisfies `GateLike`). |

See also [INTERNALS.md §6A](INTERNALS.md) for the package's place in the
dependency graph, and [CLAUDE.md](../CLAUDE.md) for the import-layer rule.
