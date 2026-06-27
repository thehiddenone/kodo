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
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Client → Server request payload types  (WS_PROTOCOL.md §7)
# ---------------------------------------------------------------------------

MSG_HELLO = "hello"
MSG_PROMPT_SUBMIT = "prompt.submit"
MSG_STOP = "stop"
# List all persisted sessions for the picker. Server replies with
# ``{sessions: [{id, name, project_root, taken}]}``. VSIX derives openability
# (project loaded? taken?) from those fields.
MSG_SESSION_LIST = "session.list"
# Release the session named by ``payload.session_id`` from this window's
# ownership immediately (graceful window close), so another window can open it.
MSG_SESSION_RELEASE = "session.release"
# Permanently delete the session named by ``payload.session_id``: stop its
# engine and physically remove its directory under ``sessions/`` (the project it
# worked on is untouched). On success the server closes the socket (the client
# reads the closure as confirmation); on failure it replies
# ``{type: "session.delete.error", message}`` and leaves the socket open.
MSG_SESSION_DELETE = "session.delete"
# Fetch the persisted CheckpointState ({current_index, entries: [...]}) for
# ``payload.root`` — used to hydrate the UI's per-entry undo/redo and
# rollback/roll-forward eligibility on session resume.
MSG_CHECKPOINT_LIST = "checkpoint.list"
# Stateful checkpoint actions on one mirror commit, triggered from a tool-call
# card. All four carry ``{root, sha}`` plus an optional ``resolution``
# (``"stash"|"discard"``, supplied only on retry after a ``*.needs_confirmation``
# reply caused by a dirty work tree — see below). ``undo``/``redo`` are a
# per-entry toggle: surgically revert/reapply only the files that commit
# changed, each as a new forward commit, flipping that entry's persisted
# ``undone`` flag. ``rollback``/``roll_forward`` are the same underlying
# operation in both directions: move the mirror's branch ref directly to
# ``sha``, preserving whatever tip it orphans under a ``rollback_<ts>`` branch
# (never a detached HEAD) — see ``kodo.mirror.ShadowMirror.rollback``. The
# server replies either ``checkpoint.<verb>.done`` with the updated
# CheckpointState, or ``checkpoint.<verb>.needs_confirmation`` (no
# ``resolution`` given and the tree is dirty — i.e. has edits Kodo didn't
# make); the client resubmits with a chosen ``resolution`` to proceed. The
# server also pushes ``EVT_CHECKPOINT_STATE`` after any successful mutation so
# every checkpoint button in the transcript can refresh, not just the one
# acted on.
MSG_CHECKPOINT_ROLLBACK = "checkpoint.rollback"
MSG_CHECKPOINT_ROLL_FORWARD = "checkpoint.roll_forward"
MSG_CHECKPOINT_UNDO = "checkpoint.undo"
MSG_CHECKPOINT_REDO = "checkpoint.redo"
MSG_MODE_SET = "mode.set"
MSG_WORKFLOW_SET = "workflow.set"
# Set the Edit Control posture.
# Payload: ``{edit_control: "review_all"|"allow_all"|"smart"}``. Unlike
# mode.set/workflow.set this is NEVER frozen: the client owns the value (forcing
# "allow_all" while Autonomous is in effect) and the server mirrors whatever it
# last sent, so the stored value is always exactly what the UI shows. (State
# tracking only — enforcement is deferred to the M4 security layer.)
MSG_EDIT_CONTROL_SET = "edit_control.set"
# Set the Command Control posture.
# Payload: ``{command_control: "defensive"|"permissive"|"smart"}``. Mirrored
# exactly like edit_control.set (client forces "permissive" under Autonomous).
MSG_COMMAND_CONTROL_SET = "command_control.set"
# Push the VS Code workspace folder map (logical name → physical path) plus the
# physical root. Sent on connect and on every workspace-folders change; the
# server rebuilds its WorkspaceLayout logical-root map. Payload:
# ``{physical_root, folders: {name: path}}``.
MSG_WORKSPACE_FOLDERS = "workspace.folders"
# Bind the session's current project (Guided mode). Sent once, lazily, when the
# user first runs Guided after picking a project. Payload: ``{root, name}``.
# Immutable for the session — a second, different value is rejected.
MSG_PROJECT_SET = "project.set"
MSG_SECURITY_ADD_RULE = "security.add_rule"
# Manually trigger context compaction for this session. Honoured only when the
# entry agent is idle (``state.phase == "awaiting_user"``) and there is context
# to compact; otherwise ignored. Drives the same path as the automatic
# 90%-threshold trigger — the engine runs the ``compactor`` sub-agent, writes a
# ``compaction`` marker to ``session.jsonl``, and resets the live LLM context.
MSG_COMPACT_NOW = "compact.now"
MSG_CONFIG_RELOAD = "config.reload"
MSG_LLAMACPP_INSTALL = "llamacpp.install"
MSG_MODEL_INSTALL = "model.install"
MSG_LLAMA_START = "llama.start"
MSG_LLAMA_STOP = "llama.stop"

# ---------------------------------------------------------------------------
# Server → Client request payload types — API key management  (WS_PROTOCOL.md §6)
#
# Server sends SREQ_API_KEY_REQUEST (kind=request) to ask VSIX for a key.
# VSIX replies with a kind=response correlated by the same id.
# Server sends EVT_API_KEY_REVOKE (kind=event) to tell VSIX to clear a key.
# ---------------------------------------------------------------------------

SREQ_API_KEY_REQUEST = "api_key.request"
EVT_API_KEY_REVOKE = "api_key.revoke"

