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
`kodo.guided_state`, `kodo.project`, `kodo.websearch` (the Playwright
discovery/scraping engine behind `web_search` — doc/WEB_SEARCH.md — and the
single-page Markdown fetch behind `read_webpage` — doc/READ_WEBPAGE.md), and
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
    external_name="Finalize Project",     # human label for prompt heading + UI
    user_description="Mark the project as done",   # short UI label for tool-call events
    description=(                          # what the MODEL reads to decide to call it
        "Terminal call: the project is complete.  "
        "Transitions state.phase to 'done' and ends the Guide session."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},  # JSON Schema
    when_to_use=(                          # rendered into the agent's `## Tools` prompt
        "All product-level stages have completed and no tracked document is "
        "left pending — the project is done.",
    ),
    autonomous_mode=None,                  # per-mode behavior (see §8)
)
```

Crucially, **not all fields reach the LLM the same way:**

- `name`, `description`, `input_schema` → sent to the model **as a tool
  definition** (the API `tools` parameter — see §6).
- `external_name`, `when_to_use`, `autonomous_mode` → rendered into the
  **system prompt's `## Tools` section** by the agent registry (see §7).
- `user_description` → used only for **UI events** (`agent.tool_call`), never
  seen by the model.

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
> a single `FilesystemTool` dispatches all eight file/directory operations
> (create/delete/copy/move × file/dir) on its `operation` field; `edit_file`
> stays a separate tool, as does the read-only `read_file` (whole file, line
> ranges, or a regex `pattern` with context lines).

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
    project_root: Path | None = None  # the bound project's root, if any
    stop_requested: bool = False                             # set by escalate_blocker
    returned_output: dict[str, object] | None = None         # set by return_result
```

Note what is **not** here: there is no `autonomous` field. The mode a handler
honours is read live from `session.effective_autonomous` (frozen per prompt by
the engine — see §8), so no per-run snapshot can drift from the session.

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
  a tool can delegate upward: `run_subagent(caller, ...)`,
  `run_dependency_manager(task_input)` (ungated `toolchain_depsmgr` spawn for
  `toolchain_deps`), `run_web_summarizer(task_input)` (ungated *silent*
  `web_summarizer` turn for `web_search` phase 3 — titler-style, no
  subsession; doc/WEB_SEARCH.md), `run_author_critic_iteration(caller, ...,
  path, input_paths, instructions,
  for_revision)`, `rollback(...)`, `disable_autonomous_mode(...)`, and
  `create_project(name)`. Runtime injects a single `_EngineServices` adapter
  (built inline in `_engine.py`) wrapping the engine's private `__run_*` /
  `__disable_autonomous` / `__create_project` methods. There is deliberately
  **no** `complete_artifact`-style method: the accept/review flow
  (`__finalize_document`) is purely engine-internal, triggered from a
  post-dispatch hook after a `document_feedback` call — never through a tool
  or a protocol indirection. `create_project` is what backs the
  `create_new_project` tool: the engine slugifies the requested name, makes a
  fresh directory under the session workspace root (auto-suffixing on
  collision), scaffolds its `.kodo/`+mirror via `RootMirrorManager.prepare`,
  and pushes `EVT_WORKSPACE_ADD_FOLDER` so the extension adds it to the open
  VS Code workspace.

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

## 6. How tools reach the LLM, and how a call comes back

Tools are passed to the model **as a separate API parameter**, never embedded in
the message text. In [llms/anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py),
each `ToolSpec` is converted to an Anthropic tool definition:

```python
tool_defs = [
    {"name": t.name, "description": t.description, "input_schema": t.input_schema}
    for t in tools
]
...
self.__client.messages.stream(model=..., system=..., messages=..., tools=tool_defs)
```

Only **three** spec fields cross the wire to the model: `name`, `description`,
`input_schema`. When the model decides to use a tool, it emits a `tool_use`
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
[`tools_for_agent(agent.tools)`](../src/kodo/tools/_dispatch.py):

```python
def tools_for_agent(tool_names: frozenset[str]) -> list[ToolSpec]:
    return [DISPATCHABLE_TOOLS_BY_NAME[n] for n in tool_names if n in DISPATCHABLE_TOOLS_BY_NAME]
