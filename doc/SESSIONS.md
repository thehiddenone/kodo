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
>   it in `hello`. The old workspace-level orchestrator-session **marker** and its
>   auto-resume are gone (`OrchestratorMarker`/`locate_orchestrator_session`
>   remain in the tree but are unused by the engine).
> - **A session's nature is its `current_project`:** `None` ⇒ problem-solving
>   only (openable in any window); set ⇒ guided-associated, linked to that
>   `kodo.md` project and openable only where that project is loaded. The session
>   picker enforces this gate in the extension.
> - **Crash-resume** of a mid-subagent turn is unchanged, and now runs when the
>   `SessionManager` (re)loads that specific session.
> - LLM scheduling across sessions is owned by the shared gateway — see
>   [LLM_GATEWAY.md](LLM_GATEWAY.md).

## The two levels

Kōdo's persisted state has exactly two levels:

1. **The main session** — the top-level conversation between the user and
   whichever *entry agent* is currently driving (the **Orchestrator** in Guided
   Project Workflow, or the **Problem Solver** in Problem-Solving mode). There is
   exactly one main session per project directory at a time.

2. **Subsessions** — each time the main agent spawns a sub-agent (e.g. a
   Narrative Author, an Architect, a Critic), that sub-agent runs in its own
   **isolated** message history called a subsession. A subsession has a
   session-wide-unique ID and is stored alongside the main session.

The main session is **not tied to an agent.** The user can switch between
Orchestrator and Problem Solver and back within a single session; both entry
agents **share one message history** (`session.jsonl`). Switching mode only
swaps the system prompt and the available tools — the conversation continues
seamlessly across the change.

> Sub-agent spawning is **not** wired to the Orchestrator. Any agent may spawn
> sub-agents if (a) its frontmatter grants a spawning tool
> (`run_subagent`/`run_author_critic_iteration`) and (b) its frontmatter
> declares a `subagents:` allow-list naming the sub-agents it may call. The
> engine gates every spawn against the **calling** agent's allow-list
> (`AgentRegistry.allowed_subagents` → `__assert_can_spawn`), so the permission
> travels with whichever agent makes the call — not with a hard-coded
> orchestrator identity. Today only the Orchestrator opts in (the Problem Solver
> ships without a spawning tool, so in Problem-Solving mode there are no
> subsessions), but the path is fully agent-agnostic and crash-resume recovers
> *whichever* entry agent was holding the floor (see `__last_entry_agent`).

## On-disk layout

```
.kodo/sessions/<main-session-id>/
    meta.json          — session name + creation time
    transient.json     — mutable runtime state: stage, last prompt, autonomous,
                         pending_prompt, and active_subsession (the resume hook)
    session.jsonl      — the MAIN session log (see below)
    subsessions/
        <subsession-id>.jsonl   — one per sub-agent run; the sub-agent's full,
                                  isolated message history
    agents/
        <agent>.jsonl  — per-call usage stats (cost/tokens), unrelated to context
```

`<main-session-id>` is the orchestrator session ID minted at bootstrap (a POSIX
timestamp). `<subsession-id>` is a random hex ID minted per `run_subagent` call.

### `session.jsonl` — the main log

`session.jsonl` is an append-only log that interleaves **two kinds of lines**:

- **Message lines** — `{"role": "user"|"assistant", "content": ..., "entry_agent": "orchestrator"|"problem_solver"}`.
  These are the top-level LLM context. `entry_agent` is a display/audit tag only;
  because the two entry agents share context, every message replays into the one
  `__main_messages` list regardless of tag.
- **Marker lines** — `{"type": "subsession_start"|"subsession_end", ...}`.
  These record, *in chronological position*, when a sub-agent took over and when
  it handed control back. They carry `subsession_id`, `agent`, `display_name`,
  and `parent_display_name`; `subsession_end` also carries the sub-agent's
  `result` (the artifact IDs it published).

`TransientStore.read_messages()` returns only the message lines (for rebuilding
LLM context); `read_session_lines()` returns everything (for resume and history
rebuild).

## Thinking blocks

Extended-thinking text is not a UI-only side channel. The engine's shared turn
loop (`__run_agent_turn`) accumulates every `ThinkingDelta` it streams to the
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
(`llms/anthropic/_claude.py`) now requests thinking on every call (`thinking:
{type: "enabled", budget_tokens: ...}`), captures that signature from the
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
`WorkflowEngine.__message_to_entries` now also emits a `thinking_block` entry
(sourced from the persisted `"thinking"` block) ahead of the
`assistant_response` entry for an assistant message, so `session.history`
replays it exactly where it appeared live, with the same collapsible UI.

## Persistence (the "append-before-respond" guarantees)

### Main turns

A main turn's messages are normally flushed to `session.jsonl` at the end of the
turn. The one exception is the **spawning-tool prefix**: right before the engine
dispatches a `run_subagent` / `run_author_critic_iteration` call, it flushes the
not-yet-persisted prefix of the turn — **including the assistant message that
contains the spawning `tool_use`** — to disk. (See `_SUBAGENT_SPAWNING_TOOLS`
and the `flush_before` argument to `__run_agent_turn`.)

