# Kodo — State, Persistence, and Lifecycle

> Status: draft for review. Reference: [DESIGN.md](DESIGN.md), [REQUIREMENTS.md](REQUIREMENTS.md).

This document covers how Kodo represents, persists, and recovers state across cold starts, interruptions, and normal operation. It assumes the artifact model from [src/kodo/CLAUDE.md](../src/kodo/CLAUDE.md) — sub-agents communicate exclusively through `publish_artifact` / `read_artifact` against a workspace owned by Kodo.

---

## 1. Directory layout

Kodo owns one directory per project:

```
<project>/
├── src/                      ← user files (user's VCS)
├── gen/                      ← generated files (user's VCS)
└── .kodo/                    ← Kodo-owned, gitignored from user's VCS
    ├── checkpoints/          ← MirrorRepo (git repo, branch "kodo")
    │   ├── .git/
    │   ├── src/              ← mirror of src/ at last checkpoint
    │   └── gen/              ← mirror of gen/ at last checkpoint
    ├── workspace/            ← in-flight artifacts (messy table)
    │   ├── <project_code>/<responsibility_code>/<type>/<artifact_id>.{md,json,…}
    │   └── .retired/<artifact_id>/<exact_filename_with_extension>    ← audit-only
    ├── sessions/             ← session state and LLM audit logs
    │   ├── <posix-timestamp>/         ← one directory per Orchestrator session
    │   │   ├── meta.json              ← session_name, created_at
    │   │   ├── transient.json         ← mutable: stage, last_prompt, autonomous
    │   │   ├── session.jsonl          ← append-only Orchestrator LLM context
    │   │   ├── agents/                ← per-sub-agent invocation JSONL call logs
    │   │   └── mcp/                   ← per-MCP-tool JSONL call logs
    │   └── <session_id>.jsonl         ← flat log per sub-agent invocation (UUID)
    └── orchestrator.session  ← marker file: current Orchestrator session_id
```

`src/` and `gen/` belong to the user. The user's VCS (git, perforce, whatever) tracks them. Kodo writes to them on promotion but does not version-control them; that is the user's choice.

`.kodo/` belongs entirely to Kodo. Kodo SHOULD write `.kodo/` into `.gitignore` on first run; if the user uses a non-git VCS the same exclusion pattern applies in their tool.

One project per VS Code workspace. Bootstrap looks for a single `.kodo/` at the workspace root.

### 1.1 Artifact placement — directories and filenames

Every artifact lands at a deterministic path derived from its `type` and `responsibility_code`. The mapping has two parts: the type-driven base directory, and the responsibility-driven component directory (where applicable).

**Base directory by type:**

`src/` holds human-readable design and specification artifacts — what the project *is* and what it *should do*. `gen/` holds machine-executable artifacts — source code and tests. The split lets the user version-control the two trees with different policies if they choose (e.g., review `src/` changes carefully, treat `gen/` churn as routine).

| Artifact type | Base directory | Scope |
| --- | --- | --- |
| `narrative` | `src/narrative/` | project-wide |
| `tech-stack` | `src/tech_stack/` | project-wide |
| `requirements` | `src/requirements/` | project-wide |
| `architecture` | `src/architecture/` | project-wide |
| `design-plan` | `src/design/` | project-wide |
| `functional-design` | `src/design/<component_dir>/` | per-component |
| `test-plan` | `src/test_design/<component_dir>/` | per-component |
| `code` | `gen/src/<component_dir>/` | per-component |
| `test` | `gen/test/<component_dir>/` | per-component |
| `feedback` | not promoted | workspace-only (see below) |

Within each directory the leaf filename is the artifact's `filename_hint`. Multiple files per `(component, type)` simply land as multiple files in the same directory.

**Component display names and `<component_dir>`:**

Each `responsibility_code` (the short codename — `AUTH`, `TRADE`, `REPORT`) carries a *display name* of up to three words assigned by Architect when the component is declared (`"User Authentication"`, `"Trade Execution"`, `"Reporting"`). The display name is normalised to snake_case and used as the directory name for that component's artifacts:

| Codename | Display name | `<component_dir>` |
| --- | --- | --- |
| `AUTH` | User Authentication | `user_authentication` |
| `TRADE` | Trade Execution | `trade_execution` |
| `REPORT` | Reporting | `reporting` |

A test artifact for `AUTH` therefore lands at `gen/test/user_authentication/<filename_hint>`; its production stub and later real code land at `gen/src/user_authentication/<filename_hint>`; its functional design lands at `src/design/user_authentication/<filename_hint>`; and so on.

The mapping `codename → display name` lives inside the `architecture` artifact's content. Bootstrap parses it when reading the completed `architecture` entry; before architecture exists the mapping is empty and per-component placement is impossible (which is correct — no per-component artifacts can exist until Architect has declared the components).

