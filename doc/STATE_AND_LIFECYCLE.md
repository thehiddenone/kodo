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
    │   └── <project_code>/<responsibility_code>/<type>/<artifact_id>.{md,json,…}
    └── sessions/             ← LLM audit log (append-only, never cleaned)
        └── <session_id>.jsonl
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

`feedback` artifacts are workflow scaffolding: they exist to drive critic loops and cross-agent routings. Their final state (accepted or rejected) is recorded in the session logs of both the critic that produced them and the author that received them, and the latest accepted feedback is what triggers promotion of the artifact under review. The feedback artifact itself adds no value to the project after that point. It is deleted from the workspace when the artifact it reviewed is promoted, and the audit story is carried entirely by the session logs (§5).

---

## 2. Internal index

The engine maintains an in-memory index covering both completed and in-flight artifacts. The index is the single structure the engine consults to decide what to schedule next.

### 2.1 Shape

The index is a collection of `IndexEntry` records, one per artifact. The primary key is `artifact_id`; secondary lookup indexes are maintained over `(project_code, responsibility_code, type)`, `requirement_id`, and `session_id`.

```
IndexEntry:
    artifact_id:         str            # primary key
    project_code:        str
    responsibility_code: str
    type:                ArtifactType
    state:               "completed" | "in_flight"
    location:            Path           # absolute path on disk
    filename_hint:       str            # leaf name (stable across revisions)
    supersedes:          list[str]      # prior artifact IDs
    requirement_ids:     list[str]      # requirements this artifact covers
    session_id:          str | None     # set when in_flight
    author:              str            # sub-agent name
    last_modified:       datetime
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

The engine derives four views from the index:

- **Frontier per component** — for each `responsibility_code`, the earliest artifact type in the canonical workflow order (`functional-design → test-plan → test → code`) that has zero completed entries. The frontier is the next stage to schedule for that component. A component with at least one completed `code` entry is treated as code-complete for that component's part of the workflow, even though more code files could be added later by revision.
- **Requirements coverage** — for each requirement ID declared in the current `requirements` artifact, the set of artifacts (per type) whose `requirement_ids` include it. Surfaces gaps such as "REQ-AUTH-003 has a `functional-design` entry but no `test-plan` entry covering it" and "REQ-NOTIFY-001 is declared but no component claims it". This view is what the engine and the user consult to verify nothing has been dropped on the floor between stages.
- **Artifact lineage** — for each `filename_hint` within `(project_code, responsibility_code, type)`, the chain of `artifact_id`s linked by `supersedes`. Used for audit and rollback UI.
- **Active sessions** — every in-flight entry's `session_id`, mapped to the sub-agent and artifact context. Used by the engine to decide which session to resume on a tool result.

The index itself is not persisted between runs. It is rebuilt on every cold start from the on-disk state (see §3).

---

## 3. Cold-start index population

Bootstrap runs on every server start. The procedure has three deterministic phases.

### Phase 1 — scan the mirror working tree

`MirrorRepo` is in a clean committed state by invariant (every successful checkpoint produced a commit; nothing else writes to `.kodo/checkpoints/`). The mirror's working tree is therefore an authoritative snapshot of "what was completed as of the last checkpoint".

Bootstrap walks `<project>/.kodo/checkpoints/src/` and `<project>/.kodo/checkpoints/gen/`, deriving `(project_code, responsibility_code, type)` from the directory layout and reading each file's metadata header for `artifact_id`, `filename_hint`, `supersedes`, `requirement_ids`, and `author`. Each file produces one `IndexEntry` with `state = "completed"`. Where a component contributes multiple files of the same type (e.g., AUTH has `auth_service.py` and `auth_helpers.py`), each file produces its own entry — the index is artifact-granular, not type-granular.

If a completed `requirements` artifact exists, its content is parsed to extract the universe of declared requirement IDs. This universe is what the *Requirements coverage* view (§2.2) compares each artifact's `requirement_ids` against. Without a completed `requirements` artifact, the coverage view is empty and the engine treats coverage gaps as not-yet-detectable.

`MirrorRepo.log()` provides the commit history, used for rollback UI but not for index construction.

### Phase 2 — scan the workspace

Bootstrap walks `<project>/.kodo/workspace/` and produces one `IndexEntry` with `state = "in_flight"` per file. The directory layout `<project_code>/<responsibility_code>/<type>/` makes the bucket self-evident; the file's metadata header carries `artifact_id`, `filename_hint`, `supersedes`, `requirement_ids`, `author`, and `session_id`. Multiple in-flight files of the same `(project_code, responsibility_code, type)` are normal and each produces its own entry.

Where a completed entry and an in-flight entry share the same `(project_code, responsibility_code, type, filename_hint)`, both entries are retained as distinct artifacts in the index; consumers receive whichever version they explicitly request via the `version` parameter on `read_artifact`, per §2.1.

### Phase 3 — classify in-flight entries by session presence

For each in-flight entry the engine checks whether `<project>/.kodo/sessions/<session_id>.jsonl` exists.

- **Session log present** → entry is resumable. The engine queues the session for rehydration (see §4).
- **Session log absent** → entry is *orphan*. The engine deletes the orphan file from the workspace and logs the deletion. Orphan artifacts can only arise from a crash between the workspace write and the session log append; the session-log append-before-respond invariant (§4.2) makes this rare but not impossible (e.g., disk full mid-write).

Bootstrap is complete when phases 1–3 finish. The engine now has a populated index and a set of sessions to resume.

### 3.1 Post-crash specifics

A crash leaves the workspace in whatever state the OS flushed to disk. Bootstrap's behavior is identical regardless of whether the previous shutdown was clean: scan, scan, classify. There is no "is the workspace dirty" flag — the workspace being non-empty after bootstrap is the dirty signal, and resumable in-flight entries drive recovery automatically.

The `MirrorRepo` cannot be in a partially committed state by construction — git commits are atomic, and `MirrorRepo` exposes no operation that stages without committing. A crash mid-checkpoint either leaves the prior commit as HEAD (writes to mirror working tree not yet committed) or the new commit as HEAD (commit fully landed). Both are valid starting states.

**Broken supersedes lineage.** When bootstrap finds an in-flight workspace artifact whose `supersedes` chain does not connect to the `artifact_id` of the completed entry in the mirror for the same `(project_code, responsibility_code, type, filename_hint)`, the conservative resolution is: keep the completed entry, drop the in-flight artifact (delete the workspace file and close its session log with an entry recording the lineage mismatch). The corresponding stage restarts from the completed entry as its base. The engine logs the anomaly and surfaces it to the user through the extension as a recovered-from-anomaly notice. This case should not arise under the engine's single-worker constraint, but the rule is specified so that if it ever does, behaviour is defined and conservative rather than ambiguous.

---

## 4. Session persistence

Every sub-agent invocation runs inside a *session*. A session is a sequence of messages exchanged with one LLM, identified by a UUID assigned at invocation time.

### 4.1 What is persisted

Session state is persisted as a JSONL file at `<project>/.kodo/sessions/<session_id>.jsonl`. Each line is one message envelope:

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

For each resumable in-flight entry identified in Phase 3 of bootstrap, the engine:

1. Loads `<project>/.kodo/sessions/<session_id>.jsonl` into memory as the message array.
2. Identifies the resume point — the last logged message and whether any tool call in that message has no matching tool result.
3. If a tool call is pending (no result logged), re-executes it per §4.4 and appends the result.
4. Issues the next API call with the full message array.

The model receives the same context it had before the crash. From its perspective the session continues uninterrupted.

### 4.4 Tool-call re-execution and request-ID dedup

Every tool call carries a `request_id` (UUID) assigned by the engine before dispatch. The engine records, per session, the set of `request_id`s for which a tool result has been logged.

On resume, if the last logged message contains a tool call whose `request_id` has no matching result:

- For **idempotent tools** (`read_artifact`, `toolchain_build`, `toolchain_test`, all `narrative_*` dialog tools when no user reply was received): the engine re-runs the tool unconditionally and logs the result.
- For **effectful tools** (`publish_artifact`, `toolchain_deps`, `escalate_to_user`): the engine consults the receiving subsystem's request-ID ledger. The workspace MCP server records the `request_id` of every successful `publish_artifact` call before returning; on re-execution it detects the duplicate and returns the prior result. `toolchain_deps` and `escalate_to_user` carry the same pattern — the receiver dedupes, never the caller.

This makes "always re-run, dedupe by request ID" the universal resume policy. The engine does not branch on tool identity; it re-runs, and the receiver decides whether the side effect has already occurred.

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

Session logs live at `<project>/.kodo/sessions/<session_id>.jsonl`. They are not exported, rotated, or compressed by Kodo. A future enhancement may add an external sink (S3, a database) for long-lived projects, but MVP keeps them local.

---

## 6. Conventional start — VS Code opens the project

This is the happy path: user opens a project in VS Code with a previously initialised Kodo workspace.

1. VS Code activates the Kodo extension.
2. Extension launches the Kodo server on a loopback port and opens the WebSocket connection.
3. Server bootstraps (§3): scans mirror, scans workspace, classifies in-flight entries.
4. Server constructs the index and the derived views (frontier per component, active sessions).
5. Server sends the extension an initial state message: index summary, frontier, and any active sessions awaiting input.
6. Extension renders the Kodo panel with the current project state.
7. The engine selects the next action based on the index and active sessions:
   - **Active sessions present** — the engine resumes them per §4.3.
   - **No active sessions, index empty** (fresh project, no `narrative` artifact yet) — the engine sends a *getting started* message to the user: a short explanation of how Kodo works, that the first step is to describe the project to **Narrative Author**, and a primary action button that initiates the Narrative Author session. The user types the project description; the engine starts the session.
   - **No active sessions, mid-workflow, no pending gate** — the engine schedules the next sub-agent based on the frontier view (§2.2) and starts the session automatically. The user sees the new session appear in the panel.
   - **No active sessions, mid-workflow, pending approval gate** — the engine surfaces the gate to the user with the artifacts awaiting approval; resumes the dependent stage once the user accepts.
   - **No active sessions, all components code-complete** — the engine sends a *project complete* message. Detailed behaviour for this state is out of scope for MVP.

User-visible result: opening the project shows the work as it was when VS Code was last closed, including any in-progress sub-agent runs, pending gates, or onboarding state.

---

## 7. Resume after interruption

Interruption covers everything from a clean VS Code reload through a hard power loss. Kodo's recovery strategy is the same for all of them, because the on-disk state is the same.

### 7.1 Procedure

On any cold start, regardless of the reason for the previous shutdown:

1. Bootstrap runs as in §3.
2. Index is constructed.
3. Active sessions are rehydrated per §4.3.
4. Interrupted tool calls are re-executed with dedup per §4.4.
5. The engine resumes the workflow.

There is no "are we recovering?" branch. The procedure that handles clean restarts handles crashes; the only difference is whether bootstrap finds in-flight entries to resume.

### 7.2 What is recovered, what is not

| Recovered | Not recovered |
|---|---|
| Sub-agent conversation context (full message history) | In-memory engine state outside session logs |
| Workspace artifacts (in-flight) | Tool calls in flight at moment of crash *only if their result was not logged* — they are re-run |
| Completed artifacts (from mirror) | Anything the engine held only in RAM (e.g., partial parsing of a streaming response) |
| Per-session request-ID dedup ledger (rebuilt from session logs) | — |

### 7.3 The unrecoverable case

A workspace artifact whose session log is missing — for example because the disk filled between artifact write and log append — is classified orphan and deleted (Phase 3 of bootstrap). The corresponding stage restarts fresh. This is the only loss-of-work case; in practice it requires a specific failure window of milliseconds.

---

## 8. Promotion — workspace to project + mirror

Promotion fires per artifact, on acceptance. Acceptance is one of:

- A critic publishes `feedback` with `verdict: "accepted"` targeting the artifact.
- The artifact has no critic and is published (Narrative Author's final state; Architect when no critic loop is configured).

### 8.1 Mechanism

Promotion is a separate concern from `MirrorRepo`. The promotion mechanism (call it `Promoter`) performs the following atomic-as-possible sequence:

1. Read the accepted artifact from the workspace.
2. Write the artifact to its destination under `<project>/src/` or `<project>/gen/`, per the `(responsibility_code, type)` → directory mapping; the leaf filename is the artifact's `filename_hint`. Multiple files per component are normal — each artifact lands at its own path determined by its `filename_hint` within the component's directory.
3. Write the same file into `<project>/.kodo/checkpoints/src/` or `<project>/.kodo/checkpoints/gen/`.
4. Call `MirrorRepo.stage_and_commit(message)` with a commit message of the form `<project_code>/<responsibility_code>/<type>: <session_id> → <artifact_id>`.
5. Delete the workspace artifact file.
6. Update the in-memory index: remove the in-flight entry, replace/insert the completed entry.

A crash between steps 2 and 5 leaves the project and mirror inconsistent with the workspace. The next bootstrap detects this — a workspace artifact whose corresponding project file already matches the workspace content — and completes the promotion by resuming from the failed step. This makes promotion crash-safe at the cost of one extra check per in-flight entry on bootstrap.

### 8.2 MirrorRepo refactor

Current `MirrorRepo.sync_and_commit()` conflates file copying with git operations. The refactor splits responsibilities:

- `MirrorRepo` stays a pure git wrapper: `init()`, `stage_and_commit(message) → sha` (just `git add -A` + `git commit`), `checkout(sha)`, `log()`, `head_sha()`.
- File copying moves to `Promoter`, which writes to both the project tree and the mirror working tree before invoking the commit.

### 8.3 Rollback

Rollback restores the project and mirror to a prior checkpoint commit. Procedure:

1. **Terminate all ongoing sub-agent sessions.** The engine stops dispatching to every active session, cancels any in-flight LLM call, and stops accepting tool results. Each terminated session's log is closed with a final entry recording the rollback (`{"direction": "engine", "event": "session_terminated_by_rollback", "target_sha": "<sha>"}`); the session files remain in `sessions/` as immutable audit history.
2. Clear `<project>/.kodo/workspace/` entirely (in-flight work is discarded).
3. `MirrorRepo.checkout(target_sha)` — mirror working tree now reflects the target snapshot.
4. Delete `<project>/src/` and `<project>/gen/` (the directories Kodo owns; the user's broader repo is untouched).
5. Copy `<project>/.kodo/checkpoints/src/` and `<project>/.kodo/checkpoints/gen/` into `<project>/src/` and `<project>/gen/`.
6. Rebuild the in-memory index from the new on-disk state (§3 phases 1 and 2 only; phase 3 finds no in-flight entries because the workspace is empty).
7. Compute the frontier per component from the rebuilt index (§2.2) and resume normal operation — the engine schedules the next sub-agent based on the frontier and either auto-starts it or surfaces a pending gate, exactly as in the conventional-start procedure (§6, step 7).

Post-rollback behaviour is indistinguishable from a clean bootstrap whose mirror happens to be at `target_sha`. There is no "rollback mode" the engine subsequently operates in; the rollback is complete the moment the index is rebuilt and the frontier is computed.

The user's VCS sees a large file-change set and decides how to record it. Kodo does not touch the user's repository configuration.

---

## 9. Component responsibilities

| Concern | Owner | Notes |
|---|---|---|
| Git history of checkpoints | `MirrorRepo` | Pure git wrapper, no file I/O beyond git's own. |
| Move accepted artifacts to project + mirror | `Promoter` | New module; owns the §8.1 sequence. |
| In-memory artifact index | Workflow engine | Rebuilt on every bootstrap from on-disk state. |
| Workspace artifact storage | Workspace MCP server | Writes under `.kodo/workspace/`, dedupes by `request_id`. |
| Session log append | Engine (LLM call wrapper) | Append-before-respond invariant (§4.2). |
| Session rehydration | Engine | Reads session log, computes resume point, re-runs pending tool call. |
| Bootstrap orchestration | Engine startup hook | Runs §3 phases, populates index, queues session resumes. |
| Rollback UI and trigger | Extension / Kodo panel | Invokes engine API; engine performs §8.3. |