This is what makes resume possible: if the process dies mid-sub-agent, the main
log ends with a dangling assistant `tool_use` that has **no** following
`tool_result`. That dangling `tool_use` is the signal that a sub-agent was
in-flight.

Turns that contain no sub-agent (plain text, `query_frontier`, `ask_user`, …)
are *not* flushed until they complete, so a crash mid-`ask_user` leaves nothing
half-written and the existing `pending_prompt` re-surfacing path is unaffected.

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
published `result`), clears `active_subsession`, and emits `subsession.ended`.
So at any instant, `active_subsession` names the one subsession that "is the
active one right now," exactly as the user model requires.

## Resume

On every server start, `ProjectBootstrap` locates the main session and the
engine reloads `__main_messages` from `session.jsonl`. Then:

- **If the last main message is a dangling assistant `tool_use`**
  (`__has_dangling_tool_use()`), a sub-agent was interrupted. The engine
  schedules `__resume_main_turn()`:
  1. Build a **replay ledger** from the `subsession_*` markers recorded after the
     dangling assistant message. Each `subsession_start` paired with a
     `subsession_end` is *completed* (its stored `result` is reused); an unpaired
     start is the single *active* subsession.
  2. Re-dispatch the dangling `tool_use`(s) through the normal dispatcher. During
     this replay, each `run_subagent` call consumes the next ledger entry instead
     of starting fresh:
     - a **completed** subsession returns its stored result immediately (its
       artifacts are already on disk and were rebuilt into the index at
       bootstrap — no LLM call);
     - the **active** subsession is rehydrated from its `subsessions/<id>.jsonl`
       log and driven to completion **live**, then closed (`subsession_end`
       marker + `subsession.ended`).
  3. Append the resulting `tool_result`s to `__main_messages`, persist them, and
     continue the **interrupted entry agent's** turn live (the next LLM call).
     The entry agent is recovered from the `entry_agent` tag on the dangling
     assistant message (`__last_entry_agent`), not assumed to be the Orchestrator
     — any agent permitted to spawn can be the one resumed.

  This is why the user sees Kōdo "recover into that mode, load both the main
  session and the active subsession, and resume the sub-agent's subsession."

- **Otherwise**, if `transient.json` holds a `pending_prompt` (an unanswered
  `ask_user`/approval), it is re-surfaced as before. With neither, the session is
  simply idle and awaits the next prompt.

Published artifacts survive a crash because `publish_artifact` stamps each
artifact with the producing subsession's ID. On resume, a resumed sub-agent
unions any pre-crash publishes (found by `session_id == subsession_id` in the
rebuilt index) with anything it publishes during the resumed run, so the
Orchestrator receives the complete result.

### Resume boundaries (what is *not* auto-resumed)

- A crash during the Orchestrator's **own** LLM call (between sub-agents, with no
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
when not set. The Orchestrator's display name is **"Kōdo"**.

On reconnect, the client requests `session.history`; the server rebuilds the full
feed by walking `session.jsonl` in order, emitting the dividers and **splicing
each sub-agent's full inner transcript** (read back from its subsession log)
between them, so a reconnecting user sees a faithful replay of who did what —
including sub-agent work.

## Wire protocol

| Event | Direction | Payload |
| --- | --- | --- |
| `subsession.started` | server → client | `{subsession_id, agent, display_name}` |
| `subsession.ended` | server → client | `{subsession_id, agent, display_name, parent_display_name}` |

`session.history` entries gained two display-only types: `subsession_start` and
`subsession_end`, each carrying `displayName` / `parentDisplayName`.

## Key code

| Concern | Location |
| --- | --- |
| Main log + subsession files + active pointer | `kodo/state/_transient.py` (`TransientStore`) |
| Shared entry-agent loop + persistence | `kodo/runtime/_engine.py` (`__run_entry_agent`, `__run_agent_turn`) |
| Subsession lifecycle + replay | `_engine.py` (`__run_subagent`, `__spawn_subagent`, `__drive_subsession`, `__open_subsession`, `__close_subsession`, `__replay_next_subsession`) |
| Spawn permission gate (per-caller `subagents:` allow-list) | `_engine.py` (`__assert_can_spawn`), `subagents/_registry.py` (`allowed_subagents`), `subagents/_loader.py` (`SubAgent.subagents`) |
| Crash resume | `_engine.py` (`start`, `__has_dangling_tool_use`, `__resume_main_turn`, `__last_entry_agent`, `__build_replay_ledger`) |
| History rebuild (full inner replay) | `_engine.py` (`history_entries`, `__message_to_entries`, `__divider_entry`) |
| Orphan detection by subsession log | `kodo/runtime/_bootstrap.py` (`__is_orphan`) |
| Display names | `kodo/subagents/_loader.py` (`SubAgent.display_name`) |
| Client dividers | `kodo-vsix/src/extension.ts`, `kodo-vsix/src/webview/main.tsx` |