```

It takes **tool names** (`frozenset[str]`), not a `SubAgent` — that would be an
upward import into T3. Names with no handler are skipped.

**(b) The prompt `## Tools` section.** Independently,
[subagents/_registry.py](../src/kodo/subagents/_registry.py) renders each
declared tool's `external_name` + `when_to_use` + `autonomous_mode` into the
agent's system prompt (replacing a `{PLACEHOLDER:TOOLS}` token), from
`ALL_TOOLS`. The model thus reads *narrative guidance* in the system prompt and
*callable schemas* via the API `tools` parameter — two views of the same spec.

---

## 8. Autonomous mode

Filtering for autonomous mode happens **once**, in the agent registry — not in
the tool layer. A spec whose `autonomous_mode` contains `"unavailable"` (today
only `ask_user`) is dropped from both the agent's `.tools` set **and** its
rendered `## Tools` section when `registry.get(name, autonomous=True)` is called.
Because the engine builds the LLM tool list from the *already-filtered*
`agent.tools`, the withheld tool simply never reaches the model.

A tool can also declare `autonomous_mode="auto-accepted …"` for a spec whose
*handler* short-circuits on `ctx.session.effective_autonomous` and synthesizes
its response instead of blocking on the gate — no tool does today, since the
one example (the former `request_user_review_artifact`) moved into the
engine: `__finalize_document` (triggered after `document_feedback`, not a
dispatched tool) checks `effective_autonomous` itself and either auto-accepts
or fires the gate. The mechanism remains available for a future tool that
needs it.

> **Where `effective_autonomous` comes from.** The user-facing toggle sets
> `SessionState.autonomous`, but the engine *freezes* that into
> `effective_autonomous` once per prompt (when the worker dequeues it), so a
> mid-prompt toggle never splits a running prompt's mode. Every tool and the
> registry read `effective_autonomous`; the dispatcher therefore needs no
> `autonomous` argument at all.

---

## 8A. The `intent` parameter — mutating tools declare their purpose

Every **first-degree mutator** — a tool whose own dispatch changes content on
disk: `filesystem`, `edit_file`, `run_command`, `create_new_project`,
`rollback` — declares a mandatory `intent` string as the **first** property of
its `input_schema`: one sentence stating what this specific call changes and
why. The property (and the generic "how to state your intent" guidance the
model reads) is defined **once**, in
[toolspecs/_intent.py](../src/kodo/toolspecs/_intent.py) (`INTENT_PROPERTY`),
and embedded by each mutating spec, so the instructions can never drift
between tools.

- **Exempt:** tools that mutate only *through other agents* —
  `run_subagent`, `run_author_critic_iteration`, `toolchain_deps` — because
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

[runtime/_engine.py](../src/kodo/runtime/_engine.py) drives the generic loop
(`__run_agent_turn`), shared by the guide and every leaf agent. Per run:

1. Resolve the agent (`registry.get(name, autonomous)`), which yields its
   filtered `tools` and rendered system prompt.
2. Build the dispatcher: `dispatcher = self.__make_dispatcher(agent_name, session_id)`
   — injecting the gate, session, the single `_EngineServices` adapter, and
   `mode`/`project_root` (read live from the current workflow mode and bound
   project, independent of each other — a Problem-Solver run still carries
   `project_root` if a project happens to be bound). No `autonomous` flag is
   passed; tools read `session.effective_autonomous`.
3. Call `__run_agent_turn(..., tools=tools_for_agent(agent.tools),
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
│   if stop_after_tools():  BREAK     ← e.g. escalate_blocker fired       │
└────────────────────────────────────────────────────────────────────────┘
```

