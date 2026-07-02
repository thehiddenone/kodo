# Kodo — State, Persistence, and Lifecycle

> Status: draft for review. Reference: [DESIGN.md](DESIGN.md), [REQUIREMENTS.md](REQUIREMENTS.md).

> **Singleton lifecycle update (2026-06-21).** The server is a machine-wide
> **singleton** rooted at the global `~/.kodo` (the per-workspace `.kodo-workspace`
> is gone; sessions/logs/settings live under `~/.kodo`). It advertises itself via
> the `~/.kodo/kodo-server` **discovery file** (`{pid, port}`). On start it
> `sys.exit(1)` if an existing file's **PID is alive OR its port is busy**,
> otherwise it deletes the stale file and claims it. The VS Code launcher mirrors
> this: it reuses a live server (port busy / pid alive) and only spawns when the
> file is absent or stale; a lost launch race (server exit 1) is expected and the
> client just connects to the winner. The singleton **self-reaps** ~30 s after the
> last window disconnects (and removes the discovery file) — unless a turn is
> still mid-flight (any engine in phase `running`), in which case the reap is
> deferred another grace period so a reloading window can reconnect and resume
> the live turn. Per-window ownership,
> the disconnect grace window, and the persisted `owner.json` are covered in
> [SESSIONS.md](SESSIONS.md); cross-session LLM scheduling in
> [LLM_GATEWAY.md](LLM_GATEWAY.md).

This document covers how Kodo represents, persists, and recovers state across cold starts, interruptions, and normal operation. It assumes the file-native model from [CLAUDE.md](../CLAUDE.md) — sub-agents read and write the project's **real files** directly via `filesystem`/`edit_file`/`read_file`; a per-file, append-only `.jsonl` evolution log (`kodo.guided_state`) tracks each document's revision/review history, with status always derived from the last line.

> **Formerly two checkpoint mechanisms existed; now there is one.** Guided mode
> used to run its own artifact-promotion mirror, separate from the generic
> mechanism that commits the real project tree after every file-mutating tool
> call. That bespoke Guided mirror is gone — Guided mode now drives the same
> generic shadow-git mirror (`kodo/mirror/ShadowMirror` +
> `runtime/_checkpoints.RootMirrorManager`) Problem Solver always has, in both
> workflow modes. See [INTERNALS.md §7/§10b/§12.1](INTERNALS.md) for the
> mechanism.

---

## 1. Directory layout

Kodo owns one directory per project:

```
<project>/
├── specs/                    ← user files (user's VCS); agents choose their own paths/names
├── src/                      ← user files (user's VCS)
├── test/                     ← user files (user's VCS)
└── .kodo/                    ← Kodo-owned, gitignored from user's VCS
    ├── checkpoints/          ← the shadow-git mirror (real GIT_DIR/GIT_WORK_TREE split
    │   └── .git/                over the real project tree; both workflow modes)
    ├── guided_dev_state/     ← per-document .jsonl evolution logs (mirrors specs/src/test)
    │   ├── specs/<...>.jsonl
    │   ├── src/<...>.jsonl
    │   └── test/<...>.jsonl
    ├── sessions/             ← session state and LLM audit logs
    │   ├── <posix-timestamp>/         ← one directory per Guide session
    │   │   ├── meta.json              ← session_name, created_at
    │   │   ├── transient.json         ← mutable: stage, last_prompt, autonomous
    │   │   ├── session.jsonl          ← append-only Guide LLM context
    │   │   ├── agents/                ← per-sub-agent invocation JSONL call logs
    │   │   └── subsessions/           ← one JSONL per sub-agent invocation (UUID)
    └── guide.session  ← marker file: current Guide session_id
```

`specs/`, `src/`, and `test/` belong to the user. The user's VCS (git, perforce, whatever) tracks them. Agents write to them directly, with no fixed naming convention — they choose paths that match the project's existing structure.

`.kodo/` belongs entirely to Kodo. Kodo SHOULD write `.kodo/` into `.gitignore` on first run; if the user uses a non-git VCS the same exclusion pattern applies in their tool. The shadow-git mirror's own `info/exclude` already excludes `.kodo/` unconditionally — this is *why* `guided_dev_state/*.jsonl` is never committed by the same mirror that commits the real document changes: "only the author's changes are tracked by git" falls out of an existing exclusion rule, not new logic.

One project per VS Code workspace. Bootstrap looks for a single `.kodo/` at the workspace root.

### 1.1 Document tracking — no fixed placement, no in-memory index