Architect MUST ensure normalised display names are unique across components — two components whose display names normalise to the same directory name is a name collision Architect catches at design time. The architecture artifact's schema rejects duplicates.

**Why `feedback` is not promoted:**

`feedback` artifacts are workflow scaffolding: they exist to drive critic loops and cross-agent routings. Their final state (accepted or rejected) is recorded in the session logs of both the critic that produced them and the author that received them. Critic acceptance is a precondition for completion, but promotion itself is triggered by the critic's (or solo agent's) explicit `report_artifact_completed` call once the artifact has also cleared its user review gate (§8). The feedback artifact itself adds no value to the project after that point. It is deleted from the workspace when the artifact it reviewed is promoted, and the audit story is carried entirely by the session logs (§5).

---

## 2. Project index

`ProjectIndex` is the single runtime source of truth for the catalog and lifecycle state of every artifact, completed and in-flight. The engine consults it to decide what to schedule next; the Workspace maintains it on every publish, supersession, and completion, and reads it to locate an artifact. It lives in memory and is never persisted as its own file — it is a reflection of on-disk state, reconstructed on every cold start (§3).

### 2.1 Shape

The index is a collection of `IndexEntry` records, one per artifact. Each entry holds **metadata only**; the artifact's content lives solely on disk at `location`. The primary key is `artifact_id`; secondary lookup indexes are maintained over `(project_code, responsibility_code, type)`, `requirement_id`, and `session_id`.

```
IndexEntry:
    artifact_id:          str            # primary key
    project_code:         str
    responsibility_code:  str
    type:                 ArtifactType
    state:                "completed" | "in_flight"
    location:             Path           # absolute path on disk
    filename_hint:        str            # leaf name (stable across revisions)
    supersedes:           list[str]      # prior artifact IDs
    requirement_ids:      list[str]      # requirements this artifact covers
    session_id:           str | None     # set when in_flight
    author:               str            # sub-agent name
    created_at:           datetime
    verdict:              Verdict | None # set on reviewed artifacts
    reviewed_artifact_id: str | None     # set on feedback artifacts
```

A single `(project_code, responsibility_code, type)` may yield **multiple** entries — a component typically has more than one `code` file (a service plus helpers), more than one `test` file (one per logical unit), and so on. Per-component artifacts (`functional-design`, `test-plan`, `test`, `code`) are looked up as a list keyed on the triple. Project-wide artifacts (`narrative`, `architecture`, `requirements`, `tech-stack`, `design-plan`) carry `responsibility_code = project_code` and produce a single entry per triple.

For `state = "completed"` the location is under `<project>/src/` or `<project>/gen/` (mirrored at `<project>/.kodo/checkpoints/`). For `state = "in_flight"` the location is under `<project>/.kodo/workspace/`.

A completed entry and an in-flight entry may coexist for the same `(project_code, responsibility_code, type, filename_hint)` — the completed entry is the prior accepted version, the in-flight entry is the revision under work. The in-flight entry's `supersedes` list contains the completed entry's `artifact_id`. The two entries are distinct artifacts in the index, not two views of the same logical thing.

`read_artifact` calls that filter by `(project_code, responsibility_code, type)` MUST state the requested version explicitly via a `version` parameter:

- `version: "in_flight"` — return the in-flight entry if one exists, raise otherwise. **Critics use this**: their job is to review the artifact under work, so they want the version that has not yet been accepted.
- `version: "stable"` — return the completed (last accepted) entry, raise if none exists. **All other sub-agents use this**: they consume neighbouring artifacts as contracts, and a contract is only binding once accepted. Reading an in-flight neighbour would let an author build on a version that may still change before acceptance.

Calls that filter by `artifact_id` are unambiguous and do not take a `version` parameter — the ID identifies one specific artifact in whatever state it is in. Critics typically receive the artifact under review by `artifact_id` directly from the engine, so the `version="in_flight"` filter form is the fallback path used when a critic needs to fetch additional in-flight context the engine did not inject.

The `version` parameter is required on filter-form `read_artifact` calls; there is no default. The workspace MCP server rejects filter-form calls that omit it, so no consumer can silently pick the wrong version.

`feedback` artifacts are indexed but not surfaced through the per-component lookup; they are reachable through `session_id` lookup and through the `reviewed_artifact_id` they carry. The engine uses them to drive critic loops, not to represent project state.

### 2.2 Derived views

Four views are derived from the index. Each is exposed to the Orchestrator through a tool in its tool surface (FR-ORCH-03); none drive any engine-internal scheduling decision. The Orchestrator consults them as inputs to its reasoning.

