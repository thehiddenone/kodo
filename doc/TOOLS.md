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
> without dragging in dispatch logic (gates, the workspace, the engine).

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
 T2  toolspecs            ← the ToolSpec catalog (pure data)
        │
        ▼
 T1  workspace · transport
        │
        ▼
 T0  common · project · toolchains · state · security
```

**Hard rule:** `kodo.tools` may import only from T0/T1/T2 — in practice
`kodo.workspace` and `kodo.toolspecs`. It must **never** import `subagents`,
`llms`, or `runtime`. The collaborators it needs from higher tiers (the gate,
the session, the sub-agent launcher) are inverted into **structural Protocols**
defined inside `tools` and injected by `runtime` (see §5). Verify the ceiling:

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
        "Transitions state.phase to 'done' and ends the Orchestrator session."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},  # JSON Schema
    when_to_use=(                          # rendered into the agent's `## Tools` prompt
        "All product-level stages have completed and the workspace has "
        "nothing left in flight — the project is done.",
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
    (PUBLISH_ARTIFACT, PublishArtifactTool),
    (READ_ARTIFACT,    ReadArtifactTool),
    (ASK_USER,         AskUserTool),
    ...
    (FINALIZE_PROJECT, FinalizeProjectTool),
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

> A spec that has **no** row here (e.g. the placeholders `post_update`,
> `toolchain_build`) is "spec only": it can be rendered into a prompt but is
> silently dropped from the LLM-facing tool list, because `tools_for_agent`
> (§7) only returns specs present in `DISPATCHABLE_TOOLS_BY_NAME`.

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
    workspace: Workspace          # T1 — publish/read artifacts, project_root for file I/O
    index: ProjectIndex           # T1 — query_frontier / list_artifacts read this
    gate: GateLike                # Protocol — ask_user / approval gates (impl in runtime)
    session: SessionLike          # Protocol — finalize_project writes .phase
    runner: SubagentRunner        # Protocol — run_subagent / run_author_critic_iteration
    rollback_fn: Callable[[str], Awaitable[None]]   # injected callback
    complete_fn: Callable[[str], Awaitable[None]]   # injected promotion callback
    agent_name: str               # the running agent (used as artifact author)
    session_id: str
    autonomous: bool
    published_ids: list[str] = field(default_factory=list)   # mutated by publish_artifact
    stop_requested: bool = False                             # set by escalate_blocker
```

The three things a tool needs from *above* its tier are **structural
Protocols**, also defined in `_context.py`:

- **`GateLike`** — `fire_question(...)` / `fire_approval(...)`. Runtime's
  [`GateOrchestrator`](../src/kodo/runtime/_gates.py) satisfies it by shape (no
  inheritance). Its response types satisfy the read-only `QuestionLike` /
  `ApprovalLike` protocols.
- **`SessionLike`** — a settable `phase: str`. Runtime's `SessionState` matches.
- **`SubagentRunner`** — `run_subagent(...)` / `run_author_critic_iteration(...)`.
  Runtime injects a small `_EngineSubagentRunner` adapter wrapping the engine's
  private methods.

This is the dependency inversion that lets the tool layer sit *below* the engine
while still calling back into it. `runtime` constructs the concrete objects and
hands them in; `tools` only ever names the Protocols.

Per-run state lives on the context, not on the tool instance:
`PublishArtifactTool` appends to `self.context.published_ids`,
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

There is **one unified tool surface** — no orchestrator-vs-leaf split. Every
agent (the orchestrator included) is granted exactly the tools its frontmatter
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

Tools marked `"auto-accepted"` (e.g. `request_user_review_artifact`) stay
available; the handler itself short-circuits on `ctx.autonomous` and synthesizes
the response instead of blocking on the gate.

---

## 9. The dispatcher

[`ToolDispatcher`](../src/kodo/tools/_dispatch.py) is built **once per agent
run** by the engine. It owns the run's `ToolContext` and routes calls:

```python
class ToolDispatcher:
    def __init__(self, *, workspace, index, gate, session, runner,
                 rollback_fn, complete_fn, agent_name, session_id, autonomous=False):
        self.__ctx = ToolContext(...)            # one context for the whole run

    @property
    def published_ids(self) -> list[str]: ...    # read by the engine after the run
    @property
    def stop_requested(self) -> bool: ...        # read by the engine after each tool batch

    async def dispatch(self, tool_name, tool_input) -> str:
        tool_cls = _CLASSES_BY_NAME.get(tool_name)
        if tool_cls is None:
            return json.dumps({"error": f"Unknown tool: {tool_name!r}"})
        return await tool_cls(self.__ctx).handle(tool_input)   # bind context, then run