There is no artifact type → directory mapping anymore, and no toolchain-driven file naming. An author chooses any path under `specs/`, `src/`, or `test/` — the only constraint is the existing project-root traversal guard every native file tool already enforces. A path outside those three roots is simply untracked: no `.jsonl` log applies to it.

**Path mapping (`kodo.guided_state.shadow_path`):** `<root>/specs/foo/bar.md` → `<root>/.kodo/guided_dev_state/specs/foo/bar.md.jsonl` (`src/`, `test/` analogously) — a pure, deterministic function of the real path, computed on demand. There is no registry, no component-display-name mapping, nothing to parse out of a document's content to determine where another document should live.

**The four jsonl entry types** (one append-only line each; full schemas in `kodo.guided_state._records`):

1. **`new_revision`** — engine-written, immediately after a `filesystem`/`edit_file` call's checkpoint commit lands under a tracked root. Carries `commit_hash`, `author` (the agent name), `tool`, and `workflow: "guided"|"problem_solving"`. Fired in **both** workflow modes whenever the touched path is tracked under the *bound* project — a Problem-Solver edit of a tracked file is recorded too, so the Guide can reconcile state once Guided mode resumes. This is the only entry type Problem Solver ever produces.
2. **`feedback`** — written by the `document_feedback` tool (critics only): `reviewer`, `accept: bool`, `concerns` (the same shape every critic already uses), `summary`.
3. **`review_result`** — engine-written only, never via a dispatched tool: the user's `decision: "approve"|"reject"` from the interactive document-review gate, plus `comment`.
4. **`accepted`** — engine-written only: the final marker. `commit_hash` is **copied from the immediately preceding `new_revision`** — acceptance never produces a new commit.