- **Frontier per component** — for each `responsibility_code`, the earliest artifact type in the canonical workflow order (`functional-design → test-plan → test → code`) that has zero completed entries. Exposed through `query_frontier()`, a read-only query: an entry counts as completed only once an agent has marked it so via `report_artifact_completed`, not by any inference inside the query. The Orchestrator uses the frontier as a hint when in execution sub-mode (FR-ORCH-07); it MAY deviate when responding to user-driven re-entry. A component with at least one completed `code` entry is treated as code-complete for that component's part of the canonical workflow, even though more code files could be added later by revision.
- **Requirements coverage** — for each requirement ID declared in the current `requirements` artifact, the set of artifacts (per type) whose `requirement_ids` include it. Exposed through `list_artifacts(filters)` queries the Orchestrator composes. Surfaces gaps such as "REQ-AUTH-003 has a `functional-design` entry but no `test-plan` entry covering it" — the Orchestrator's prompt instructs it to detect these and either schedule the missing work or escalate.
- **Artifact lineage** — for each `filename_hint` within `(project_code, responsibility_code, type)`, the chain of `artifact_id`s linked by `supersedes`. Used by the rollback UI and by the Orchestrator when it needs to reason about the most recent accepted version of a revised artifact.
- **Active sessions** — every in-flight entry's `session_id`, mapped to the sub-agent and artifact context. Used by the engine to decide which session to resume on a tool result and by the Orchestrator (through its index snapshot) to know what work is still outstanding from a prior turn.

The index itself is not persisted between runs. It is rebuilt on every cold start from the on-disk state (see §3).

---

## 3. Cold-start index population

Bootstrap runs on every server start. The procedure has four deterministic phases.

### Phase 1 — scan the mirror working tree

`MirrorRepo` is in a clean committed state by invariant (every successful checkpoint produced a commit; nothing else writes to `.kodo/checkpoints/`). The mirror's working tree is therefore an authoritative snapshot of "what was completed as of the last checkpoint".

Bootstrap walks `<project>/.kodo/checkpoints/src/` and `<project>/.kodo/checkpoints/gen/`, deriving `(project_code, responsibility_code, type)` from the directory layout and reading the artifact's `.kodo.json` sidecar (written alongside each promoted file by the Promoter, §8.1) for `artifact_id`, `filename_hint`, `supersedes`, `requirement_ids`, and `author`. Each promoted file produces one `IndexEntry` with `state = "completed"` whose `location` points at the materialized file under `src/`/`gen/`. Where a component contributes multiple files of the same type (e.g., AUTH has `auth_service.py` and `auth_helpers.py`), each file produces its own entry — the index is artifact-granular, not type-granular.

If a completed `requirements` artifact exists, its content is parsed to extract the universe of declared requirement IDs. This universe is what the *Requirements coverage* view (§2.2) compares each artifact's `requirement_ids` against. Without a completed `requirements` artifact, the coverage view is empty and the engine treats coverage gaps as not-yet-detectable.

`MirrorRepo.log()` provides the commit history, used for rollback UI but not for index construction.

### Phase 2 — scan the workspace

Bootstrap walks `<project>/.kodo/workspace/` and produces one `IndexEntry` with `state = "in_flight"` per file. The directory layout `<project_code>/<responsibility_code>/<type>/` makes the bucket self-evident; the file's metadata header carries `artifact_id`, `filename_hint`, `supersedes`, `requirement_ids`, `author`, and `session_id`. Multiple in-flight files of the same `(project_code, responsibility_code, type)` are normal and each produces its own entry.

Where a completed entry and an in-flight entry share the same `(project_code, responsibility_code, type, filename_hint)`, both entries are retained as distinct artifacts in the index; consumers receive whichever version they explicitly request via the `version` parameter on `read_artifact`, per §2.1.

### Phase 3 — classify in-flight sub-agent entries by session presence

For each in-flight entry the engine checks whether the sub-agent session log `<project>/.kodo/sessions/<session_id>.jsonl` (flat file, UUID-keyed) exists.

- **Session log present** → entry is resumable. The engine queues the session for rehydration (see §4).
- **Session log absent** → entry is *orphan*. The engine deletes the orphan file from the workspace and logs the deletion. Orphan artifacts can only arise from a crash between the workspace write and the session log append; the session-log append-before-respond invariant (§4.2) makes this rare but not impossible (e.g., disk full mid-write).

### Phase 4 — locate the Orchestrator session

The Orchestrator's session is recorded by a marker file at `<project>/.kodo/orchestrator.session` containing the current Orchestrator `session_id`. Session IDs are POSIX timestamps (e.g. `1748792400`). The engine reads the marker and checks for the corresponding session directory:

- **Marker present and `sessions/<session_id>/` directory exists** → resume. The engine loads `transient.json` for phase/prompt/autonomous state, replays `session.jsonl` to reconstruct the Orchestrator's message history, and queues its next turn per §4.3. Sub-agent sessions queued in Phase 3 are subordinate: they finish first (the Orchestrator was blocked on whichever `run_subagent` tool call spawned them); their results then flow back into the Orchestrator's resumed loop via the request-ID dedup mechanism (§4.4).
- **Marker present but the named session directory is missing** → the previous Orchestrator session was lost (rare; e.g., disk corruption). The engine logs the anomaly, discards the marker, and falls through to "no marker".
- **No marker** → no prior Orchestrator session. The engine creates a fresh session directory (new POSIX-timestamp `session_id`, writes `meta.json` with `session_name: "Unnamed Session"`, `transient.json` with initial state, updates the marker file). The Orchestrator's first turn is constructed with `{system prompt + index summary + an explicit "cold start" event in the uncached user block}`. The Orchestrator decides what to do based on the index — typically, "no narrative artifact exists, drive intake".

Bootstrap is complete when phases 1–4 finish. The engine now has a populated index, a set of sub-agent sessions to resume, and an Orchestrator session ready to drive.

### 3.1 Post-crash specifics

A crash leaves the workspace in whatever state the OS flushed to disk. Bootstrap's behavior is identical regardless of whether the previous shutdown was clean: scan, scan, classify. There is no "is the workspace dirty" flag — the workspace being non-empty after bootstrap is the dirty signal, and resumable in-flight entries drive recovery automatically.

The `MirrorRepo` cannot be in a partially committed state by construction — git commits are atomic, and `MirrorRepo` exposes no operation that stages without committing. A crash mid-checkpoint either leaves the prior commit as HEAD (writes to mirror working tree not yet committed) or the new commit as HEAD (commit fully landed). Both are valid starting states.

**Broken supersedes lineage.** When bootstrap finds an in-flight workspace artifact whose `supersedes` chain does not connect to the `artifact_id` of the completed entry in the mirror for the same `(project_code, responsibility_code, type, filename_hint)`, the conservative resolution is: keep the completed entry, drop the in-flight artifact (delete the workspace file and close its session log with an entry recording the lineage mismatch). The corresponding stage restarts from the completed entry as its base. The engine logs the anomaly and surfaces it to the user through the extension as a recovered-from-anomaly notice. This case should not arise under the engine's single-worker constraint, but the rule is specified so that if it ever does, behaviour is defined and conservative rather than ambiguous.

---

## 4. Session persistence

Every sub-agent invocation runs inside a *session*. A session is a sequence of messages exchanged with one LLM, identified by a UUID assigned at invocation time. The Orchestrator's session uses the same shape and the same on-disk format as any leaf sub-agent's session — the only differences are its scope (project-lifetime per FR-ORCH-04) and its tool list (the larger Orchestrator surface per FR-ORCH-03).

### 4.1 What is persisted

**Orchestrator session** — persisted as a directory at `<project>/.kodo/sessions/<posix-timestamp>/` containing:

- `meta.json` — `session_name` and `created_at` (written once at session creation).
- `transient.json` — mutable runtime state (`stage`, `last_prompt`, `autonomous`); overwritten in place on each state change.
- `session.jsonl` — append-only LLM context: every message (`role`, `content`) exchanged with the Orchestrator LLM in order.
- `agents/` and `mcp/` — one JSONL call log per sub-agent invocation and per MCP tool call respectively.

**Sub-agent sessions** — persisted as flat JSONL files at `<project>/.kodo/sessions/<session_id>.jsonl` (UUID-keyed). Each line is one message envelope:

```
{
  "ts":        "<iso-8601>",
  "direction": "to_model" | "from_model",
  "role":      "system" | "user" | "assistant" | "tool",
  "content":   <message body>,
  "tool_call": { "request_id": "<uuid>", "name": "...", "input": {...} } | null,
  "tool_result": { "request_id": "<uuid>", "output": ... } | null,
  "metadata":  { "subagent": "...", "project_code": "...",
                 "responsibility_code": "...", "artifact_id_context": [...] }
}
```

A session log is the complete record of one sub-agent's conversation: system prompt, task message, every model response, every tool call, every tool result. Replaying the log in order reconstructs the exact message array the engine would send on the next API call.

### 4.2 Append-before-respond invariant

The engine appends to the session log *before* it acts on the message it just received from the model. Concretely:

1. Model returns a response containing tool calls.
2. Engine writes the model's response to the session log.
3. Engine executes the tool(s) and collects results.
4. Engine writes each tool result to the session log.
5. Engine sends the next API call (with the appended messages).

