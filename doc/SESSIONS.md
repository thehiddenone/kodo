# Sessions and Subsessions

This document explains how Kōdo persists a conversation, how sub-agents get
their own isolated history, and how an interrupted sub-agent is resumed after a
crash or restart.

> **Multi-session update (2026-06-21).** A single **singleton server** (rooted at
> the global `~/.kodo`, no more `.kodo-workspace`) now manages **many sessions
> concurrently** — one per VS Code window. Session stores live at
> `~/.kodo/sessions/<id>/` as before. Key changes:
> - **A session is owned by exactly one window at a time** (server-authoritative).
>   Opening it from a second window is rejected (`session_in_use`). A disconnect
>   starts a short **grace window** that lets the same window reload and reclaim;
>   a graceful close (`session.release`) frees it immediately.
> - **Resume is client-driven:** each window persists its `session_id` and sends
>   it in `hello`. The old workspace-level guide-session **marker** and its
>   auto-resume are gone (`GuideMarker`/`locate_guide_session`
>   remain in the tree but are unused by the engine).
> - **A session's bound roots are its `workspace.folders`** — the live VS Code
>   workspace folders, or the locked/bound-directories fallback when
>   disconnected (§ below, WS_PROTOCOL.md §7.1b/§7.1c) — the same mechanism for
>   both workflow modes since the 2026-07-24 multi-project rework. There is no
>   separate Guided-mode project binding any more (the old `current_project`
>   field, immutable and singular, is gone): a Guided session may be bound to
>   zero, one, or several projects, exactly like Problem Solver. `workflow_mode`
>   (`session.list`'s display field) says which pipeline is driving, not which
>   or how many directories are bound.
> - **Crash-resume** of a mid-subagent turn is unchanged, and now runs when the
>   `SessionManager` (re)loads that specific session.
> - LLM scheduling across sessions is owned by the shared gateway — see
>   [LLM_GATEWAY.md](LLM_GATEWAY.md).

## The two levels

Kōdo's persisted state has exactly two levels:

1. **The main session** — the top-level conversation between the user and
   whichever *entry agent* is currently driving (the **Guide** in Guided
   Project Workflow, or the **Problem Solver** in Problem-Solving mode). A
   session is owned by exactly one window at a time (above), but — since the
   2026-07-24 multi-project rework — is not tied to a single project
   directory: it may bind zero, one, or several roots, in either mode.

2. **Subsessions** — each time the main agent spawns a sub-agent (e.g. a
   Narrative Author, an Architect, a Critic), that sub-agent runs in its own
   **isolated** message history called a subsession. A subsession has a
   session-wide-unique ID and is stored alongside the main session.

The main session is **not tied to an agent.** The user can switch between
Guide and Problem Solver and back within a single session; both entry
agents **share one message history** (`session.jsonl`). Switching mode only
swaps the system prompt and the available tools — the conversation continues
seamlessly across the change.

> Sub-agent spawning is **not** wired to the Guide. Any agent may spawn
> sub-agents if (a) its frontmatter grants a spawning tool
> (a `run_subagent_<name>` tool) and (b) its frontmatter
> declares a `subagents:` allow-list naming the sub-agents it may call. The
> engine gates every spawn against the **calling** agent's allow-list
> (`AgentRegistry.allowed_subagents` → `_assert_can_spawn`), so the permission
> travels with whichever agent makes the call — not with a hard-coded
> guide identity. Today only the Guide opts in (the Problem Solver
> ships without a spawning tool, so in Problem-Solving mode there are no
> subsessions), but the path is fully agent-agnostic and crash-resume recovers
> *whichever* entry agent was holding the floor (see `_last_entry_agent`).

## On-disk layout

```
.kodo/sessions/<main-session-id>/
    meta.json          — session name, creation time (created_at), and
                         last_modified (bumped on every persisted write below)
    transient.json     — mutable runtime state: stage, last prompt, autonomous,
                         pending_prompt, pending_security_alert (SECURITY.md §7a),
                         active_subsession (the resume hook), the session's
                         remembered VS Code workspace shape (workspace_physical_root,
                         workspace_folders, workspace_code_file, workspace_locked_paths
                         — WS_PROTOCOL.md §7.1b, CHECKPOINTS.md §8), and the running
                         token totals (cumulative_input_tokens,
                         cumulative_input_tokens_uncached, cumulative_output_tokens
                         — WS_PROTOCOL.md §5.7)
    session.jsonl      — the MAIN session log (see below)
    subsessions/
        <subsession-id>.jsonl   — one per sub-agent run; the sub-agent's full,
                                  isolated message history (per-call usage
                                  stats included, as its own `usage` markers)
    attachments/
        <attachment_id>__<basename>   — immutable copies of files the user attached
                                to a prompt (one per accepted file), named after
                                their own UUID4 id. session.jsonl holds only a
                                link, never the content; see below
```

`<main-session-id>` is the guide session ID minted at bootstrap (a POSIX
timestamp). `<subsession-id>` is a random hex ID minted per `run_subagent` call.

`meta.json`'s `session_name` (set once by the titler — INTERNALS.md §10c) is
disambiguated against every other session's persisted name before it's
written (`TransientStore.set_session_name` → `__unique_name`, scanning
sibling `sessions/*/meta.json` files, this session's own directory excluded):
a collision gets `-1`, `-2`, ... appended. The titler's output is a
deterministic function of the sanitized prompt, so two sessions started from
similar or identical prompts would otherwise be indistinguishable in the tab
strip and session picker.

`transient.json` also remembers the session's running input/output token
totals — `cumulative_input_tokens`, `cumulative_input_tokens_uncached`, and
`cumulative_output_tokens` (WS_PROTOCOL.md §5.7), folded in via
`TransientStore.add_tokens` from every LLM call the session makes, main or
subsession alike (`EngineEmitters.add_tokens_from_usage`). This is what lets
the status line's "Total input/output tokens" figure survive a resume — it
keeps counting from where it left off instead of resetting to zero the way
`cumulative_usd` does (that one stays in-memory only, on `EngineEmitters`).

`transient.json` also remembers the session's whole VS Code workspace shape —
`workspace_physical_root`, `workspace_folders` (logical name → physical path,
in the window's own order), and `workspace_code_file` (the `.code-workspace`
file the window was opened from, or `None`) — kept in sync with every
`workspace.folders` push (`WorkflowEngine.handle_workspace_folders`), not just
the live in-memory `SessionWorkspace`. This is what lets a session resumed
from a *different* window (`pickSession()`, WS_PROTOCOL.md §7.1b) have its
remembered workspace reopened before it loads, instead of resuming into
whatever happens to already be open.

Since 2026-07-22 that reconciliation is no longer a verbatim overwrite: a
folder in `workspace_locked_paths` (populated exclusively by
`TransientStore.lock_workspace_path`, called the moment a root earns its
first non-empty checkpoint commit — CHECKPOINTS.md §8) can never be dropped
from `workspace_folders` again, even if a later push omits it. This is what
makes the session ↔ folder link irreversible once Kōdo has actually done
work there; resuming a session with any locked folder into a different
workspace now asks for confirmation first instead of silently reopening
(WS_PROTOCOL.md §7.1b).

As of 2026-07-23 (mode-agnostic since the 2026-07-24 multi-project rework — see
below), a locked session whose live pushed workspace
is missing or no longer hosts every locked path operates in
**disconnected/isolated mode**: `EngineCore._root_paths()`/`_has_workspace()`/
`_make_resolver()` fall back to the bound directories (`workspace_folders`
filtered to `workspace_locked_paths`) instead of the live `SessionWorkspace`,
so file/shell tools — and `run_command`'s default cwd, and the security
layer's escape analysis, which all resolve through the same path resolver —
keep working against real, on-disk history even with no matching VS Code
window open. The judgment call behind this — "can this candidate workspace
shape host this session's bound directories" — is one function,
`kodo.state.workspace_shape_compatible` (root must match, every locked path
must be present), reused by `WorkflowEngine._is_workspace_connected()` (live,
feeds the `state` event's `workspace_connected` field, WS_PROTOCOL.md §5.1)
and by `session.list`'s `compatible` field (from disk alone, no live engine
needed — WS_PROTOCOL.md §7.1b). This connection-state concept was already
**mode-agnostic** before the root-resolution fallback itself was: a Guided
session got `workspace_connected` too, and `scaffold_new_project`
already skipped its `workspace.add_folder` push (but still scaffolded and
locked immediately) whenever disconnected, regardless of mode. As of
2026-07-24 the root-resolution fallback is mode-agnostic too (WS_PROTOCOL.md
§7.1c) — Guided mode no longer has its own singular `current_project` binding
to be exempt via; it shares this exact fallback. New-project locking
is now always immediate on creation (not gated on a first real commit like
every other root), so a disconnected session's own fallback sees a
freshly-created directory right away.

### `session.jsonl` — the main log

`session.jsonl` is an append-only log that interleaves **two kinds of lines**,
every one stamped with a unique `id` and an ISO-8601 UTC `ts` (both added
centrally by `TransientStore.__append_line`, so no caller needs to set
them — a stable `id` lets a later entry reference one that resolved it, e.g.
reconciling a dangling tool call; `ts` is for eventual client display, not
read by anything server-side, since append order already *is* chronological
order):

- **Message lines** — `{"role": "user"|"assistant", "content": ..., "entry_agent": "guide"|"problem_solver"}`.
  These are the top-level LLM context. `entry_agent` is a display/audit tag only;
  because the two entry agents share context, every message replays into the one
  `_main_messages` list regardless of tag.
  A user message that carried file attachments also gets `"attachments":
  [{"id", "name", "stored"}]` — opaque links to the copies under `attachments/`
  (`id` is the attachment's UUID4, `stored` is the session-relative path). The
  persisted `content` is the user's **clean** prompt; the attachment *content*
  is never written here. On resume the links are re-expanded into the LLM
  message as the same `<ATTACHMENT ID="..." filename="..."/>` tags appended
  after the prompt at submit time (content is fetched on demand via the
  `read_attachment` tool, not re-read from the links), so the reconstructed
  context matches submit time without the log ever holding the file bytes. See
  WS_PROTOCOL.md §7.1 / `kodo.runtime._attachments`.
- **Marker lines** — `{"type": "subsession_start"|"subsession_end"|"compaction"|"error"|"security_rule_added"|"agent_stuck_critical"|"usage"|"greeting", ...}`.
  `subsession_start`/`subsession_end` record, *in chronological position*, when
  a sub-agent took over and when it handed control back. They carry
  `subsession_id`, `agent`, `display_name`, and `parent_display_name`;
  `subsession_end` also carries the sub-agent's structured `result` from
  `return_result` (e.g. an author's `primary_path`). `compaction` records a
  context reset (`summary`, `reason`, `tokens_before`/`tokens_after`). `error`
  records an `EngineEmitters.emit_error` runtime failure (`message`,
  `recoverable`). `security_rule_added` and `agent_stuck_critical` similarly
  durably record their live counterparts. `greeting` (`{text}`) records a
  brand-new session's opening greeting (`EngineEmitters.emit_greeting`,
  `runtime._engine._greeting.SessionGreeter`, doc/WS_PROTOCOL.md §5.9i) — like
  the others, this is what lets it replay via `session.history` after a
  reload; being a marker rather than a `{role, content}` message line is what
  keeps it out of `load_main_messages`' live LLM context (see below). `usage` records one LLM call's
  per-turn stats (`cumulative_usd`, `duration_seconds`, `last_call_tokens`,
  `model`, `usd_cost`, `stop_reason`, `agent`) — the persisted twin of the live
  `usage.update` event, added so the WebView's "Kodo responded in..." row
  replays in its correct chronological position on reload instead of being
  reconstructed from whatever had accumulated in the live client's memory
  (which used to bunch every such row into one trailing block after a reload
  — see `HistoryProjector._marker_to_entries` and the WebView reducer's
  `session_history` case). This is the *only* per-call audit record — there
  used to be a separate, never-read-back `agents/<agent>.jsonl` log
  (`TransientStore.write_agent_record`); it was removed in favor of folding
  `usd_cost`/`stop_reason`/`agent` straight into this marker, so a call's
  full audit trail lives in the one file (session or subsession) it actually
  happened in, with nothing split out to a separate directory.
  All of these except `subsession_start`/`subsession_end`/`greeting` can
  equally appear inside a **subsession's own log** (below) — an error, a
  granted security rule, or the stuck watchdog can just as easily happen to a
  sub-agent's own turn as to a top-level one; `EngineEmitters._append_marker`
  routes to whichever log is active (`TransientStore.active_subsession`) so
  the event lands where it actually happened, never bleeding into the
  parent's log. `greeting` can't appear there in practice either — it only
  ever fires once, from `WorkflowEngine.start`'s brand-new-session branch,
  before any subsession could possibly be active.

`TransientStore.read_messages()` returns only the message lines (for rebuilding
LLM context); `read_session_lines()` returns everything (for resume and history
rebuild). A subsession's own `<subsession-id>.jsonl` under `subsessions/` is
otherwise identical in shape and append/read API
(`append_subsession_marker`/`read_subsession_messages`/`read_subsession_lines`
mirror `append_marker`/`read_messages`/`read_session_lines`) — the only
structural difference between a session and a subsession is that a subsession
can never itself contain a `subsession_start`/`subsession_end` marker
(subsessions do not nest).

## Thinking blocks

Extended-thinking text is not a UI-only side channel. The engine's shared turn
loop (`_run_agent_turn`) accumulates every `ThinkingDelta` it streams to the
client into one `{"type": "thinking", "thinking": "<text>"}` content block,
prepended as the **first** block of the assistant `Message` it appends to
`messages` — ahead of any `text`/`tool_use` blocks in that same turn. Because
that `Message` is exactly what gets persisted to `session.jsonl` (or a
subsession file) and exactly what gets replayed back to the LLM on the next
call, thinking is now real, durable conversation context, the same way
`tool_use`/`tool_result` blocks already were — not something reconstructed
only for display.

**Per-provider signature handling.** Anthropic's extended thinking signs each
thinking block; the API rejects a later request that replays thinking text
without the exact signature Anthropic issued for it. The Claude plugin
(`llms/anthropic/_claude.py`) requests thinking on every call — `thinking:
{type: "adaptive"}` plus an `output_config.effort` on the adaptive-thinking
models, `thinking: {type: "enabled", budget_tokens: ...}` on the older ones,
both driven by the session's thinking level (see below) — captures that
signature from the
stream's `signature_delta` as a new `ThinkingSignature` event, and the engine
stores it on the block (`"signature": "<sig>"`). llama.cpp has no equivalent
mechanism — its `ThinkingDelta`s (parsed from `<think>...</think>` tags or an
OpenAI-style `reasoning_content` field) never carry a signature, so their
blocks are persisted without one.

This matters because a session can switch providers mid-conversation (local
↔ cloud), so a thinking block produced by one provider can end up in front of
the other on the next call:

- `llms/anthropic/_cache.py:_drop_unsigned_thinking` strips any `"thinking"`
  block lacking a `signature` before it reaches the Anthropic API — a
  llama.cpp-origin block would otherwise make Claude reject the whole request.
- `llms/llamacpp/_llama.py:_expand_assistant` re-wraps a `"thinking"` block
  back into the model's own `<think>...</think>` convention when building
  history for llama.cpp, regardless of which provider produced it (the
  signature, if any, is simply irrelevant to llama.cpp and dropped).

**Known gap:** Anthropic's `RedactedThinkingBlock` (safety-flagged reasoning,
delivered as opaque ciphertext with no plain text) is not captured — there is
nothing human-readable to show, and it is rare enough that the Claude plugin
just ignores it rather than threading a third content-block shape through the
engine.

**Display + reload.** The WebView's `thinking_block` session-entry type
already rendered a collapsible, toggleable block for the *live* streamed
text; the only missing piece was that nothing rebuilt it after a reload.
`WorkflowEngine.HistoryProjector._message_to_entries` now also emits a `thinking_block` entry
(sourced from the persisted `"thinking"` block) ahead of the
`assistant_response` entry for an assistant message, so `session.history`
replays it exactly where it appeared live, with the same collapsible UI.

## Persistence (the "append-before-respond" guarantees)

### Main turns

A main turn's messages are flushed to `session.jsonl` **before every tool
dispatch** — not just sub-agent spawns. `_run_agent_turn`'s `_flush()` runs
once immediately after the assistant `tool_use` message is appended (before any
tool in the batch runs) and again after the tool results land, so the persisted
prefix — **including the assistant message that contains the `tool_use`** — is
never behind an in-flight tool call. The added latency is negligible next to the
LLM round-trip the flush is nested inside.

This is what makes resume possible: if the process dies (or a tool's *own* side
effect reloads the client — e.g. `scaffold_new_project` firing
`workspace.add_folder`, which reloads the VS Code window into a multi-root
workspace) mid-dispatch, the main log ends with a dangling assistant `tool_use`
that has **no** following `tool_result`. That dangling `tool_use` is the signal
that a tool was in-flight. (Before this, only `_SUBAGENT_SPAWNING_TOOLS` were
flushed before dispatch, so the very turn that triggered a workspace reload was
the one guaranteed to still be unpersisted — and thus missing from the replayed
history — when the client reconnected.)

Turns that never reach a tool dispatch (a plain-text reply) still flush only at
the end, so a crash before any tool runs leaves nothing half-written and the
existing `pending_prompt` re-surfacing path — which now covers **approvals
only** — is unaffected. `ask_user` no longer persists a `pending_prompt`: its
`tool_use` (carrying the whole question batch) is flushed before dispatch, so a
crash while the user is mid-answer resumes through the dangling-tool-use path
below and the **entire batch is re-asked from scratch** — nothing the user had
entered is ever stored before they confirm.

### Subsession turns

Sub-agent messages are flushed to `subsessions/<id>.jsonl` at **every turn
boundary** (`persist_each_iteration=True`), so the sub-agent's progress is
durable as it goes and can be resumed mid-run.

### The active-subsession pointer

When a sub-agent takes over, the engine:

1. writes a `subsession_start` marker to `session.jsonl`,
2. sets `transient.json`'s `active_subsession` to
   `{subsession_id, agent, display_name, parent_display_name}`,
3. emits `subsession.started` to the client.

When it finishes, the engine writes a `subsession_end` marker (with the
sub-agent's structured `result`), clears `active_subsession`, and emits
`subsession.ended`. So at any instant, `active_subsession` names the one
subsession that "is the active one right now," exactly as the user model
requires.

A live user-initiated **Stop** while a subsession is active goes through the
same three steps, not a bypass of them: `_spawn_subagent` awaits
`_drive_subsession` then `_close_subsession` as two sequential,
unguarded calls, so cancelling the worker task mid-subsession unwinds
straight through `_drive_subsession` and skips `_close_subsession`
entirely — nothing else would ever clear `active_subsession`, clear the
subsession context gauge, or tell the client the block ended.
`WorkflowEngine.stop()` (`_core.py`) checks `active_subsession` itself and,
if set, runs `_abort_active_subsession()` (`_subagents.py`) — the same
marker/pointer-clear/event sequence as `_close_subsession`, sourcing the
closing agent/display names from the `active_subsession` record itself
rather than `session.agent` (which `_drive_subsession` has by then
overwritten with the sub-agent's own name) — always with `failed: true` (a
Stop is neither the clean finish nor the schema-compliance failure that flag
otherwise distinguishes, but the generic "Interrupted by user" callout that
follows right after already says what happened). This runs before `stop()`
flips `session.phase` to `"stopped"`, so the client's `subsession.ended`
handling (closing the collapsible block) lands before the `state` event that
produces its "Interrupted by user" callout, and the callout renders outside
the now-closed block, not swallowed into it.

`stop()` also cannot use `session.agent` for the interrupted-turn record's
`entry_agent` tag while a subsession is active, for the same reason —
it recovers the true top-level entry agent (who actually owns the dangling
`run_subagent` tool_use in `_main_messages`) via `_last_entry_agent()`
instead.

### Typed sub-agent interface (input/output schemas)

Agent↔sub-agent interaction is typed, mirroring tools. Every sub-agent except
the entry agents (`guide`, `problem_solver`) has a `SubAgentSpec`
(`kodo.subagents.specs`, one literal per file) declaring an `input_schema` and an
`output_schema`. Neither schema is ever restated as prose in a system prompt.
The registry auto-grants such agents the terminal `return_result` tool and a
short, fixed note pointing at where its real task lands (no schema, no
per-agent detail — see `_registry.py`'s `_INPUT_PARAMETERS_NOTE`, which
replaced the old `## Your Task Contract` section); a caller's
each `run_subagent_<name>` tool carries its callee's description but no
schemas either — those reach the caller as real JSON Schema on its own
`run_subagent_<name>` tool.

- **Input.** `run_subagent` takes `{name, task_input}` where `task_input` is a
  structured object conforming to the callee's `input_schema`. The engine
  renders it to the seed user turn (`_render_task_input`): `instructions`
  becomes a `# Task` heading, and every other field the sub-agent was actually
  given is pretty-printed under a trailing `## Input Parameters` section, each
  labeled with its schema `description` when one exists — this is the
  concrete-values counterpart to the caller-facing schema, and the last
  section of the message (for a chat-templated local model, the last section
  of the whole prompt). That section also carries the sub-agent's only
  remaining prose reminder of how `return_result` works. The seed is persisted
  with `kind="subagent_task"`, so the UI shows it as a distinct **task brief**
  card, not a user-prompt bubble. The rendered task also rides the
  `subsession.started` event's `task` field for the live feed.
- **Output.** The sub-agent ends its run by calling `return_result` with a
  payload validated/normalized against its `output_schema` (`normalize_output`,
  which also handles a top-level `oneOf` for a dual-role sub-agent returning one
  of several output shapes). The
  engine reads the structured result off `dispatcher.returned_output`; if the
  agent never called `return_result`, a bare `{schema_compliance: false}`
  fallback is synthesized — there is no artifact index to recover a partial
  result from anymore. `run_subagent` returns this structured result verbatim.
- **Author/critic.** When the spawned sub-agent declares a `critic:`, that same
  `run_subagent_<name>` call runs the *whole* loop (`_run_review_loop`; see
  doc/TOOLS.md §5A). Each round spawns the author with
  `{instructions, input_paths, for_revision_path}` (the last two set by the
  engine from the previous round), reads the author's
  `output_schema.primary_path` from its `return_result`, then spawns the critic
  against that same path. The critic's own `return_result` **is** its verdict
  (`{path, accept, concerns, summary}`), which the engine appends to that file's
  `.jsonl` evolution log; but the **status the loop acts on is read back from
  the log**, not from that payload, because the user's own review decision lands
  there too (and can turn an accepted file back into one needing revision). The
  jsonl remains the single source of truth for status. There is no
  `previous_artifact_id`/`for_revision_artifact_ids` plumbing —
  `for_revision_path` is a single path, since the loop always concerns exactly
  one file per round.
- **Engine-driven agents.** `compactor` carries a spec and returns through
  `return_result` (`{summary}`); the silent `_run_silent_return_turn` grants
  it the tool and captures the payload, with raw text as a fallback. Session
  titling used to work the same way (`session_titler`, a sub-agent LLM call
  through the main chat model, taking 10-15s) but is now `kodo.titling` — a
  guardrailed chat-completion call to a small, dedicated Qwen3-0.6B
  llama-server (its own process, separate from the main chat model's),
  awaited directly from a fire-and-forget background task rather than
  running as a main-agent turn. See `SessionTitler`
  (`runtime/_engine/_titling.py`), doc/INTERNALS.md §10c, and §5.9a/§5.9b of
  `WS_PROTOCOL.md`.

## Thinking level

Each session tracks its own **`thinking_level`** (`SessionState.thinking_level`,
`TransientStore.thinking_level`) — a reasoning-tier slug for whatever the
session's currently active model's thinking family supports. Two catalogs
back it, both keyed by `base_llm` (doc/LLM_REGISTRY.md §4.5/§4.5a):

- **Local models** — `kodo.llms.local_thinking_family`/`local_thinking_tiers`/
  `local_thinking_default_tier`: six tiers (`minimal`..`unlimited`) for the
  Qwen reasoning-budget family, three (`low`/`medium`/`high`) for the GPT-OSS
  reasoning-effort family.
- **Cloud vendors** — `kodo.llms._cloud_thinking`'s
  `CLOUD_THINKING_FAMILIES`, keyed by a synthetic `base_llm` that is the
  vendor key itself. Every vendor has one, and each carries its own API's
  effort vocabulary rather than a shared scale (Anthropic
  `low`..`max`, Google `minimal`..`high`, DeepSeek/Kimi `low`/`high`/`max`,
  Alibaba `low`/`medium`/`xhigh`, …). On the two aggregator vendors the tier
  can only reach the models that actually accept one — silently for OpenRouter
  (unsupported models ignore the parameter), and by explicit omission for
  Bedrock (whose per-model parameters are passthrough-validated, so kōdo sends
  an effort only to the Claude families documented to accept it,
  doc/LLM_REGISTRY.md §3b). Both say so in the client's tier tooltips.

`""` means the active model has no thinking family at all — a local model
outside both families (e.g. Qwen3-Coder-Next-80B, or any `custom_*` registry
entry). Every LLM call the session's engine makes — the main turn, the
security judge, compaction, `web_search`'s tool loop — carries this one value
(`LLMPlumbingMixin._thinking_kwargs`), not a per-call override; it is a
whole-session setting, the same way `command_control` is. Each plugin
translates the tier into its provider's own request shape and validates it
against its own accepted set, falling back to the family default rather than
forwarding a value the provider would reject.

Unlike `edit_control`/`command_control` (WS_PROTOCOL.md §5.1), which are
fixed 3-way enums the client owns and the server just mirrors,
`thinking_level`'s valid value set is **model-dependent**, so the engine is
the source of truth and validates every change:

- **A brand-new session** seeds `thinking_level` from the active model's
  family default — Qwen-family sessions start at `"unlimited"`, GPT-OSS-family
  at `"medium"`, cloud sessions at their vendor's own documented API default
  (Anthropic/Bedrock `"high"`, OpenAI/Google/Meta/OpenRouter `"medium"`,
  DeepSeek `"high"`, Alibaba `"xhigh"`, Kimi `"max"`), non-thinking models at `""`. A caller can override this seed via `hello`'s optional
  `thinking_level` field (WS_PROTOCOL.md §4.1) instead — built for the
  validator's RVP judge session, whose `hello` fires before there is
  anywhere else to attach the tier its preceding `llm.select` pinned
  (doc/VALIDATOR.md §9).
- **A resumed session** restores its persisted value, but only if it is
  still valid for the *currently* active model — the active local model /
  cloud vendor is a machine-global selection, not per-session, so it may have
  changed while this session was closed (and a switch between two cloud
  vendors changes the valid tier set just as a local model switch does). An invalid persisted value
  self-heals to the current model's family default rather than being kept.
- **A live model switch** (`config.reload`, broadcast to every open
  session — WS_PROTOCOL.md §7.5) re-derives `thinking_level` the same way if
  the active model's thinking-family identity actually changed
  (`WorkflowEngine._sync_thinking_level_to_model`, called from the worker's
  `config_changed` handling alongside `ContextCompactor.handle_config_changed`)
  — a tier valid for one family (e.g. Qwen's `"huge"`) is not necessarily
  valid for another (GPT-OSS has no `"huge"` tier, Google no `"max"`,
  DeepSeek no `"medium"`), so the reset avoids silently carrying over a
  meaningless value. Switching cloud vendors counts as such a switch.
- **A user request** (`thinking_level.set`, WS_PROTOCOL.md §7.4e) is
  validated against the active model's tiers and rejected outright if
  invalid (`WorkflowEngine.handle_thinking_level_set`) — unlike
  `edit_control.set`/`command_control.set`'s coerce-to-a-safe-default
  behavior, there is no single "safe" tier to fall back to across every
  model family.

`WorkflowEngine._current_base_llm()` is the shared resolver behind all of
this: it resolves the entry agent's model key the same way `_resolve_plugin`
does, then looks up its `base_llm` in the local registry — `""` for a cloud
model or a local entry with none.

## Sampling parameters

The session's other per-request local-model knob (`SessionState.sampling`,
`TransientStore.sampling`) — request-level `llama-server` sampling overrides
such as `temperature`/`top_k`/`min_p`, edited from the ⚙ button in the chat
footer. Full reference: [SAMPLING.md](SAMPLING.md).

Shares thinking level's shape — per-session, server-owned, applied to *every*
local LLM call the session makes (main turn, compaction, `web_search`'s tool
loop), never to a cloud call — but differs on the two points that matter:

- **Keyed by quant, not flat.** `sampling` is
  `{entry_name: {parameter: value}}`, one override set per local registry
  entry the session has tuned. Because each set is already scoped to the entry
  it applies to, a model switch **does not reset it** the way it resets
  `thinking_level` — the other entries' sets simply go dormant, and switching
  back restores them. There is no `_sync_*_to_model` equivalent, and a resumed
  session adopts the persisted map verbatim
  (`WorkflowEngine.start`) rather than re-validating it against the current
  model.
- **Bad values degrade instead of failing.** `handle_sampling_set` accepts the
  request and runs it through `SamplingParams.from_json`, which drops unknown,
  reserved and wrong-typed parameters and clamps out-of-range numbers, then
  echoes back what actually stuck (`sampling.accepted`, WS_PROTOCOL.md §7.4f).
  Only an unknown `model` is rejected. Contrast `thinking_level.set`, which
  rejects an invalid tier outright.

Each `sampling.set` is a **full replace** for that one quant, not a patch: an
absent parameter means "don't send this field", so llama-server falls back to
whatever the active configuration's launch args set it to. Clearing a field in the UI is
therefore a real operation, not a reset to some default — see SAMPLING.md §1.

`WorkflowEngine._sampling_kwargs` is the read side — it sends this session's
own overrides for the active entry, read fresh on every call, so moving a
slider in the sampling modal lands on the next request with no llama-server
restart. Launch-level sampling is not a separate layer: it reaches
`llama-server`'s command line through the Default profile's sampling knobs or
a user-defined profile's own flags, and is already baked into whatever the
process was launched with (SAMPLING.md §9) — so editing it, unlike this
session state, always requires a restart.

## Resume

Everything in this section is about a genuine server **process** restart
(crash, or an explicit relaunch) — not a live client disconnect/reconnect
(a VS Code window reload) with the process still running. The two are
different events with different recovery paths: a live reconnect never
reaches any of the machinery below at all, because the session stays
resident in memory (`SessionManager` only rebuilds a session that *isn't*
already loaded) — see WS_PROTOCOL.md §8 and doc/SECURITY.md §7b for how that
case is handled instead (a still-outstanding server-initiated prompt is
never lost to begin with, so there is nothing to resume).

On every server *process* start, `locate_guide_session` locates (or creates)
the main session and the engine reloads `_main_messages` from
`session.jsonl`. There is no project-wide index to rebuild — a project's
documents are real files with their own `.jsonl` evolution logs
(STATE_AND_LIFECYCLE.md §1.1/§3), read on demand, never reconstructed at
startup. Then:

- **If the last main message is a dangling assistant `tool_use`**
  (`_has_dangling_tool_use()`), a tool was interrupted mid-dispatch. Because
  every tool now flushes before dispatch, that call may be *any* tool, not just
  a sub-agent spawn, so the engine schedules `_resume_main_turn()`, which
  resolves each pending `tool_use` **by kind**:
  1. Build a **replay ledger** from the `subsession_*` markers recorded after the
     dangling assistant message. Each `subsession_start` paired with a
     `subsession_end` is *completed* (its stored `result` is reused); an unpaired
     start is the single *active* subsession.
  2. For each dangling `tool_use`:
     - a **sub-agent spawn** (`run_subagent`, in any `run_subagent_<name>` form) is
       re-dispatched through the normal dispatcher, where it consumes the next
       ledger entry instead of starting fresh: a **completed** subsession returns
       its stored result immediately (whatever it wrote to real files is already
       on disk with its own `.jsonl` history — nothing to rebuild, no LLM call);
       the **active** subsession is rehydrated from its `subsessions/<id>.jsonl`
       log and driven to completion **live**, then closed (`subsession_end`
       marker + `subsession.ended`).
     - **`ask_user`** is re-dispatched for real: its only "side effect" is
       asking the present user, so the question batch is simply re-fired
       (`prompt.question`) and the user answers the whole set again from
       scratch — partial answers are never persisted anywhere.
     - **the one call `TransientStore.pending_security_alert` names**, if any
       (at most one — dispatch is strictly sequential) is also re-dispatched
       for real: that field is proof the call was still blocked inside
       `fire_permission`'s wait — never handed to the tool — when the
       interruption happened, so re-dispatching it is safe. Judgement runs
       fresh (picking up e.g. an "always allow" rule granted since) and, if
       still `ask`, the exact same `prompt.permission` is re-fired to the
       user (doc/SECURITY.md §7a) — the "dangling security alert" this
       marker's name refers to.
     - **any other tool** (`filesystem`, `edit_file`, `create_file`, `create_directory`, `run_command`, read-only
       tools, …) is **not** re-executed — its side effects may already have
       landed and there is no per-tool dedup ledger — so it gets a synthesized
       `error`-envelope `tool_result` (`_interrupted_tool_result`) keyed to the
       original `tool_use_id`, telling the model the call didn't complete and was
       not retried. A call that died waiting on a security permission prompt
       lands here too *if* `pending_security_alert` doesn't name it (it must
       have died elsewhere, not at the gate) — same treatment as before: the
       agent sees the interruption and may retry, re-triggering the same
       judgement.
  3. Append the resulting `tool_result`s to `_main_messages`, persist them, and
     continue the **interrupted entry agent's** turn live (the next LLM call).
     The entry agent is recovered from the `entry_agent` tag on the dangling
     assistant message (`_last_entry_agent`), not assumed to be the Guide
     — any entry agent can be the one resumed.

  This is why the user sees Kōdo "recover into that mode, load both the main
  session and the active subsession, and resume the sub-agent's subsession"
  when a spawn was in flight.

- **Otherwise**, if `transient.json` holds a `pending_prompt` (an unanswered
  **approval** — questions never persist one anymore; a legacy `question`
  record is dropped), it is re-surfaced as before. With neither, the session is
  simply idle and awaits the next prompt.

Documents survive a crash for the same reason any other file write does: a
`filesystem`/`edit_file`/`create_file`/`create_directory` call only earns its `new_revision` jsonl entry once
its checkpoint commit has actually landed (STATE_AND_LIFECYCLE.md §1.1), so a
crash mid-write never leaves a half-recorded revision. There is no
producing-subsession union step on resume — a rehydrated sub-agent simply
keeps writing real files and returning verdicts the engine logs; nothing needs to be
merged back into a shared index, because there is no shared index.

### Resume boundaries (what is *not* auto-resumed)

- A crash during the Guide's **own** LLM call (between sub-agents, with no
  dangling `tool_use` on disk) is not auto-continued — the session is left at a
  valid boundary awaiting the next prompt. The explicit guarantee is resuming an
  **interrupted sub-agent**.
- A sub-agent crash mid-leaf-tool resumes from the previous clean turn boundary
  (the in-flight tool call is re-decided by the LLM), so a partially executed
  leaf tool may run again. Leaf tools are written to tolerate this.

## User experience (dividers)

When a sub-agent takes over, the WebView drops a divider into the feed reading
**"<Sub-agent> subagent took over"**; when control returns, a second divider
reads **"<Main agent> resumed from <Sub-agent>"** (e.g. "Kōdo resumed from
Narrative Author"). The sub-agent's own streamed work appears between the two
dividers.

Display names come from the sub-agent's `display_name:` frontmatter field, or are
derived by title-casing the agent name (`narrative_author` → "Narrative Author")
when not set. The Guide's display name is **"Kōdo"**.

On reconnect, the client requests `session.history`. **The server hydrates one
file at a time, never merging them into a single flat array.**
`HistoryProjector.history_entries()` (`kodo/runtime/_engine/_history.py`) walks
`session.jsonl` alone, producing the main array with a takeover/hand-back
divider for each `subsession_start`/`subsession_end` marker — the divider
carries the subsession's id (`subsessionId`) but **not** its content.
`HistoryProjector.subsession_entries(subsession_id)` separately rebuilds
exactly one subsession's own inner transcript, from exactly its own
`<id>.jsonl`, in isolation. `HistoryProjector.full_history()` runs the main
walk, finds every `subsession_start` it emitted, and calls
`subsession_entries()` once per id, returning
`{"entries": [...main...], "subsessions": {id: [...]}}`. The client places
each `subsessions[id]` block right after that subsession's start divider
itself — a one-time, unambiguous splice keyed by id, not a guess based on
what the client's live in-memory feed already happens to contain — so a
reconnecting user still sees a faithful replay of who did what, including
sub-agent work, without the server ever pre-flattening N files into one array
or the client ever needing to reconcile a flat array against live state by
tool-call id (see the superseded design note in git history / the
`kodo-vsix` reducer's `session_history` case for why that used to be
error-prone: any not-yet-flushed content briefly straddled both the "history"
and "live" halves and had to be merged by guesswork).

The one thing this restructuring deliberately leaves untouched is a
**dangling tool call** — a `tool_call`/`ask_user` entry that has no
persisted result yet because it is still genuinely in flight (e.g. inside
the currently active subsession, which only flushes at turn boundaries).
That can never appear in either file's hydrated content by construction, so
the client keeps carrying it forward from its own live state exactly as
before — see WS_PROTOCOL.md §5.11.

## Wire protocol

| Event | Direction | Payload |
| --- | --- | --- |
| `subsession.started` | server → client | `{subsession_id, agent, display_name, task}` |
| `subsession.ended` | server → client | `{subsession_id, agent, display_name, parent_display_name, failed}` |

`task` is the rendered task brief; the client shows it as a `subagent_task` card
right after the start divider.

`session.history`'s wire shape is `{"entries": [...], "subsessions": {id: [...]}}`
(§5.11) — `entries` mirrors the main log alone; `subsession_start`/
`subsession_end` entries there are display-only dividers (`displayName` /
`parentDisplayName` / `subsessionId`) with no inline content. `subsessions`
maps each referenced id to that subsession's own entries, in the same shapes
`entries` uses (`user_message`, `assistant_response`, `tool_call`,
`subagent_task`, etc. — the structured task a sub-agent was seeded with,
reconstructed from the `kind="subagent_task"` seed message).

## Key code

| Concern | Location |
| --- | --- |
| Main log + subsession files + active pointer | `kodo/state/_transient.py` (`TransientStore`) |
| Shared entry-agent loop + persistence | `kodo/runtime/_engine/` (`_run_entry_agent`, `_run_agent_turn`) |
| Subsession lifecycle + replay | `runtime/_engine/` (`_run_subagent`, `_spawn_subagent`, `_drive_subsession`, `_open_subsession`, `_close_subsession`, `_replay_next_subsession`) |
| Spawn permission gate (per-caller `subagents:` allow-list) | `runtime/_engine/` (`_assert_can_spawn`), `subagents/_registry.py` (`allowed_subagents`), `subagents/_loader.py` (`SubAgent.subagents`) |
| Crash resume | `runtime/_engine/` (`start`, `_has_dangling_tool_use`, `_resume_main_turn`, `_last_entry_agent`, `_build_replay_ledger`) |
| History rebuild (one file at a time) | `runtime/_engine/_history.py` (`HistoryProjector.full_history`, `.history_entries`, `.subsession_entries`, `._message_to_entries`, `._divider_entry`) |
| Orphan detection by subsession log | `kodo/runtime/_bootstrap.py` (`__is_orphan`) |
| Display names | `kodo/subagents/_loader.py` (`SubAgent.display_name`) |
| Client dividers | `kodo-vsix/src/extension.ts`, `kodo-vsix/src/webview/main.tsx` |