**Status derivation** (`kodo.guided_state.derive_status`, from the log's *last* line only):

| Last entry | Status |
|---|---|
| `new_revision` | `pending_review` |
| `feedback`, `accept: false` | `needs_revision` |
| `feedback`, `accept: true` | `pending_acceptance` |
| `review_result`, `decision: reject` | `needs_revision` |
| `review_result`, `decision: approve` | `pending_acceptance` (transient — the engine writes `accepted` in the same flow) |
| `accepted` | `accepted` |

No file outside those four entry types ever has a status query answered — `read_status`/`scan_tracked_files` simply find nothing for an untracked path.

---

## 2. No project index — status is read on demand

There is **no `ProjectIndex`, no in-memory catalog, and nothing reconstructed at
bootstrap.** Every query about a document's state — `guided_dev_status` (the
Guide's tool), `run_author_critic_iteration`'s verdict read, the interactive
review gate — reads that one document's `.jsonl` log fresh, every time. This
replaced the artifact system's `Workspace`/`ProjectIndex` entirely; there is no
successor class hierarchy to learn, just the plain functions in
`kodo.guided_state` (§1.1).

**`guided_dev_status`** (`kodo.guided_state.scan_tracked_files`) is the closest
analogue to the old per-component "frontier" query: it walks
`.kodo/guided_dev_state/` and returns `{path, status, last_event}` for every
tracked document, derived fresh from each log's last line. It is the **only**
read view the Guide consults to decide what to schedule next, and it is
Guided-mode only — the tool errors if called from Problem Solver.

There is no separate "requirements coverage" or "artifact lineage" view
anymore. Traceability (which requirement a document satisfies, which revision
superseded which) was an explicit scope cut when the artifact index was
removed — paths and jsonl history are the only record kept; nothing is
reimplemented to replace the old `requirement_ids`/`supersedes` bookkeeping.

---

## 3. Cold start — nothing to rebuild

There is no bootstrap phase for project state. `bind_project` validates the
`ProjectLayout` (checks `kodo.md` exists and has the right heading) and that is
the entire "project-tier" startup cost. The shadow-git mirror
(`RootMirrorManager`) lazily scaffolds itself the first time a mutating tool
touches a given root — not upfront, not for every root, and identically in
both workflow modes.

The **workspace-tier** session location (which Guide session to resume) is
unrelated to any of this and still runs: `locate_guide_session` reads the
`.kodo/guide.session` marker and either resumes the named session directory or
mints a fresh one. See §4 for what that session itself persists.

The only thing a project's documents "recover" on a cold start is implicit:
their `.jsonl` logs are still sitting on disk, so the very first
`guided_dev_status` call after restart reports exactly the state they were
last left in — there is nothing to rebuild because nothing was ever held only
in memory.

---

## 4. Session persistence

Every sub-agent invocation runs inside a *session*. A session is a sequence of messages exchanged with one LLM, identified by a UUID assigned at invocation time. The Guide's session uses the same shape and the same on-disk format as any leaf sub-agent's session — the only differences are its scope (project-lifetime) and its tool list (the larger Guide surface).

### 4.1 What is persisted

**Guide session** — persisted as a directory at `<project>/.kodo/sessions/<posix-timestamp>/` containing:

- `meta.json` — `session_name` and `created_at` (written once at session creation).
- `transient.json` — mutable runtime state (`stage`, `last_prompt`, `autonomous`, `current_project`); overwritten in place on each state change.
- `session.jsonl` — append-only LLM context: every message (`role`, `content`) exchanged with the entry agent, interleaved with subsession start/end markers.
- `agents/` — one JSONL call log per sub-agent invocation.
- `subsessions/` — one isolated JSONL transcript per sub-agent invocation (UUID-keyed); the sub-agent's own message history, separate from the main session log.

A session log is the complete record of one agent's conversation: system prompt, task message, every model response, every tool call, every tool result. Replaying the log in order reconstructs the exact message array the engine would send on the next API call.

### 4.2 Append-before-respond invariant

The engine appends to the session log *before* it acts on the message it just received from the model. Concretely:

1. Model returns a response containing tool calls.
2. Engine writes the model's response to the session log.
3. Engine executes the tool(s) and collects results.
4. Engine writes each tool result to the session log.
5. Engine sends the next API call (with the appended messages).

A crash at any point between steps 1 and 5 leaves the session log either at step 2 (model response logged, tool result missing) or at step 4 (both logged, next call not yet issued). Both states are recoverable: resume reads the log, and either replays from the partial-tool-call state or sends the next API call directly.

### 4.3 Resume on cold start

For an interrupted main turn — the last persisted message is an assistant message with a `tool_use` block and no matching `tool_result` — the engine rebuilds a replay ledger from the markers recorded after that point (`__build_replay_ledger`): a subsession paired with a `subsession_end` marker is `completed` and its stored structured result is reused verbatim; the one unpaired (active) subsession is rehydrated from its own log and driven to completion live. Once every pending tool call is resolved, the engine appends the tool results and continues the interrupted turn.

The model receives the same context it had before the crash. From its perspective the session continues uninterrupted. There is no artifact-index reconciliation step in this path anymore — a sub-agent's output is whatever it returned via `return_result`, or a bare `{schema_compliance: False}` fallback if it never called it.

### 4.4 Tool-call re-execution

A pending tool call on resume is re-executed through the same `__dispatch_tool_calls` path a live turn uses — there is no special-cased dedup ledger per tool. For a file-mutating tool (`filesystem`/`edit_file`/`run_command`), re-running is safe by construction: `create_file` fails loudly if the file already exists, `edit_file`'s string-match either still applies cleanly or fails with a clear error, and the checkpoint mirror's commit is a no-op when nothing actually changed (`commit_for_path` returns `None` and no `new_revision` entry is appended a second time). Read-only tools (`read_file`, `guided_dev_status`) are naturally idempotent.

### 4.5 Context compaction

An entry agent (the Guide, or the Problem Solver — they share one agent-agnostic main message history, `__main_messages`) may run for arbitrarily long. After **every** entry-agent turn the engine measures the live context in tokens (the just-finished call's `input + cache_read + cache_write + output` ≈ what the next call will carry) and pushes it to the client as `context.stats` (WS_PROTOCOL.md §5). The limit is the **current model's context window** — the per-model `context_window` in `kodo/llms/_registry.py`, resolved for the entry-agent model selected in settings (`__context_limit`; *not* a global setting — see SETTINGS.md §2.3). Once that measure reaches **90%** of the window the engine compacts **in place** — it does **not** rotate to a new session:

1. **Trigger.** Auto-compaction runs at the end of the turn (after the LLM has responded and the session is `awaiting_user`). The user may also trigger it manually at any idle moment via the header's **Compact now** button, which sends `compact.now`; both paths funnel through the single-consumer worker queue, so compaction never races a live turn or a tool call.
2. **Summarize.** The engine runs the **`compactor`** sub-agent (a tool-less, single-shot summarizer; `subagents/subagent_compactor.md`) directly — like the session titler, never via `run_subagent`. The current `__main_messages` are flattened to a plain-text transcript and handed to it as one user message; its output is a compact "prior-context block" capturing the goal, decisions, a **mandatory "Files changed" section** (every file created/edited/moved/deleted so far, each by its **exact path** — so the resumed agent never re-edits, re-creates, or undoes its own work), progress (plan/component position), durable tool results, open items, and the next step. The call streams nothing to the feed; only its USD cost is folded into the running total.
3. **Mark + reset.** A `compaction` marker — `{type, summary, reason, tokens_before, tokens_after, ts}` — is appended to `session.jsonl`. The live `__main_messages` is then reset to a **single** synthetic user message wrapping that summary, and the gauge is reseeded from a char-based estimate until the next real turn supplies a measured count.
4. **Surface.** The engine emits `context.compacting {active}` to bracket the run (the WebView shows a "Compacting context, please hold on" indicator with running dots) and, on completion, `context.compacted {summary_excerpt, summary, tokens_before, tokens_after}` so a clickable "✦ Context compacted" divider drops into the feed; expanding it reveals the full `summary` (the exact post-compaction context), styled like a thinking block.

**Model-switch compaction.** The context limit follows the model, so a model switch can cross the threshold instantly. `config.reload` (sent window-globally when the user changes the model) calls `WorkflowEngine.handle_config_changed()` on **every** live session; it compares the new entry-agent model key to the one that last drove the main context (`__active_model_key`). If the new model's window is **smaller than the live context size**, the engine compacts **first, using the outgoing model** (`__run_compaction(reason="model_switch", force_model_key=<old>)`) — so the switch only takes effect on a context that fits the new window — then records the new model and re-emits `context.stats`. Funnelled through the worker queue, so it never races a turn.

**Audit & replay.** The full `session.jsonl` is never rewritten: lines before the marker stay as immutable audit history (§5.1) and are still replayed into the client feed by `history_entries` (the WebView shows the whole conversation — before and after every compaction, of which there may be many). What changes is only the *LLM message history*: `__load_main_messages` rebuilds it from the **latest** `compaction` marker onward (the summary block + every message appended after it), so the pre-compaction transcript is never resent to the model. The **system prompt is untouched** — it is rebuilt fresh on every turn from `AgentRegistry.get()`, so both the security and performance preambles (INTERNALS.md) are always present after a compaction; compaction can never strip them.

**Crash safety.** The marker is a single appended line. A crash before it lands → the next resume rebuilds the full (un-compacted) context and re-checks the threshold on the next turn; a crash after it lands → resume rebuilds from the summary. Both states are valid; the engine needs no separate persisted "compacting" flag (the in-memory `__compacting` bool only gates the live UI and the manual trigger).

---

## 5. Audit trail

Session logs serve a second purpose beyond recovery: they are the immutable audit trail of every interaction Kodo had with any LLM.

### 5.1 Invariants

- **No deletion.** Session logs are append-only and never deleted by Kodo. Documents and their evolution logs come and go on disk like any project file; session logs do not.
- **No mutation.** Existing lines are never rewritten. Corrections happen by appending a new line.
- **One session per sub-agent invocation.** A new invocation gets a new `subsession_id`, even if it concerns the same document. Re-runs after escalation and revision loops all produce distinct subsession files.

The audit trail is the only place that records *why* a document looks the way it does. A document's own jsonl log records *what* happened to it and *when* (a revision landed, a critic reviewed it, the user decided); session logs record the reasoning that produced each of those events.

### 5.2 Linkage to documents

A `new_revision` jsonl entry carries the `author` agent name; a `feedback` entry carries the `reviewer`. Neither carries a `session_id` — tracing a specific revision back to the full conversation that produced it means correlating the entry's timestamp/author against that agent's subsession logs under `sessions/<main-id>/subsessions/`, not a direct foreign key. This is a deliberate simplification versus the old artifact system, which stamped every artifact with its producing `session_id`.

### 5.3 Storage

Sub-agent subsession logs live at `<project's session dir>/subsessions/<subsession_id>.jsonl`. Guide session logs live at `<project>/.kodo/sessions/<posix-timestamp>/session.jsonl`. Neither is exported, rotated, or compressed by Kodo. A future enhancement may add an external sink (S3, a database) for long-lived projects, but MVP keeps them local.

---

## 6. Conventional start — VS Code opens the project

This is the happy path: user opens a project in VS Code with a previously initialised Kodo project.

1. VS Code activates the Kodo extension.
2. Extension launches the Kodo server on a loopback port and opens the WebSocket connection.
3. Server attaches the session: `locate_guide_session` resumes or creates the Guide session marker; if a project was previously bound for this session, `bind_project` re-validates its `ProjectLayout`. There is no index to construct — nothing else runs at this point.
4. Extension sends `hello`; server responds with `hello.ack` embedding the current state snapshot (WS_PROTOCOL.md §4.1).
5. Extension renders the Kodo panel from the state snapshot.
6. The engine drives whatever was found:
   - **Session existed and was resumed** — the engine loads `transient.json` and replays `session.jsonl` to restore the message history (§4.1), then resumes the next turn, re-executing any pending tool call per §4.4. The Guide's next turn happens automatically; the user sees its `agent.tokens` stream and whichever side effect it produces (a sub-agent spawning, a prompt appearing, a file landing).
   - **Session was freshly created** — the engine issues its first turn with a "cold start" event. The Guide decides what to do: most commonly, no Narrative document present → drive intake by sending a `prompt.question` asking the user to describe what to build (WS_PROTOCOL.md §6.1, §7.1).

No engine-level branch table selects "what to do next" — that table lives in the Guide's prompt. The engine's job at startup is just to put the Guide in a position to decide (now backed by `guided_dev_status` instead of an index summary).

User-visible result: opening the project shows the work as it was when VS Code was last closed, including any in-progress sub-agent runs, pending prompts, or the Guide's next decision arriving as cards in the panel.

---

## 7. Resume after interruption

Interruption covers everything from a clean VS Code reload through a hard power loss. Kodo's recovery strategy is the same for all of them, because the on-disk state is the same.

### 7.1 Procedure

On any cold start, regardless of the reason for the previous shutdown:

1. The session is located/resumed (§3, §6).
2. The Guide session is rehydrated (engine loads `transient.json` + replays `session.jsonl` per §4.1).
3. Any dangling tool call (a main turn interrupted while a sub-agent held the floor) is resolved per §4.3.
4. Sub-agent subsessions finish their pending tool calls; their results flow back into the resumed main turn; the entry agent's next turn resumes.

There is no "are we recovering?" branch. The procedure that handles clean restarts handles crashes; the only difference is whether the last persisted message has a dangling `tool_use`. The Guide does not need to know it was interrupted — its message array is identical to what it was before the crash.

### 7.2 What is recovered, what is not

| Recovered | Not recovered |
|---|---|
| Guide session (full message history) | In-memory engine state outside session logs |
| Sub-agent conversation context (full message history) | Tool calls in flight at moment of crash — they are re-run (§4.4) |
| Every document on disk, exactly as last written, with its full `.jsonl` evolution history | Anything the engine held only in RAM (e.g., partial parsing of a streaming response) |
| The Guide's pending user prompts (rebuilt from session log + outbox) | — |

### 7.3 The unrecoverable case

There is no orphan-artifact case anymore — a document is just a file, and a file surviving a crash needs no special reconciliation. The closest analogue is a `new_revision` jsonl entry appended without the matching real-file write having fully landed; this cannot happen by construction, since the engine only appends `new_revision` *after* the mirror commit (§1.1) confirms the write succeeded.

---

## 8. Document acceptance — no promotion step

Acceptance fires per document, after a critic calls `document_feedback(path, accept=True, concerns=[])`. There is **no promotion** — the document was already a real file the moment its author wrote it; there is nothing to materialize, no toolchain to consult for a target path, no sidecar to write.

### 8.1 Mechanism

The engine's post-dispatch hook (right where `checkpoint_sha` is already injected into a tool's result, see INTERNALS.md §12.1) watches for a successful `document_feedback` call with `accept: true` and calls `__finalize_document(path)`:

1. **Autonomous mode** — immediately append an `accepted` entry to the document's `.jsonl` log, reusing the most recent `new_revision`'s `commit_hash`.
2. **Interactive mode** — fire the same approval gate the old `request_user_review_artifact` tool used to, now driven by the engine directly (no tool indirection). On agreement: append `review_result` (`decision: "approve"`) then `accepted`. On feedback: append `review_result` (`decision: "reject"`, the user's comment) — no `accepted` entry; the next `run_author_critic_iteration` round on that path picks this up as `needs_revision`.

A critic never decides what happens after `accept: true` — it has no further obligation once it calls `document_feedback`.

### 8.2 The unified checkpoint mirror

There is one shadow-git mirror per root, shared by both workflow modes (`RootMirrorManager`/`ShadowMirror`). A `filesystem`/`edit_file` call's checkpoint commit, when its path is tracked, also drives a `new_revision` jsonl append in the same post-dispatch step — see §1.1. There is no separate "promotion mirror" anymore, and nothing analogous to the old `MirrorRepo`/sidecar-file refactor: the real project tree *is* the mirror's work tree (a true `GIT_DIR`/`GIT_WORK_TREE` split, not a copy).

### 8.3 Rollback

Rollback restores a project's checkpoint mirror to a prior commit. It is triggered by the Guide calling its `rollback(target_sha)` tool, which the engine carries out by delegating directly to `RootMirrorManager.rollback` — the same primitive that backs Problem Solver's "Rollback to this state" checkpoint-card control. Procedure:

1. The engine moves the project root's mirror branch to `target_sha` (a real branch-ref move; any orphaned tip is preserved on a `rollback_<ts>` branch, never a detached HEAD — it remains reachable as "Roll forward to this state").
2. The real working tree (which *is* the mirror's work tree) now reflects the target snapshot directly — no separate copy/delete/restore dance across `specs/`/`src/`/`test/` is needed, because there was never a second copy of the files to reconcile.
3. The engine resets the in-memory conversation (`__main_messages = []`) — the Guide was reasoning about a state that no longer exists.

There is no index to rebuild and no fresh-session ceremony beyond the conversation reset — the next turn simply starts from whatever `guided_dev_status` reports for the restored tree, which is correct automatically because every document's `.jsonl` log is itself just a file under `.kodo/`, restored along with everything else the rollback touched (or, if the log lived outside the rolled-back commit's history because `.kodo/` is excluded from the mirror entirely — see §1.1 — it is simply left as-is, reflecting the project's tracked-document history independent of the rollback. This is intentional: rolling back code/specs should not erase the *record* that a now-reverted revision was once reviewed.).

The user's VCS sees a large file-change set and decides how to record it. Kodo does not touch the user's repository configuration.

---

## 9. Component responsibilities

| Concern | Owner | Notes |
|---|---|---|
| Deciding what runs next | Guide sub-agent | Drives every sub-agent invocation via its tool surface. |
| Hosting the Guide's tool surface | `kodo.tools` (dispatch) + `kodo.runtime._engine` (services) | `guided_dev_status`, `run_subagent`, `run_author_critic_iteration`, `ask_user`, `rollback`, `finalize_project`. `ask_user` is dropped from the surface in autonomous mode. |
| Author/Critic iteration cap and bail logic | Guide's system prompt | Cap (5) and judgment rules live in the prompt, not the engine. |
| Per-document evolution log | `kodo.guided_state` | Pure functions: append/read each document's `.jsonl` log; status always derived from the last line. No in-memory index. |
| Checkpoint mirror (both workflow modes) | `kodo.mirror.ShadowMirror` + `runtime._checkpoints.RootMirrorManager` | Real `GIT_DIR`/`GIT_WORK_TREE` split over the actual project tree; commits after every mutating tool call. |
| Document acceptance / review gate | Engine (`__finalize_document`, `runtime._engine`) | Triggered from the post-dispatch hook after `document_feedback(accept: true)`; no tool fires this — autonomous mode auto-accepts, interactive mode drives the approval gate directly. |
| Session log append (Guide and sub-agents) | Engine (LLM call wrapper) | Append-before-respond invariant (§4.2). |
| Session rehydration | Engine | Reads session log, computes resume point, re-runs pending tool call. |
| Context compaction | Runtime (`runtime/_engine.py`, `compactor` sub-agent) | Auto-triggers at 90% of the current model's context window (or manual `compact.now`, or a switch to a smaller-window model); summarises in place via a `compaction` marker; surfaces `context.stats` / `context.compacting` / `context.compacted` (§4.5). |
| Session location at startup | `runtime._bootstrap.locate_guide_session` | Workspace-tier marker + `sessions/` lookup only — no project-tier index to populate. |
| Review-gate and user-prompt blocking | Runtime (`runtime/_gates.py`) | Resolves `ask_user` / document-review Futures; in autonomous mode auto-accepts review gates and withholds `ask_user`. |
| Rollback UI trigger | Extension / Kodo panel | User triggers via the panel; the engine accepts the request and reports completion. |
| Rollback execution | Guide + engine | Guide calls `rollback(target_sha)` (after `ask_user` confirmation in interactive mode; directly in autonomous mode); engine delegates to `RootMirrorManager.rollback`; conversation resets. |