A crash at any point between steps 1 and 5 leaves the session log either at step 2 (model response logged, tool result missing) or at step 4 (both logged, next call not yet issued). Both states are recoverable: resume reads the log, and either replays from the partial-tool-call state (re-executing the tool, see §4.4) or sends the next API call directly.

### 4.3 Resume on cold start

For each resumable in-flight sub-agent entry identified in Phase 3 of bootstrap, the engine:

1. Loads `<project>/.kodo/sessions/<session_id>.jsonl` (flat JSONL) into memory as the message array.
2. Identifies the resume point — the last logged message and whether any tool call in that message has no matching tool result.
3. If a tool call is pending (no result logged), re-executes it per §4.4 and appends the result.
4. Issues the next API call with the full message array.

The model receives the same context it had before the crash. From its perspective the session continues uninterrupted.

### 4.4 Tool-call re-execution and request-ID dedup

Every tool call carries a `request_id` (UUID) assigned by the engine before dispatch. The engine records, per session, the set of `request_id`s for which a tool result has been logged.

On resume, if the last logged message contains a tool call whose `request_id` has no matching result:

- For **idempotent tools** (`read_artifact`, `toolchain_build`, `toolchain_test`, `query_frontier`, `list_artifacts`, and `ask_user`/`request_user_review_artifact` when no user reply was received): the engine re-runs the tool unconditionally and logs the result.
- For **effectful tools** (`publish_artifact`, `toolchain_deps`, `escalate_blocker`, `report_artifact_completed`): the engine consults the receiving subsystem's request-ID ledger. The workspace records the `request_id` of every successful `publish_artifact` call before returning; on re-execution it detects the duplicate and returns the prior result. `toolchain_deps` and `escalate_blocker` carry the same pattern — the receiver dedupes, never the caller. `report_artifact_completed` is idempotent at the receiver: a re-run finds the entry already `completed` (the staging file already moved out) and returns without re-promoting.

This makes "always re-run, dedupe by request ID" the universal resume policy. The engine does not branch on tool identity; it re-runs, and the receiver decides whether the side effect has already occurred.

The dedup rules extend to Orchestrator tools (FR-ORCH-03):

- `query_frontier`, `list_artifacts` — idempotent reads of the index. Re-run unconditionally.
- `run_subagent`, `run_author_critic_iteration` — effectful. Dedup key is the Orchestrator's tool-call `request_id` against existence of the spawned sub-agent's `session_id`. If the spawned session exists, the engine waits for it to finish (or, if it had already finished, returns its result) instead of spawning a duplicate.
- `ask_user`, `request_user_review_artifact` — effectful (the user's response is the side effect of interest). Dedup key is the `request_id` against the wire's pending-prompt ledger. A re-run after crash finds the pending prompt still outstanding and returns the user's eventual response when it arrives; or if the user already answered before the crash, the response is in the session log and no re-run is needed. In autonomous mode `request_user_review_artifact` resolves immediately with a synthesized acceptance and `ask_user` is not in the tool set at all.
- `rollback` — effectful and destructive. Dedup key is the `request_id` against the mirror commit reached by `MirrorRepo.head_sha()`; if the head already matches `target_sha`, the rollback already happened.
- `finalize_project` — effectful and terminal. Dedup key is the `request_id` against the wire's `state.phase`; if already `done`, return immediately.

### 4.5 Orchestrator-session compaction

The Orchestrator session may run for arbitrarily long — across an entire project from intake to `finalize_project`. When token usage approaches the model's context window (initial threshold: 75%), the engine triggers compaction:

1. **Quiesce.** The engine waits for the Orchestrator's current tool call to complete and for any in-flight sub-agent session it spawned to finish. Compaction does not happen mid-tool-call.
2. **Summarize.** The engine issues a dedicated compaction LLM call: input is the full Orchestrator transcript plus a summarization prompt; output is a compact "prior-context block" capturing decisions made, artifacts produced (by `artifact_id`), current Plan position, outstanding user-blocking moments, and the current responsibility/component under work.
3. **Rotate.** The engine generates a fresh POSIX-timestamp `session_id`, creates a new session directory (`meta.json`, `transient.json`, `session.jsonl`) starting with `{Orchestrator system prompt + the compacted prior-context block + the current index snapshot + a "compaction completed" event in the uncached user block}`, and updates the `<project>/.kodo/orchestrator.session` marker to point at the new session.
4. **Surface.** The engine emits `orchestrator.compacted {from_session_id, to_session_id, summary_excerpt}` over the wire (WS_PROTOCOL.md §5) so the user sees the transition.
5. **Resume.** The Orchestrator's next turn begins from the fresh session. Sub-agent sessions are unaffected — they belong to their own session logs and are referenced by `artifact_id` and `session_id` from the compacted summary, not by message-array content.

The prior Orchestrator session log is **not deleted**; it remains in `sessions/` as immutable audit history per §5.1. Multiple compactions over a project's lifetime produce multiple Orchestrator session logs whose succession is recorded by the `orchestrator.compacted` wire events (and by the fact that each new session's first messages reference the prior `session_id` in their compaction-summary metadata).