# ---------------------------------------------------------------------------
# Server → Client request payload types — user prompts  (WS_PROTOCOL.md §6)
#
# These are ``kind=request`` frames the server initiates. The client's reply
# is a ``kind=response`` whose ``correlation_id`` equals the request's ``id``.
# ---------------------------------------------------------------------------

SREQ_PROMPT_QUESTION = "prompt.question"
SREQ_PROMPT_APPROVAL = "prompt.approval"
SREQ_PROMPT_PERMISSION = "prompt.permission"

# ---------------------------------------------------------------------------
# Server → Client event payload types — visibility  (WS_PROTOCOL.md §5)
# ---------------------------------------------------------------------------

EVT_STATE = "state"
EVT_AGENT_STARTED = "agent.started"
EVT_AGENT_FINISHED = "agent.finished"
EVT_AGENT_TOKENS = "agent.tokens"  # carried inside stream_chunk / stream_end
EVT_AGENT_TOOL_CALL = "agent.tool_call"
# Post-dispatch follow-up to EVT_AGENT_TOOL_CALL: carries the customer-visible
# input/output projection, the persisted Markdown doc path, and the
# schema-compliance flag, correlated by tool_call_id (= the tool_use block id).
EVT_AGENT_TOOL_CALL_DETAIL = "agent.tool_call_detail"
# Emitted when a tool's raw output did not match its declared output schema
# (the engine repaired it). Drives a VSIX error message box.
EVT_TOOL_INCOMPLIANT = "tool.incompliant"
EVT_REVIEW_STARTED = "review.started"
EVT_REVIEW_VERDICT = "review.verdict"
EVT_ARTIFACT_PUBLISHED = "artifact.published"
EVT_ARTIFACT_REMOVED = "artifact.removed"
EVT_GUIDE_COMPACTED = "guide.compacted"
# Context-compaction events (in-place compaction of an entry agent's main
# context; see runtime/_engine.py + doc/STATE_AND_LIFECYCLE.md §4.5).
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
# Pushed after any successful checkpoint.undo/redo/rollback/roll_forward:
# ``{root, current_index, entries: [{sha, undone}]}``. Lets the WebView
# recompute every tool-call card's undo/redo + rollback/roll-forward labels
# for that root in one pass, since a single action can change every other
# entry's eligible action (see MSG_CHECKPOINT_ROLLBACK above).
EVT_CHECKPOINT_STATE = "checkpoint.state"
EVT_LLM_TURN_START = "llm.turn_start"
# Emitted by the LLM gateway while a session's LLM request is queued behind the
# serial local gate / a saturated cloud feed (``reason:"queued"``) or is being
# held back by 429 throttling (``reason:"throttled"`` with ``retry_in_seconds``).
# ``{waiting:false}`` clears the indicator. Owned entirely by the gateway.
EVT_LLM_WAITING = "llm.waiting"
EVT_USAGE_UPDATE = "usage.update"
EVT_ERROR = "error"
EVT_LLAMACPP_INSTALL_PROGRESS = "llamacpp.install.progress"
EVT_MODEL_INSTALL_PROGRESS = "model.install.progress"
EVT_LLAMA_STATE = "llama.state"
EVT_AUTONOMOUS_CHANGED = "autonomous.changed"
EVT_POST_UPDATE = "post.update"
EVT_SESSION_NAME = "session.name"
EVT_SESSION_NAMING = "session.naming"
# Subsession (sub-agent takeover) boundaries — drive the WebView feed dividers
# ("Narrative Author subagent took over from Kōdo" / "Kōdo resumed").
EVT_SUBSESSION_STARTED = "subsession.started"
EVT_SUBSESSION_ENDED = "subsession.ended"
# The session's current project was bound (Guided). Payload: ``{root, name}``.
# Lets the client display the locked project and stop re-prompting for it.
EVT_PROJECT_BOUND = "project.bound"
# A submitted prompt carried file attachments that the server has now stored in
# the session. Emitted right after the user message is persisted so the live
# WebView can render clickable chips on the just-sent bubble pointing at the
# stored copies. Payload: ``{attachments: [{name, path}]}`` (``path`` absolute).
EVT_USER_ATTACHMENTS = "user.attachments"

# ---------------------------------------------------------------------------
# DEPRECATED — legacy constants retained until handler/event wiring is migrated
# to the WS_PROTOCOL.md catalogue. Do not use in new code.
#
# - MSG_PING / "ping"            — superseded by WebSocket-level ping/pong.
# - MSG_SESSION_RESUME           — resume is automatic on bootstrap
#                                  (STATE_AND_LIFECYCLE.md §4.3); no client-
#                                  initiated resume in the new protocol.
# - MSG_APPROVAL_RESPOND         — superseded by the response side of
#                                  SREQ_PROMPT_APPROVAL.
# - EVT_FILE_CHANGE              — superseded by EVT_ARTIFACT_PUBLISHED /
#                                  EVT_ARTIFACT_REMOVED.
# - EVT_SHELL_RUN                — superseded by EVT_AGENT_TOOL_CALL.
# - EVT_APPROVAL_REQUEST         — superseded by SREQ_PROMPT_APPROVAL
#                                  (kind=request, not kind=event).
# - EVT_SECURITY_PROMPT          — superseded by SREQ_PROMPT_PERMISSION.
# - EVT_RESUME_OFFER             — see MSG_SESSION_RESUME.
# ---------------------------------------------------------------------------

MSG_PING = "ping"
MSG_SESSION_RESUME = "session.resume"
MSG_APPROVAL_RESPOND = "approval.respond"

EVT_FILE_CHANGE = "file.change"
EVT_SHELL_RUN = "shell.run"
EVT_APPROVAL_REQUEST = "approval.request"
EVT_SECURITY_PROMPT = "security.prompt"
EVT_RESUME_OFFER = "resume_offer"
