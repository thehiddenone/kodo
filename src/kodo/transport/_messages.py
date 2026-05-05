"""Typed message-type constants for the Kōdo wire protocol.

Client-to-server message type strings (``MSG_*``) and server-to-client event
type strings (``EVT_*``) are stable identifiers used in ``payload["type"]``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Client → Server message types (carried in request payloads)
# ---------------------------------------------------------------------------

MSG_HELLO = "hello"
MSG_PING = "ping"
MSG_PROMPT_SUBMIT = "prompt.submit"
MSG_APPROVAL_RESPOND = "approval.respond"
MSG_STOP = "stop"
MSG_SESSION_RESUME = "session.resume"
MSG_CHECKPOINT_LIST = "checkpoint.list"
MSG_CHECKPOINT_ROLLBACK = "checkpoint.rollback"
MSG_SECURITY_ADD_RULE = "security.add_rule"
MSG_MODE_SET = "mode.set"

# ---------------------------------------------------------------------------
# Server → Client event types (carried in event payloads)
# ---------------------------------------------------------------------------

EVT_AGENT_STARTED = "agent.started"
EVT_AGENT_FINISHED = "agent.finished"
EVT_FILE_CHANGE = "file.change"
EVT_SHELL_RUN = "shell.run"
EVT_APPROVAL_REQUEST = "approval.request"
EVT_SECURITY_PROMPT = "security.prompt"
EVT_USAGE_UPDATE = "usage.update"
EVT_ERROR = "error"
EVT_STATE = "state"
EVT_RESUME_OFFER = "resume_offer"
