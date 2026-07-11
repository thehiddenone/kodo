"""Typed message-type constants for the Kōdo wire protocol.

The constants in this module are the stable ``payload["type"]`` strings used
by both sides of the WebSocket. They are grouped by frame role so the dispatch
layer can pre-filter by direction. The authoritative catalogue is documented
in [doc/WS_PROTOCOL.md](../../doc/WS_PROTOCOL.md).

Three role-based prefixes:

- ``MSG_*`` — client→server request payload types (§7 of WS_PROTOCOL.md).
- ``SREQ_*`` — server→client request payload types, i.e. user prompts (§6).
- ``EVT_*`` — server→client event payload types (§5).

Response payload type strings are not exported here; they are tied 1:1 to the
originating request via ``correlation_id`` and never participate in dispatch.

A trailing ``DEPRECATED — legacy`` section retains constants used by code that
has not yet migrated to the WS_PROTOCOL.md catalogue. Removal will happen as
the handler wiring is updated.

Every constant below is commented with: what role it plays in the workflow,
when it fires, and its side effects on both client (kodo-vsix) and server
(kodo). Comments were verified against the actual handler/emitter code, not
just the docs — where the two disagreed, the doc has been corrected.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Client → Server request payload types  (WS_PROTOCOL.md §7)
# ---------------------------------------------------------------------------

# Client → Server. First frame on every connection, both connection roles.
# Session role (default, no payload.role): binds a new session (empty
# session_id) or resumes an existing one. Server side effects: binds the
# connection, replies hello.ack (session_id, current_project, a full `state`
# snapshot, llama/model status, detected_vram_gb, detected_ram_gb), then
# pushes EVT_STATE, EVT_SESSION_NAME, session.history (if the resumed
# session has prior turns), and finally replays any backlog buffered while
# this session was disconnected — strictly after session.history, so a
# reconnect can't see a live frame before scrollback.
# Control role (payload.role == "control", the sidebar's session-less
# connection): no session is created; the ack carries only the llama/model
# snapshot (incl. detected_vram_gb, detected_ram_gb), and the client uses it
# to reconcile this window's remembered-open sessions once it lands.
MSG_HELLO = "hello"

# Client → Server. The user's submitted prompt text (payload: {text}), with any
# attachment control line still attached. Server strips the attachment marker,
# persists the clean prompt, enqueues it on the session's worker, and replies
# prompt.accepted immediately; the worker later dequeues it, freezes the two
# frozen mode toggles for this turn, runs the session titler on the session's
# first prompt, and routes it to the Guide or Problem Solver per workflow_mode.
# An empty prompt is rejected with an EVT_ERROR (code "empty_prompt"). Client
# renders the text as an optimistic user_message bubble immediately and clears
# the input box and any staged attachment chips.
MSG_PROMPT_SUBMIT = "prompt.submit"

# Client → Server. Cancels the in-flight turn (FR-LLM-07). Server cancels the
# worker task — aborting any in-flight LLM stream — folds whatever text/
# thinking/tool_use had streamed so far into a persisted (possibly partial)
# assistant message, and transitions state.phase to "stopped", the one
# unambiguous signal (unlike a normal turn ending on awaiting_user/done/error)
# that a Stop, specifically, happened. Client reacts to the following EVT_STATE
# phase=="stopped" by silencing every "waiting" indicator (thinking/toolgen/
# awaiting-LLM/run_command progress) and dropping an "Interrupted by user"
# callout into the feed.
MSG_STOP = "stop"

# Client → Server. List all persisted sessions for the picker. Server replies
# with ``{sessions: [{id, name, project_root, taken}]}``. VSIX derives
# openability (project loaded? taken?) from those fields; also used to
# reconcile a window's remembered-open sessions after a reload the panel
# serializer couldn't restore (see kodo-vsix extension.ts).
MSG_SESSION_LIST = "session.list"

# Client → Server. Release the session named by ``payload.session_id`` from
# this window's ownership immediately (graceful window close), so another
# window can open it. Sent from a session tab's dispose handler, but skipped on
# window reload/deactivate (the disconnect grace lets this window reclaim it)
# and skipped when the session is mid-delete (already gone).
MSG_SESSION_RELEASE = "session.release"

# Client → Server. Permanently delete the session named by ``payload.session_id``:
# stop its engine and physically remove its directory under ``sessions/`` (the
# project it worked on is untouched). On success the server closes the socket
# (the client reads the closure as confirmation and disposes the tab); on
# failure it replies ``{type: "session.delete.error", message}`` and leaves the
# socket open (client hides its "Deleting…" progress notification and shows the
# error). Client clears the webview feed (``session_cleared``) right after
# sending this, ahead of the server's reply.
MSG_SESSION_DELETE = "session.delete"

# Client → Server. Fetch the persisted CheckpointState ({current_index,
# entries: [...]}) for ``payload.root`` — the same per-root shadow-mirror state
# the client already gets pushed via EVT_CHECKPOINT_STATE after every mutation,
# and embedded per-call in each tool_call's ``checkpoint`` field. Implemented
# server-side (``WorkflowEngine.handle_checkpoint_list``, replies
# ``checkpoint.list.done``), but the current VSIX client never sends it — there
# is no dedicated checkpoint-browsing UI yet, so this is effectively unused
# today (WS_PROTOCOL.md previously — incorrectly — listed it as unimplemented;
# see the doc fix alongside this comment).
MSG_CHECKPOINT_LIST = "checkpoint.list"

# Client → Server. Stateful checkpoint actions on one mirror commit, triggered
# from a tool-call card. All four carry ``{root, sha}`` plus an optional
# ``resolution`` (``"stash"|"discard"``, supplied only on retry after a
# ``*.needs_confirmation`` reply caused by a dirty work tree — see below).
# ``undo``/``redo`` are a per-entry toggle: surgically revert/reapply only the
# files that commit changed, each as a new forward commit, flipping that
# entry's persisted ``undone`` flag. ``rollback``/``roll_forward`` are the same
# underlying operation in both directions: move the mirror's branch ref
# directly to ``sha``, preserving whatever tip it orphans under a
# ``rollback_<ts>`` branch (never a detached HEAD) — see
# ``kodo.mirror.ShadowMirror.rollback``. The server replies either
# ``checkpoint.<verb>.done`` with the updated CheckpointState, or
# ``checkpoint.<verb>.needs_confirmation`` (no ``resolution`` given and the
# tree is dirty — i.e. has edits Kodo didn't make); the client resubmits with a
# chosen ``resolution`` to proceed (a native "Stash & Continue"/"Discard &
# Continue" modal). The server also pushes ``EVT_CHECKPOINT_STATE`` after any
# successful mutation so every checkpoint button in the transcript can refresh,
# not just the one acted on.
MSG_CHECKPOINT_ROLLBACK = "checkpoint.rollback"
MSG_CHECKPOINT_ROLL_FORWARD = "checkpoint.roll_forward"
MSG_CHECKPOINT_UNDO = "checkpoint.undo"
MSG_CHECKPOINT_REDO = "checkpoint.redo"

# Client → Server. Toggles Autonomous/Interactive mode (``{autonomous: bool}``).
# One of the two *frozen* toggles (paired with ``workflow.set``): applies to
# the *next* prompt only — an in-flight turn keeps the ``effective_autonomous``
# it was frozen with at dequeue. Server updates session state, replies
# ``mode.accepted``, and follows with an updated EVT_STATE.
MSG_MODE_SET = "mode.set"

# Client → Server. Selects the top-level workflow for the next prompt:
# ``"guided"`` (Guide + full pipeline) or ``"problem_solving"`` (standalone
# Problem Solver); unknown values fall back to ``"guided"``. The other frozen
# toggle — same next-prompt-only semantics as ``mode.set``. Server replies
# ``workflow.accepted`` and follows with an updated EVT_STATE.
MSG_WORKFLOW_SET = "workflow.set"

# Client → Server. Set the Edit Control posture.
# Payload: ``{edit_control: "review_all"|"allow_all"|"smart"}``. Unlike
# mode.set/workflow.set this is NEVER frozen: the client owns the value (forcing
# "allow_all" while Autonomous is in effect) and the server mirrors whatever it
# last sent, so the stored value is always exactly what the UI shows. (State
# tracking only — no edit gate is enforced yet; not part of the security layer.)
MSG_EDIT_CONTROL_SET = "edit_control.set"

# Client → Server. Set the Command Control posture — the security layer's mode
# (doc/SECURITY.md). Payload: ``{command_control: "defensive"|"permissive"|"smart"}``.
# Mirrored exactly like edit_control.set (client forces "permissive" under
# Autonomous); the tool dispatcher reads the stored value live per tool call
# and an "ask" verdict fires SREQ_PROMPT_PERMISSION.
MSG_COMMAND_CONTROL_SET = "command_control.set"

# Client → Server. Push the VS Code workspace folder map (logical name →
# physical path) plus the physical root. Sent on connect and on every
# workspace-folders change; the server rebuilds its WorkspaceLayout
# logical-root map. Payload: ``{physical_root, folders: {name: path}}``.
MSG_WORKSPACE_FOLDERS = "workspace.folders"

# Client → Server. Bind the session's current project (Guided mode). Sent once,
# lazily, when the user first runs Guided after picking a project. Payload:
# ``{root, name}``. Immutable for the session — a second, different value is
# rejected with an EVT_ERROR; server side effect on the first bind is
# EVT_PROJECT_BOUND.
MSG_PROJECT_SET = "project.set"

# Client → Server. Scaffold a new project directly (no LLM round-trip) — backs
# the VS Code "Create Project" command, which already has a concrete folder
# from its own picker dialog. Payload: ``{path, name?, force?}`` (``name``
# optional, ``path`` always supplied by the client). Shares
# ``WorkflowEngine._create_project`` with the ``create_new_project`` tool, so
# the server also pushes EVT_WORKSPACE_ADD_FOLDER on success. Replies
# ``project.create.done`` ``{path, name}`` on success or ``project.create.error``
# ``{message}`` if ``path``'s ``kodo.md`` already exists and ``force`` wasn't set.
MSG_PROJECT_CREATE = "project.create"

# Client → Server. ⟪planned⟫ — persistent user-defined allow/deny rules layered
# ahead of the per-call security judgement (doc/SECURITY.md §9, FR-SEC-07).
# Defined here but no handler is registered for it on the server and nothing on
# the client sends it yet; it is inert today.
MSG_SECURITY_ADD_RULE = "security.add_rule"

# Client → Server. Manually trigger context compaction for this session. Honoured
# only when the entry agent is idle (``state.phase == "awaiting_user"``) and
# there is context to compact; otherwise ignored. Drives the same path as the
# automatic 90%-threshold trigger — the engine runs the ``compactor`` sub-agent,
# writes a ``compaction`` marker to ``session.jsonl``, and resets the live LLM
# context. Client only enables the button when the latest EVT_CONTEXT_STATS had
# ``can_compact: true``.
MSG_COMPACT_NOW = "compact.now"

# Client → Server. Tells the server to re-read ``settings.json`` (user +
# project layers). The primary use is model switching: the VSIX writes the
# ``models`` map (or ``mode``) to ``~/.kodo/etc/settings.json`` and sends this so
# every *live* session's engine notices before its next LLM dispatch (settings
# are read fresh per call) and, via ``handle_config_changed``, compacts right
# away if the newly selected model's context window is smaller than what's
# already in use.
MSG_CONFIG_RELOAD = "config.reload"

# Client → Server. Window-global local-model management, sent over the
# session-less control connection (extension.ts sidebar), never a session
# connection. ``llamacpp.install`` still streams EVT_LLAMACPP_INSTALL_PROGRESS
# on the requesting connection until done (that install is a one-shot binary
# fetch with no pause/resume). The ``local_llm.*`` download commands below are
# fire-and-forget instead: the handler kicks off the transfer in the
# background and replies immediately with ``local_llm.registry_state``.
# Progress itself is **not** pushed over the wire at all — kodo-vsix polls
# ``manager-state.json`` directly off disk (doc/LOCAL_MODEL_MANAGER.md §11),
# which is what lets a download keep running (and stay watchable) across the
# requesting connection/window closing entirely.
#   local_llm.install {name} — start (or continue) a fresh download
#   local_llm.resume  {name} — resume a paused/failed download by id alone
#   local_llm.pause   {name} — signal an in-flight download to stop between chunks
MSG_LLAMACPP_INSTALL = "llamacpp.install"
MSG_LOCAL_LLM_INSTALL = "local_llm.install"
MSG_LOCAL_LLM_RESUME = "local_llm.resume"
MSG_LOCAL_LLM_PAUSE = "local_llm.pause"
MSG_LLAMA_START = "llama.start"
MSG_LLAMA_STOP = "llama.stop"

# Client → Server. Synchronous local-model switch (WS_PROTOCOL.md §7.6a).
# ``{name}`` is a *local registry* name. The server persists the selection
# into ``~/.kodo/etc/settings.json`` (``mode: "local"`` + ``models.local``),
# restarts llama-server for the new model, waits until it is actually
# serving (or has failed to start), and only then replies
# ``llm.select.done {ok, model, error?}``. Unlike the VSIX's settings-write +
# ``config.reload`` + ``llama.start`` dance, the reply *confirms readiness* —
# built for ``kodo.validator``'s LUT↔VLLM swaps (doc/VALIDATOR.md §9), where
# the next frame must already hit the requested model. Model loads take
# minutes; callers need a generous response timeout.
MSG_LLM_SELECT = "llm.select"

# Client → Server. Session-less one-shot completion (WS_PROTOCOL.md §7.6b):
# ``{prompt, system?, json_schema?}`` runs a single tool-less turn on the
# currently selected *local* model, scheduled through the shared LLMGateway
# feed like any session dispatch, and replies ``llm.complete.done
# {ok, model, text, error?}`` with the full concatenated response text (no
# stream frames reach the client; ``llm.waiting`` may). ``json_schema``
# constrains the output via llama-server's grammar enforcement — the
# validator's UPP answers rely on it being parseable by construction. Not an
# agent turn: no tools, no session, no feed events, no persistence.
MSG_LLM_COMPLETE = "llm.complete"

# Client → Server. Local Inference Settings webview actions (doc/LLM_REGISTRY.md),
# sent over the control connection like the block above. All mutate the
# server-owned ``~/.kodo/etc/local-llm-registry.json`` and reply with
# ``local_llm.registry_state`` (the merged registry + override path) on the
# same connection — there is no separate ack payload.
#   local_llm.add_huggingface {name, description, repo_id, filename,
#                              llama_args?, context_window?}
#   local_llm.add_file        {name, description, path, llama_args?, context_window?}
#   local_llm.add_server_url  {name, description, url}
#   local_llm.uninstall       {name} — frees the downloaded GGUF, keeps the entry
#                                       (also the "cancel a download" action)
#   local_llm.remove          {name} — removes a custom entry (hardcoded ones
#                                       are rejected); uninstalls first if needed
MSG_LOCAL_LLM_ADD_HUGGINGFACE = "local_llm.add_huggingface"
MSG_LOCAL_LLM_ADD_FILE = "local_llm.add_file"
MSG_LOCAL_LLM_ADD_SERVER_URL = "local_llm.add_server_url"
MSG_LOCAL_LLM_UNINSTALL = "local_llm.uninstall"
MSG_LOCAL_LLM_REMOVE = "local_llm.remove"

# Client → Server. Global llama-server binary override (doc/LLM_REGISTRY.md) —
# not a model, a replacement for the executable kodo launches for every local
# model (hardcoded and custom alike), e.g. a CUDA-enabled custom build on
# Linux. ``llama_server_override.set`` payload is ``{path}``; ``.remove``
# carries no payload. Both reply with ``local_llm.registry_state``.
MSG_LLAMA_SERVER_OVERRIDE_SET = "llama_server_override.set"
MSG_LLAMA_SERVER_OVERRIDE_REMOVE = "llama_server_override.remove"

# ---------------------------------------------------------------------------
# Server → Client — API key management  (WS_PROTOCOL.md §6.3/§6.4)
# ---------------------------------------------------------------------------

# Server → Client request. Sent when the engine resolves a cloud LLM plugin and
# holds no key in memory for that vendor. Client answers from VS Code
# SecretStorage, or prompts the user and stores what they enter; concurrent
# requests for the same vendor are serialized client-side (only one "enter key"
# dialog at a time). A cancelled prompt replies ``{error: "cancelled"}``.
SREQ_API_KEY_REQUEST = "api_key.request"

# Server → Client event (no reply). Sent when a cloud LLM call fails with HTTP
# 401 — alongside a non-recoverable EVT_ERROR — telling the client to delete
# the now-invalid stored key (VS Code SecretStorage) for that vendor so the
# next call re-prompts instead of retrying the same rejected key.
EVT_API_KEY_REVOKE = "api_key.revoke"

# ---------------------------------------------------------------------------
# Server → Client request payload types — user prompts  (WS_PROTOCOL.md §6)
#
# These are ``kind=request`` frames the server initiates. The client's reply
# is a ``kind=response`` whose ``correlation_id`` equals the request's ``id``.
# ---------------------------------------------------------------------------

# Server → Client request. Surfaced when any agent calls ``ask_user`` (e.g. the
# Narrative Author or Problem Solver eliciting input, or the Guide's own
# judgment-call questions) or ``escalate_blocker`` (a single free-text-only
# question) — carries the agent's whole question batch in one request.
# Withheld entirely in autonomous mode (the tool is not offered to the agent).
# The originating ``tool_use`` is flushed to ``session.jsonl`` before dispatch
# (so a reconnect mid-question already sees the panel via session.history), and
# no ``pending_prompt`` is persisted: a server restart re-drives the whole batch
# from scratch through the dangling-tool-use resume path, never partial
# answers. Client renders an in-feed panel of question boxes and replies once,
# on "Confirm and Send".
SREQ_PROMPT_QUESTION = "prompt.question"

# Server → Client request. Fired by the engine itself — never a sub-agent tool
# call — right after a critic calls ``document_feedback(path, accept=True)``:
# the Guide pipeline's document-review sign-off gate. Auto-accepted with no
# prompt in autonomous mode (the engine writes the ``accepted`` jsonl entry
# directly). Unlike questions, the pending prompt IS persisted, so a server
# restart re-surfaces the same gate on resume. Client shows an approve/feedback
# panel; on "agree" the engine appends ``review_result``+``accepted`` jsonl
# entries (no sub-agent call), on feedback it appends
# ``review_result(decision: "reject")`` and the next author/critic round on
# that path reads it as ``needs_revision``.
SREQ_PROMPT_APPROVAL = "prompt.approval"

# Server → Client request. Fired when the security layer's verdict on a gated
# tool call is "ask" (doc/SECURITY.md) — the dispatcher blocks the call until
# the user allows or denies. No ``pending_prompt`` is persisted: a crash
# mid-prompt resolves through the dangling-tool-use resume path (the
# un-executed call gets an interrupted stand-in, and the agent may simply
# retry, re-triggering the same judgement). Client shows a transient permission
# panel — not a session entry, the gated tool call's own card records the
# outcome. On allow the call dispatches normally; on deny the tool is *not*
# executed and the agent receives a "user DENIED permission" error result.
SREQ_PROMPT_PERMISSION = "prompt.permission"

# ---------------------------------------------------------------------------
# Server → Client event payload types — visibility  (WS_PROTOCOL.md §5)
# ---------------------------------------------------------------------------

# Server → Client event. The complete session snapshot — phase, the two frozen
# toggles (autonomous/workflow_mode) with their effective twins, the two
# never-frozen toggles (edit_control/command_control), and current_agent — never
# a delta. Pushed on every phase transition, mode-toggle change, and turn
# freeze/unfreeze, and embedded again as ``state`` inside ``hello.ack`` so
# first-connect and reconnect share one client code path. ``phase`` is the
# client's authoritative "is a turn running" signal, and ``phase=="stopped"``
# is the one unambiguous marker of an explicit user Stop. Note: the current
# VSIX client does not read ``current_agent`` off this event — the live
# "who has the floor" name comes from EVT_AGENT_STARTED/EVT_AGENT_FINISHED
# instead (its own ``stage``/``agent`` reads here are vestigial and always
# default).
EVT_STATE = "state"

# Server → Client events. Bracket one agent turn holding the floor — fired for
# every entry-agent turn (Guide/Problem Solver) and every sub-agent turn alike
# (``EngineEmitters.emit_agent_started``/``emit_agent_finished``, also used by
# the crash-resume path). ``component`` is the Guide's current responsibility
# code, or ``null`` outside Guided mode. Client uses these — not
# ``state.current_agent`` — to drive the visible "<agent> is working" label.
EVT_AGENT_STARTED = "agent.started"
EVT_AGENT_FINISHED = "agent.finished"

# Server → Client event. Emitted before every dispatched tool call except
# ``ask_user`` (which fires SREQ_PROMPT_QUESTION instead of a tool-call card),
# so the client can render a one-line activity entry keyed by ``tool_call_id``
# (the ``tool_use`` block id). Sent *before* the security gate, so the card can
# appear well before it's known whether the call will be judged, prompt for
# permission, or run immediately (see doc/SECURITY.md). ``run_command`` and
# ``web_search`` additionally carry ``timeout_seconds``, driving each one's
# "elapsed vs. timeout" progress bar.
EVT_AGENT_TOOL_CALL_PREP = "agent.tool_call_prep"

# Emitted once the security gate has cleared (allowed outright, or the user
# granted permission) and the tool is about to actually run — the moment a
# run_command/web_search timeout genuinely starts. Sent between
# EVT_AGENT_TOOL_CALL_PREP and EVT_AGENT_TOOL_CALL_DETAIL so the client can
# defer the "waiting for tool output" timeout animation past any judging
# round / permission wait (see doc/SECURITY.md §6).
EVT_AGENT_TOOL_CALL_IN_PROGRESS = "agent.tool_call_in_progress"

# Post-dispatch follow-up to EVT_AGENT_TOOL_CALL_PREP: carries the customer-visible
# input/output projection, the persisted Markdown doc path, and the
# schema-compliance flag, correlated by tool_call_id (= the tool_use block id).
EVT_AGENT_TOOL_CALL_DETAIL = "agent.tool_call_detail"

# Emitted when a tool's raw output did not match its declared output schema
# (the engine repaired it). Drives a VSIX error message box.
EVT_TOOL_INCOMPLIANT = "tool.incompliant"

# Server → Client event. Live narration from the `web_search` agent's silent
# tool loop (`WorkflowEngine._run_web_search_agent`, doc/WEB_SEARCH.md §6):
# one event per round in which the agent produced free text, carrying
# `tool_call_id` (correlates with the `web_search` call's `agent.tool_call_prep`)
# and `text` (the agent's own account of what it just decided/did). Drives the
# "Web Search is in progress" collapsible block. Live-only on the wire — the
# durable copy is a best-effort sidecar file (`TransientStore.write_web_search_notes`)
# written once the run ends, replayed via `session.history`'s `tool_call.webSearchNotes`
# rather than by replaying this event; a crash mid-run loses whatever wasn't
# flushed yet, which is acceptable (see doc/WEB_SEARCH.md §6).
EVT_WEB_SEARCH_NOTE = "web_search.note"

# Server → Client event. Bracket the Guide pipeline's author→critic document
# review step (``run_author_critic_iteration``): ``review.started`` fires right
# before the critic sub-agent is spawned, ``review.verdict`` right after,
# carrying the ``document_feedback``-derived status (``accepted`` /
# ``needs_revision`` / ``pending_acceptance``) and ``concern_count`` — never
# file content, diffs, or feedback text (low-fidelity by design). NOTE: emitted
# by the server today, but the current VSIX client has no handler for either —
# the events are silently dropped on arrival (no UI surfaces them yet).
EVT_REVIEW_STARTED = "review.started"
EVT_REVIEW_VERDICT = "review.verdict"

# Context-compaction events (in-place compaction of an entry agent's main
# context; see runtime/_engine/_compaction.py + doc/STATE_AND_LIFECYCLE.md §4.5).
# Server → Client events.
# - context.stats   {current_tokens, limit_tokens, percent, can_compact}: pushed
#   on every state change and after each measured turn so the WebView header can
#   show the live context gauge and enable/disable its "Compact now" button.
# - context.compacting {active}: brackets a compaction run so the WebView shows a
#   "Compacting context, please hold on" indicator with running dots.
# - context.compacted {summary_excerpt, tokens_before, tokens_after}: emitted once
#   a compaction completes, to drop a "Context compacted" divider into the feed.
EVT_CONTEXT_STATS = "context.stats"
EVT_CONTEXT_COMPACTING = "context.compacting"
EVT_CONTEXT_COMPACTED = "context.compacted"

# Server → Client event. Broadcasts one root's full CheckpointState
# (``{root, current_index, entries: [{sha, undone}]}``) so every tool-call card
# sharing that root can recompute its undo/redo + rollback/roll-forward
# eligibility in one pass. Pushed after any successful checkpoint.undo/redo/
# rollback/roll_forward (a single action can change every other entry's
# eligible action — see MSG_CHECKPOINT_ROLLBACK above), and *also* after a
# fresh mutating tool call's own commit (it advances current_index past every
# earlier entry on that root, so their "Rollback to this state" links would
# otherwise go stale).
EVT_CHECKPOINT_STATE = "checkpoint.state"

# Server → Client event. Fired just before every LLM request inside a turn's
# tool-use loop (i.e. once per round-trip, not once per turn) so the client can
# clear any leftover toolgen indicator and show an "awaiting" state before the
# first token arrives.
EVT_LLM_TURN_START = "llm.turn_start"

# Emitted by the LLM gateway while a session's LLM request is queued behind the
# serial local gate / a saturated cloud feed (``reason:"queued"``) or is being
# held back by 429 throttling (``reason:"throttled"`` with ``retry_in_seconds``).
# ``{waiting:false}`` clears the indicator. Owned entirely by the gateway.
EVT_LLM_WAITING = "llm.waiting"

# Server → Client event. Pushed after every LLM call: ``{cumulative_usd,
# duration_seconds, last_call_tokens, model, breakdown}``. ``last_call_tokens:
# null`` (via ``EngineEmitters.emit_cost_only``) folds a *silent* call's cost —
# e.g. session titling — into the running total without appending a status row
# to the feed.
EVT_USAGE_UPDATE = "usage.update"

# Server → Client event. Unsolicited runtime error not tied to a specific
# request (request-scoped failures reply with ``payload.error`` on the response
# instead). ``recoverable: false`` means the engine has halted for this
# session; the client surfaces only non-recoverable errors as a modal
# notification. A cloud 401 sends this (recoverable=false) right after
# EVT_API_KEY_REVOKE for the rejected vendor.
EVT_ERROR = "error"

# Server → Client events. Drive the sidebar's llama.cpp/model controls only —
# no workflow meaning. ``llamacpp.install.progress`` streams ``{percent,
# message}`` for the llama.cpp binary install; ``percent == -1`` signals
# failure (``message`` carries why). There is no ``local_llm.*`` download
# progress event any more — kodo-vsix polls ``manager-state.json`` off disk
# instead (see the comment above ``MSG_LOCAL_LLM_INSTALL``).  ``llama.state``
# reports ``{running, model, port?}`` or ``{running: false, error}`` whenever
# the local server starts, stops, or fails — including an auto-start
# triggered mid-prompt by a local-model dispatch, which is why it can arrive
# on a *session* connection instead of the control connection (see
# ``onLlamaState`` in kodo-vsix), not only after explicit
# ``llama.start``/``llama.stop``.
EVT_LLAMACPP_INSTALL_PROGRESS = "llamacpp.install.progress"
EVT_LLAMA_STATE = "llama.state"

# Server → Client event. Sent once after every ``local_llm.*``/
# ``llama_server_override.*`` mutation (add/install/uninstall/remove/
# override), on the same connection that issued the request — mirrors
# ``llama.state``'s single-connection-reply shape rather than a broadcast.
# ``install``/``resume`` send it *twice*: an immediate kickoff reply, then one
# more from ``_run_background_download`` once the transfer thread actually
# finishes (success or failure) — see doc/LOCAL_MODEL_MANAGER.md §11. The
# kickoff reply is sent before the background task is even created so the two
# can't race each other onto the wire out of order.
# Payload: ``{local_registry: [...], llama_server_override_path}`` — the full
# merged registry (hardcoded + custom, each with ``installed``) so the webview
# can just replace its whole card list rather than patching it.
EVT_LOCAL_LLM_REGISTRY_STATE = "local_llm.registry_state"

# Server → Client event. Pushed when the engine itself disables Autonomous mode
# (the Guide's ``disable_autonomous_mode`` tool) — as opposed to a user toggle
# via ``mode.set``. Clears both the selected and per-turn-frozen effective
# values immediately (an in-session engine decision takes effect right away,
# unlike a user flip which waits for the next prompt) and accompanies a fresh
# EVT_STATE carrying the same. Client also shows an informational toast and
# unlocks Edit/Command Control back to the user's selection.
EVT_AUTONOMOUS_CHANGED = "autonomous.changed"

# Server → Client events. The session's display name, generated once from the
# first prompt by the local CPU summarizer in kodo.titling (fired in the
# background by SessionTitler, never blocking the main turn) and persisted to
# ``meta.json``. ``session.naming`` brackets that background call
# (``active: true/false``) so the client can show a "Naming session …"
# indicator instead of an unexplained pause; ``session.name`` carries the
# result, and is replayed on every ``hello.ack`` with the current name
# (default ``"Unnamed Session"``) then pushed again once titling completes.
# Client renames the editor tab and the session header.
EVT_SESSION_NAME = "session.name"
EVT_SESSION_NAMING = "session.naming"

# Brackets the security layer's silent LLM intent-judge round (SECURITY.md §3.2),
# which streams nothing and can take several seconds to tens of seconds. Lets
# the client show an "Evaluating…" indicator instead of an unexplained stall.
EVT_SECURITY_JUDGING = "security.judging"

# Subsession (sub-agent takeover) boundaries — drive the WebView feed dividers
# ("Narrative Author subagent took over from Kōdo" / "Kōdo resumed").
EVT_SUBSESSION_STARTED = "subsession.started"
EVT_SUBSESSION_ENDED = "subsession.ended"

# The session's current project was bound (Guided). Payload: ``{root, name}``.
# Lets the client display the locked project and stop re-prompting for it.
EVT_PROJECT_BOUND = "project.bound"

# An agent created a brand-new project (``create_new_project`` tool) and the
# server has scaffolded it on disk; ask the VS Code extension to add the new
# directory to the open workspace (``vscode.workspace.updateWorkspaceFolders``).
# Payload: ``{path, name}`` (``path`` absolute). The extension's resulting
# ``onDidChangeWorkspaceFolders`` re-pushes ``workspace.folders``, reconciling
# the server's folder map.
EVT_WORKSPACE_ADD_FOLDER = "workspace.add_folder"

# A submitted prompt carried file attachments that the server has now stored in
# the session. Emitted right after the user message is persisted so the live
# WebView can render clickable chips on the just-sent bubble pointing at the
# stored copies. Payload: ``{attachments: [{name, path}]}`` (``path`` absolute).
EVT_USER_ATTACHMENTS = "user.attachments"
