"""Wire-protocol types for the Kōdo extension↔server WebSocket connection."""

from kodo.common import Envelope, MessageKind

from ._messages import (
    # Server → Client events (WS_PROTOCOL.md §5)
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_AGENT_TOKENS,
    EVT_AGENT_TOOL_CALL,
    # Server → Client events — API key management
    EVT_API_KEY_REVOKE,
    # Deprecated / legacy — retained until handler wiring is migrated
    EVT_APPROVAL_REQUEST,
    EVT_ARTIFACT_PUBLISHED,
    EVT_ARTIFACT_REMOVED,
    EVT_ERROR,
    EVT_FILE_CHANGE,
    EVT_LLAMA_STATE,
    EVT_LLAMACPP_INSTALL_PROGRESS,
    EVT_MODEL_INSTALL_PROGRESS,
    EVT_ORCHESTRATOR_COMPACTED,
    EVT_RESUME_OFFER,
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_SECURITY_PROMPT,
    EVT_SHELL_RUN,
    EVT_STATE,
    EVT_USAGE_UPDATE,
    MSG_APPROVAL_RESPOND,
    # Client → Server requests (WS_PROTOCOL.md §7)
    MSG_CHECKPOINT_LIST,
    MSG_CHECKPOINT_ROLLBACK,
    MSG_CONFIG_RELOAD,
    MSG_HELLO,
    MSG_LLAMA_START,
    MSG_LLAMA_STOP,
    MSG_LLAMACPP_INSTALL,
    MSG_MODE_SET,
    MSG_MODEL_INSTALL,
    MSG_PING,
    MSG_PROMPT_SUBMIT,
    MSG_SECURITY_ADD_RULE,
    MSG_SESSION_RESUME,
    MSG_STOP,
    # Server → Client requests — user prompts (WS_PROTOCOL.md §6)
    SREQ_API_KEY_REQUEST,
    SREQ_PROMPT_APPROVAL,
    SREQ_PROMPT_PERMISSION,
    SREQ_PROMPT_QUESTION,
)
from ._outbox import Outbox
from ._ws import WebSocketDispatcher

__all__ = [
    "WebSocketDispatcher",
    "Envelope",
    "MessageKind",
    "Outbox",
    # WS_PROTOCOL.md §7 — client requests
    "MSG_HELLO",
    "MSG_PROMPT_SUBMIT",
    "MSG_STOP",
    "MSG_CHECKPOINT_LIST",
    "MSG_CHECKPOINT_ROLLBACK",
    "MSG_CONFIG_RELOAD",
    "MSG_LLAMACPP_INSTALL",
    "MSG_LLAMA_START",
    "MSG_LLAMA_STOP",
    "MSG_MODEL_INSTALL",
    "MSG_MODE_SET",
    "MSG_SECURITY_ADD_RULE",
    # WS_PROTOCOL.md §6 — server-initiated requests
    "SREQ_API_KEY_REQUEST",
    "SREQ_PROMPT_QUESTION",
    "SREQ_PROMPT_APPROVAL",
    "SREQ_PROMPT_PERMISSION",
    # API key management events
    "EVT_API_KEY_REVOKE",
    # WS_PROTOCOL.md §5 — visibility events
    "EVT_STATE",
    "EVT_AGENT_STARTED",
    "EVT_AGENT_FINISHED",
    "EVT_AGENT_TOKENS",
    "EVT_AGENT_TOOL_CALL",
    "EVT_REVIEW_STARTED",
    "EVT_REVIEW_VERDICT",
    "EVT_ARTIFACT_PUBLISHED",
    "EVT_ARTIFACT_REMOVED",
    "EVT_ORCHESTRATOR_COMPACTED",
    "EVT_USAGE_UPDATE",
    "EVT_ERROR",
    "EVT_LLAMACPP_INSTALL_PROGRESS",
    "EVT_LLAMA_STATE",
    "EVT_MODEL_INSTALL_PROGRESS",
    # Deprecated / legacy
    "MSG_PING",
    "MSG_SESSION_RESUME",
    "MSG_APPROVAL_RESPOND",
    "EVT_FILE_CHANGE",
    "EVT_SHELL_RUN",
    "EVT_APPROVAL_REQUEST",
    "EVT_SECURITY_PROMPT",
    "EVT_RESUME_OFFER",
]