```

`dispatch` is the single function the engine passes into its turn loop as the
`tool_dispatch` callback. It instantiates the matching `Tool` subclass bound to
this run's context and calls its `handle`. Whether the caller is the orchestrator
or a leaf sub-agent, the routing is identical — only the *contents* of the
context and the *set* of tools differ.

---

## 10. The engine turn loop — putting it together

[runtime/_engine.py](../src/kodo/runtime/_engine.py) drives the generic loop
(`__run_agent_turn`), shared by the orchestrator and every leaf agent. Per run:

1. Resolve the agent (`registry.get(name, autonomous)`), which yields its
   filtered `tools` and rendered system prompt.
2. Build the dispatcher: `dispatcher = self.__make_dispatcher(agent_name, session_id)`
   — injecting the gate, session, the `_EngineSubagentRunner`, and the
   `rollback`/`complete` callbacks, reading the *current* `ProjectIndex`.
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
the engine reads `dispatcher.published_ids` (what a leaf produced) and used
`dispatcher.stop_requested` to decide early exit.

---

## 11. Full end-to-end sequence

A concrete trace of the orchestrator calling `run_subagent`, which spawns a leaf
that publishes an artifact:

```text
 LLM (orchestrator)                  Engine / ToolDispatcher              tools/ Tool classes
 ──────────────────                  ───────────────────────              ───────────────────
   │  tool_use: run_subagent ──────────►  dispatch("run_subagent", …)
   │                                        └─► RunSubagentTool(ctx).handle(…)
   │                                              └─► self.context.runner.run_subagent(name, …)
   │                                                    │  (engine adapter)
   │                                                    ▼
   │                                        engine.__run_subagent: builds a NEW
   │                                        ToolDispatcher for the leaf, runs its turn
   │                                                    │
   │                          leaf LLM  tool_use: publish_artifact ─► dispatch(…)
   │                                                    └─► PublishArtifactTool(ctx).handle(…)
   │                                                          └─► self.context.workspace.publish(…)
   │                                                          └─► self.context.published_ids.append(id)
   │                                        leaf turn ends → published_ids = [id]
   │                                                    ▼
   │  tool_result: {"artifact_ids":[id]} ◄──  json.dumps({"artifact_ids": published_ids})
   │  …reasons, calls next tool…
```

The gate-backed tools (`ask_user`, `request_user_review_artifact`,
`escalate_blocker`) follow the same path but their handler `await`s
`ctx.gate.fire_*`, which sends a `kind=request` frame to the VS Code client and
blocks on a future until the user responds — see
[INTERNALS.md §15 "User gate"](INTERNALS.md).

---

## 12. Adding a new tool — checklist

1. **Spec** — create `src/kodo/toolspecs/_<tool_name>.py` exporting one
   `ToolSpec` (with `input_schema`, a model-facing `description`, generic
   `when_to_use` bullets, optional `autonomous_mode`). Add it to
   `toolspecs/__init__.py` imports / `__all__` / `ALL_TOOLS`.
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
| [toolspecs/_<tool>.py](../src/kodo/toolspecs/) | One `ToolSpec` constant per tool (pure data). |
| [toolspecs/__init__.py](../src/kodo/toolspecs/__init__.py) | Re-exports specs + `ALL_TOOLS` (for prompt rendering). |
| [tools/_context.py](../src/kodo/tools/_context.py) | `ToolContext` + the injected Protocols (`GateLike`, `SessionLike`, `SubagentRunner`, `QuestionLike`, `ApprovalLike`). |
| [tools/_tool.py](../src/kodo/tools/_tool.py) | The `Tool` ABC: binds a `ToolContext` (read-only `context` property) and declares abstract `handle`. |
| [tools/_&lt;tool&gt;.py](../src/kodo/tools/) | One `Tool` subclass per tool, with `handle(self, tool_input) -> str`. |
| [tools/_dispatch.py](../src/kodo/tools/_dispatch.py) | `_TOOL_CLASSES` table, `ToolDispatcher`, `tools_for_agent`, `DISPATCHABLE_TOOLS_BY_NAME`. |
| [tools/_paths.py](../src/kodo/tools/_paths.py) | `resolve_within` path guard (file-I/O + shell). |
| [tools/_serialize.py](../src/kodo/tools/_serialize.py) | `serialize_artifact` (used by `read_artifact`). |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | Renders each agent's `## Tools` prompt section from spec metadata; autonomous filtering. |
| [llms/anthropic/_claude.py](../src/kodo/llms/anthropic/_claude.py) | Converts `ToolSpec` → API `tools` param; parses `tool_use` → `ToolCallEvent`. |
| [llms/_interface.py](../src/kodo/llms/_interface.py) | `Message`, `ToolCallEvent`, `TurnEnd`, the `stream_query` contract. |
| [runtime/_engine.py](../src/kodo/runtime/_engine.py) | `__make_dispatcher`, `__run_agent_turn` (the tool loop), `_EngineSubagentRunner`. |
| [runtime/_gates.py](../src/kodo/runtime/_gates.py) | `GateOrchestrator` (satisfies `GateLike`). |

See also [INTERNALS.md §6A](INTERNALS.md) for the package's place in the
dependency graph, and `src/kodo/CLAUDE.md` for the import-layer rule.