A crash during compaction is recoverable: the marker either still points at the old session (step 3 not committed → resume the old session, retry compaction at next threshold check) or points at the new session (step 3 committed → resume the new session). Both states are valid; the engine does not need a separate "compacting" flag.

---

## 5. Audit trail

Session logs serve a second purpose beyond recovery: they are the immutable audit trail of every interaction Kodo had with any LLM.

### 5.1 Invariants

- **No deletion.** Session logs are append-only and never deleted by Kodo. Workspace artifacts come and go on promotion; session logs do not.
- **No mutation.** Existing lines are never rewritten. Corrections happen by appending a new line.
- **One session per sub-agent invocation.** A new invocation gets a new `session_id`, even if it concerns the same artifact. Re-runs after escalation, post-checkpoint re-invocations, and revision loops all produce distinct session files.

The audit trail is the only place that records *why* each artifact looks the way it does. Mirror commits record *what* was produced; session logs record the reasoning that produced it.

### 5.2 Linkage to artifacts

Every workspace artifact carries the `session_id` of the sub-agent invocation that wrote it. Every mirror commit's message includes the `session_id`(s) of the sessions whose accepted artifacts the commit promotes. The chain `mirror commit → session_id → session log` lets any historical artifact be traced back to its full reasoning record.

### 5.3 Storage

Sub-agent session logs live at `<project>/.kodo/sessions/<session_id>.jsonl` (flat files). Orchestrator session logs live at `<project>/.kodo/sessions/<posix-timestamp>/session.jsonl`. Neither is exported, rotated, or compressed by Kodo. A future enhancement may add an external sink (S3, a database) for long-lived projects, but MVP keeps them local.

---

## 6. Conventional start — VS Code opens the project

This is the happy path: user opens a project in VS Code with a previously initialised Kodo workspace.

1. VS Code activates the Kodo extension.
2. Extension launches the Kodo server on a loopback port and opens the WebSocket connection.
3. Server bootstraps (§3): phases 1–4 in order — scans mirror, scans workspace, classifies in-flight sub-agent entries, locates or creates the Orchestrator session.
4. Server constructs the index and the derived views.
5. Extension sends `hello`; server responds with `hello.ack` embedding the current state snapshot (WS_PROTOCOL.md §4.1). Pending sub-agent sessions are queued for rehydration; the Orchestrator session is queued to take its next turn.
6. Extension renders the Kodo panel from the state snapshot.
7. The engine drives whatever Phase 4 produced:
   - **Orchestrator session existed and was resumed** — the engine loads `transient.json` and replays `session.jsonl` to restore the message history (§4.1), then resumes the next turn, re-executing any pending tool call with dedup per §4.4. The Orchestrator's next turn happens automatically; the user sees its `agent.tokens` stream and whichever side effect it produces (a sub-agent spawning, a prompt appearing, an artifact landing).
   - **Orchestrator session was freshly created** — the engine issues its first turn with the index summary in the cached user block and a "cold start" event in the uncached block. The Orchestrator decides what to do: most commonly, no `narrative` artifact present → drive intake by sending a `prompt.question` asking the user to describe what to build (WS_PROTOCOL.md §6.1, §7.1).

No engine-level branch table selects "what to do next" — that table lives in the Orchestrator's prompt. The engine's job at startup is just to put the Orchestrator in a position to decide.

User-visible result: opening the project shows the work as it was when VS Code was last closed, including any in-progress sub-agent runs, pending prompts, or the Orchestrator's next decision arriving as cards in the panel.

---

## 7. Resume after interruption

Interruption covers everything from a clean VS Code reload through a hard power loss. Kodo's recovery strategy is the same for all of them, because the on-disk state is the same.

### 7.1 Procedure

On any cold start, regardless of the reason for the previous shutdown:

1. Bootstrap runs as in §3 (phases 1–4).
2. Index is constructed.
3. Sub-agent sessions are rehydrated per §4.3.
4. The Orchestrator session is rehydrated (Phase 4 located it; engine loads `transient.json` + replays `session.jsonl` per §4.1).
5. Interrupted tool calls in any session are re-executed with dedup per §4.4.
6. Sub-agent sessions finish their pending tool calls; their results flow back into the Orchestrator's tool-call dedup ledger; the Orchestrator's next turn resumes.

