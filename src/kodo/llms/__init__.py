"""LLM plugin interfaces and Anthropic implementation."""

from ._cloud_registry import (
    CloudLLMEntry,
    get_cloud_entry,
    get_cloud_registry,
    get_cloud_vendor_display_name,
    get_cloud_vendor_module,
)
from ._context import get_context_window
from ._gateway import EventSink, LLMGateway, LLMRouting
from ._interface import (
    LLMPlugin,
    Message,
    RateLimited,
    StreamEvent,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
)
from ._local_registry import (
    LocalLLMEntry,
    add_local_entry,
    clear_llama_server_override_path,
    get_llama_server_override_path,
    get_local_registry,
    remove_local_entry,
    set_llama_server_override_path,
)
from ._logger import LoggingLLMPlugin
from ._sanitize import strip_kodo_callouts
from ._tool_logger import ToolCallLogger

__all__ = [
    "CloudLLMEntry",
    "EventSink",
    "LLMGateway",
    "LLMPlugin",
    "LLMRouting",
    "LocalLLMEntry",
    "LoggingLLMPlugin",
    "Message",
    "RateLimited",
    "ThinkingDelta",
    "ThinkingSignature",
    "ToolCallLogger",
    "ToolSpec",
    "Usage",
    "StreamEvent",
    "TokenDelta",
    "ToolCallArgDelta",
    "ToolCallEvent",
    "TurnEnd",
    "add_local_entry",
    "clear_llama_server_override_path",
    "get_cloud_entry",
    "get_cloud_registry",
    "get_cloud_vendor_display_name",
    "get_cloud_vendor_module",
    "get_context_window",
    "get_llama_server_override_path",
    "get_local_registry",
    "remove_local_entry",
    "set_llama_server_override_path",
    "strip_kodo_callouts",
]