So the handler's returned **JSON string becomes the `content` of a
`tool_result` block**, fed back to the model on the next iteration. The model
reads it, reasons, and either calls more tools or ends its turn. After the loop,
the engine reads `dispatcher.returned_output` (what a leaf returned via
`return_result`) and used `dispatcher.stop_requested` to decide early exit. A
mutating tool call (`filesystem`/`edit_file`) is additionally bracketed by
`__checkpoint_prepare`/`__checkpoint_commit` (§12.1 in INTERNALS.md) — outside
this loop, around the `tool_dispatch` call — so every dispatch in this diagram
that touches a file also earns a mirror commit.

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
   │                                        engine.__run_subagent: builds a NEW
   │                                        ToolDispatcher for the leaf, runs its turn
   │                                                    │
   │                          leaf LLM  tool_use: filesystem(create_file) ─► dispatch(…)
   │                                                    └─► FilesystemTool(ctx).handle(…)
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

The gate-backed tools (`ask_user`, `escalate_blocker`) follow the same path
but their handler `await`s `ctx.gate.fire_*`, which sends a `kind=request`
frame to the VS Code client and
blocks on a future until the user responds — see
[INTERNALS.md §15 "User gate"](INTERNALS.md).

`ask_user` carries a **question batch** — every open question about the
agent's current topic in one call, each with the candidate answers the agent
derived itself (top choice first; the client appends the free-text option, so
specs never include an "Other"). The discipline lives in
`preamble_performance.md` ("Asking the User Questions"), shared by every
agent, not in per-agent prompts. The client renders the batch as an
interactive **in-feed question panel** rather than a tool-call card (the
engine suppresses `agent.tool_call`/`agent.tool_call_detail` for `ask_user`):
the user navigates the boxes, revises selections freely, and answers land
only on *Confirm and Send*. The confirmed panel freezes read-only and is
rebuilt after a reload purely from the persisted `tool_use` (questions) +
`tool_result` (answers) — only the tool call and its result ever reach LLM
context. A crash mid-answer re-drives the whole batch (SESSIONS.md);
`escalate_blocker`'s interactive prompt rides the same gate as a single
free-text-only question (`options: []`).

---

## 12. Adding a new tool — checklist

1. **Spec** — create `src/kodo/toolspecs/_<tool_name>.py` exporting one
   `ToolSpec` (with `input_schema`, a model-facing `description`, generic
   `when_to_use` bullets, optional `autonomous_mode`). Add it to
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
   implementation from the engine's `__make_dispatcher`.

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
| [tools/_tool.py](../src/kodo/tools/_tool.py) | The `Tool` ABC: binds a `ToolContext` (read-only `context` property) and declares abstract `handle`. |
| [tools/_&lt;tool&gt;.py](../src/kodo/tools/) | One `Tool` subclass per tool, with `handle(self, tool_input) -> str`. |
| [tools/_dispatch.py](../src/kodo/tools/_dispatch.py) | `_TOOL_CLASSES` table, `ToolDispatcher`, `tools_for_agent`, `DISPATCHABLE_TOOLS_BY_NAME`. |
| [tools/_paths.py](../src/kodo/tools/_paths.py) | `resolve_within` path guard (file-I/O + shell). |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | Renders each agent's `## Tools` prompt section from spec metadata; autonomous filtering. |
| [llms/anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | Converts `ToolSpec` → API `tools` param; parses `tool_use` → `ToolCallEvent`. |
| [llms/_interface.py](../src/kodo/llms/_interface.py) | `Message`, `ToolCallEvent`, `TurnEnd`, the `stream_query` contract. |
| [runtime/_engine.py](../src/kodo/runtime/_engine.py) | `__make_dispatcher`, `__run_agent_turn` (the tool loop), the `_EngineServices` adapter. |
| [runtime/_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator` (satisfies `GateLike`). |

See also [INTERNALS.md §6A](INTERNALS.md) for the package's place in the
dependency graph, and [CLAUDE.md](../CLAUDE.md) for the import-layer rule.