There is no "are we recovering?" branch. The procedure that handles clean restarts handles crashes; the only difference is whether bootstrap finds in-flight entries to resume. The Orchestrator does not need to know it was interrupted — its message array is identical to what it was before the crash.

### 7.2 What is recovered, what is not

| Recovered | Not recovered |
|---|---|
| Orchestrator session (full message history) | In-memory engine state outside session logs |
| Sub-agent conversation context (full message history) | Tool calls in flight at moment of crash *only if their result was not logged* — they are re-run |
| Workspace artifacts (in-flight) | Anything the engine held only in RAM (e.g., partial parsing of a streaming response) |
| Completed artifacts (from mirror) | — |
| Per-session request-ID dedup ledger (rebuilt from session logs) | — |
| The Orchestrator's pending user prompts (rebuilt from session log + outbox) | — |

### 7.3 The unrecoverable case

A workspace artifact whose session log is missing — for example because the disk filled between artifact write and log append — is classified orphan and deleted (Phase 3 of bootstrap). The corresponding work restarts when the Orchestrator next decides to re-spawn the sub-agent (typically immediately, since the index shows no completed entry for that slot). This is the only loss-of-work case; in practice it requires a specific failure window of milliseconds.

The Orchestrator's marker file (§3 Phase 4) pointing at a missing session log is handled by creating a fresh Orchestrator session; the project's accumulated state is in the mirror and the workspace, so a new Orchestrator session can pick up coherently — though it loses the prior Orchestrator's in-conversation reasoning state and starts from index-derived facts only.

---

## 8. Promotion — workspace to project + mirror

Promotion fires per artifact, on completion. Completion is the explicit `report_artifact_completed` call made by the artifact's owner — the critic of an author/critic pair, or a solo agent with no critic — once the artifact has passed every gate:

- A critic has published `feedback` with `verdict: "accepted"` targeting the artifact, **and**
- in interactive mode, the user has accepted the artifact at the review gate (`request_user_review_artifact`); in autonomous mode that gate is auto-accepted.

`report_artifact_completed` routes to the engine, which resolves the toolchain from the Tech Stack (`select_toolchain`, parsing the "Primary programming language" line → Python or Node plugin), builds a `ComponentRegistry` from the architecture artifact, and runs the Promoter below. Publication alone never promotes; an author never reports its own work complete.

### 8.1 Mechanism

Promotion is a separate concern from `MirrorRepo`. The promotion mechanism (call it `Promoter`) performs the following atomic-as-possible sequence:

1. Read the completed artifact from the workspace.
2. Write the artifact to its destination under `<project>/src/` or `<project>/gen/`, per the `(responsibility_code, type)` → directory mapping; the leaf filename is the artifact's `filename_hint`. Multiple files per component are normal — each artifact lands at its own path determined by its `filename_hint` within the component's directory.
3. Write the same file into `<project>/.kodo/checkpoints/src/` or `<project>/.kodo/checkpoints/gen/`, alongside a `<filename>.kodo.json` sidecar carrying the artifact's metadata (`artifact_id`, `filename_hint`, `supersedes`, `requirement_ids`, `author`) — the durable record bootstrap reads to reconstruct the completed entry (§3, Phase 1).
4. Call `MirrorRepo.stage_and_commit(message)` with a commit message of the form `<project_code>/<responsibility_code>/<type>: <session_id> → <artifact_id>`.
5. Delete the workspace staging file (the artifact moves out of the workspace).
6. Update `ProjectIndex`: flip the entry's `state` to `completed` and set its `location` to the materialized `src/`/`gen/` path.

A crash between steps 2 and 5 leaves the project and mirror inconsistent with the workspace. The next bootstrap detects this — a workspace artifact whose corresponding project file already matches the workspace content — and completes the promotion by resuming from the failed step. This makes promotion crash-safe at the cost of one extra check per in-flight entry on bootstrap.

### 8.2 MirrorRepo refactor

Current `MirrorRepo.sync_and_commit()` conflates file copying with git operations. The refactor splits responsibilities:

- `MirrorRepo` stays a pure git wrapper: `init()`, `stage_and_commit(message) → sha` (just `git add -A` + `git commit`), `checkout(sha)`, `log()`, `head_sha()`.
- File copying moves to `Promoter`, which writes to both the project tree and the mirror working tree before invoking the commit.

### 8.3 Rollback

Rollback restores the project and mirror to a prior checkpoint commit. It is triggered by the Orchestrator calling its `rollback(target_sha)` tool (FR-ORCH-03), which the engine carries out. Procedure:

1. **Terminate all ongoing sub-agent sessions.** The engine stops dispatching to every active sub-agent session, cancels any in-flight LLM call, and stops accepting tool results. Each terminated session's log is closed with a final entry recording the rollback (`{"direction": "engine", "event": "session_terminated_by_rollback", "target_sha": "<sha>"}`); the session files remain in `sessions/` as immutable audit history.
2. **Terminate the current Orchestrator session.** Same treatment as a sub-agent session: cancel in-flight LLM call, close its log with the rollback entry, retain the file for audit. The reason: the Orchestrator was reasoning about a state that no longer exists; continuing with the same conversation would carry stale assumptions into the post-rollback world.
3. Clear `<project>/.kodo/workspace/` entirely (in-flight work is discarded; `.retired/` audit history is also cleared since it pertains to the abandoned state).
4. `MirrorRepo.checkout(target_sha)` — mirror working tree now reflects the target snapshot.
5. Delete `<project>/src/` and `<project>/gen/` (the directories Kodo owns; the user's broader repo is untouched).
6. Copy `<project>/.kodo/checkpoints/src/` and `<project>/.kodo/checkpoints/gen/` into `<project>/src/` and `<project>/gen/`.
7. Rebuild the in-memory index from the new on-disk state (§3 phases 1 and 2 only; phase 3 finds no in-flight entries because the workspace is empty).
8. **Create a fresh Orchestrator session.** Generate a new POSIX-timestamp `session_id`, create a new session directory (`meta.json`, `transient.json`, `session.jsonl`) with `{Orchestrator system prompt + the rebuilt index snapshot + a "post-rollback start" event in the uncached user block}`, update the `<project>/.kodo/orchestrator.session` marker. The fresh Orchestrator decides what to do based on the restored state; from its perspective, this is a cold start at `target_sha`.
9. Wire surfaces the rollback completion via a `state` event (the user already knows it happened because they confirmed the `ask_user` prompt that preceded the Orchestrator's `rollback` call).

Post-rollback behaviour is indistinguishable from a clean bootstrap whose mirror happens to be at `target_sha`. There is no "rollback mode" the engine subsequently operates in; the rollback is complete the moment the new Orchestrator session takes its first turn.

The user's VCS sees a large file-change set and decides how to record it. Kodo does not touch the user's repository configuration.

---

## 9. Component responsibilities

| Concern | Owner | Notes |
|---|---|---|
| Deciding what runs next | Orchestrator sub-agent | Drives every sub-agent invocation via its tool surface (FR-ORCH-02/03). |
| Hosting the Orchestrator's tool surface | Runtime (`src/kodo/runtime/_tool_surface.py`) | Implements `query_frontier`, `list_artifacts`, `run_subagent`, `run_author_critic_iteration`, `ask_user`, `rollback`, `finalize_project`. `ask_user` is dropped from the surface in autonomous mode. |
| Author/Critic iteration cap and bail logic | Orchestrator's system prompt | Cap (5) and judgment rules live in the prompt, not the engine. |
| Git history of checkpoints | `MirrorRepo` | Pure git wrapper, no file I/O beyond git's own. |
| Move completed artifacts to project + mirror | `Promoter` | Owns the §8.1 sequence; one Promoter run per completed artifact, driven by the engine on `report_artifact_completed`. |
| Project index (`ProjectIndex`) | Workspace + engine | Single runtime source of truth; held in memory, reconstructed on every bootstrap from mirror sidecars (completed) + workspace staging files (in-flight). Workspace maintains it on publish/supersede/complete. |
| Workspace staging storage | Workspace | Writes in-flight artifacts under `.kodo/workspace/`, dedupes by `request_id`, moves them out on completion. |
| Session log append (Orchestrator and sub-agents) | Engine (LLM call wrapper) | Append-before-respond invariant (§4.2). |
| Session rehydration | Engine | Reads session log, computes resume point, re-runs pending tool call. |
| Orchestrator-session compaction | Runtime (`runtime/_compaction.py`) | Triggers at the context-window threshold; surfaces `orchestrator.compacted` (§4.5). |
| Bootstrap orchestration | Engine startup hook | Runs §3 phases 1–4, populates index, queues session resumes, ensures the Orchestrator marker is current. |
| Review-gate and user-prompt blocking | Runtime (`runtime/_gates.py`) | Resolves `ask_user` / `request_user_review_artifact` Futures; in autonomous mode auto-accepts review gates and withholds `ask_user`. |
| Rollback UI trigger | Extension / Kodo panel | User triggers via the panel; the engine accepts the request and reports completion. |
| Rollback execution | Orchestrator + engine | Orchestrator calls `rollback(target_sha)` (after `ask_user` confirmation in interactive mode; directly in autonomous mode); engine performs the §8.3 sequence; a fresh Orchestrator session resumes. |
