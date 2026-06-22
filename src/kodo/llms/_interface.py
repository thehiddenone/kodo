"""LLMPlugin abstract base class and shared data types.

Defines the uniform interface all LLM provider plugins must implement,
plus the data types exchanged between the workflow engine and plugins.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from kodo.toolspecs import ToolSpec

__all__ = [
    "LLMPlugin",
    "Message",
    "RateLimited",
    "ToolSpec",
    "Usage",
    "StreamEvent",
    "ThinkingDelta",
    "ThinkingSignature",
    "TokenDelta",
    "ToolCallArgDelta",
    "ToolCallEvent",
    "TurnEnd",
]


class RateLimited(Exception):
    """Provider-agnostic HTTP 429 signal raised by an :class:`LLMPlugin`.

    The plugin surfaces a rate-limit rejection as this exception so the shared
    :class:`kodo.llms.LLMGateway` can own backoff/re-queue policy (the plugin
    itself stays a stateless one-shot facade and never queues).

    Attributes:
        retry_after: Server-advised seconds to wait before retrying, if the
            provider supplied a ``Retry-After`` header; ``None`` otherwise.
    """

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Message:
    """A single turn in a conversation.

    Attributes:
        role: ``'user'`` or ``'assistant'``.
        content: Plain text or a list of typed content blocks.
    """

    role: str
    content: str | list[dict[str, object]]


@dataclass(frozen=True)
class Usage:
    """Token usage and computed USD cost for one LLM call.

    Attributes:
        input_tokens: Uncached input tokens consumed.
        output_tokens: Generated output tokens.
        cache_write_tokens: Tokens written to the prompt cache this call.
        cache_read_tokens: Tokens read from the prompt cache this call.
        model: Model identifier used for pricing lookup.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    model: str

    @property
    def usd_cost(self) -> float:
        """Estimated USD cost based on published model pricing.

        Returns:
            float: Dollar cost of this LLM call.
        """
        from kodo.llms.anthropic._usage import compute_cost

        return compute_cost(self)


@dataclass(frozen=True)
class StreamEvent:
    """Base class for events emitted by :meth:`LLMPlugin.stream_query`."""


@dataclass(frozen=True)
class ThinkingDelta(StreamEvent):
    """A chain-of-thought text fragment emitted during extended thinking.

    Stripped from conversation history by default; displayed to the user
    separately.  Provider-agnostic — any plugin that supports extended
    thinking yields this event type.

    Attributes:
        text: The thinking text fragment.
    """

    text: str


@dataclass(frozen=True)
class ThinkingSignature(StreamEvent):
    """The cryptographic signature Anthropic attaches to a finished thinking block.

    Anthropic requires the exact signature to be replayed verbatim alongside
    the thinking text for a later request to be accepted; a thinking block
    without one is rejected by the API. Emitted once, after the run of
    :class:`ThinkingDelta` events for a thinking block completes. llama.cpp has
    no equivalent concept and never emits this event — its thinking blocks
    carry no signature.

    Attributes:
        signature: Opaque signature string to persist and replay unchanged.
    """

    signature: str


@dataclass(frozen=True)
class TokenDelta(StreamEvent):
    """A text token fragment emitted during streaming.

    Attributes:
        text: The token text fragment.
    """

    text: str


@dataclass(frozen=True)
class ToolCallArgDelta(StreamEvent):
    """An incremental fragment of a tool call's arguments as the model streams them.

    Display-only. A tool call's arguments can be very large (e.g. an
    ``edit_file`` whose ``content`` is an entire file), and the model spends
    most of a turn decoding them — during which no other event is produced, so
    the UI looks frozen for minutes. Emitting this fragment lets the client show
    a live "generating" indicator that proves the model is still working.

    The fully-parsed call still arrives later as :class:`ToolCallEvent`; this
    event is NOT accumulated into conversation history (the engine ignores it
    for that purpose).

    Attributes:
        tool_name: Name of the tool whose arguments are streaming. May be ``""``
            on the very first fragment if the model has not emitted the name yet.
        text: Raw argument-text fragment (partial JSON), as produced by the model.
    """

    tool_name: str
    text: str


@dataclass(frozen=True)
class ToolCallEvent(StreamEvent):
    """A fully-assembled tool invocation from the model.

    Attributes:
        tool_use_id: Unique ID for this tool call (for result correlation).
        tool_name: Name of the tool to invoke.
        tool_input: Parsed JSON input arguments.
    """

    tool_use_id: str
    tool_name: str
    tool_input: dict[str, object]


@dataclass(frozen=True)
class TurnEnd(StreamEvent):
    """Signals the end of a model turn with usage statistics.

    Attributes:
        usage: Token usage and cost for the completed turn.
        stop_reason: Why the model stopped (``'end_turn'``, ``'tool_use'``, …).
    """

    usage: Usage
    stop_reason: str


class LLMPlugin(ABC):
    """Abstract LLM provider plugin.

    Wraps a model provider and exposes a uniform streaming interface for
    the workflow engine.  MVP ships one concrete implementation:
    :class:`kodo.llms.anthropic.ClaudePlugin`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name, e.g. ``'anthropic'``."""

    @property
    @abstractmethod
    def supported_models(self) -> list[str]:
        """Model identifiers this plugin can serve."""

    @abstractmethod
    def stream_query(
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
    ) -> AsyncIterator[StreamEvent]:
        """Stream a model response.

        Args:
            stream_id: Caller-supplied identifier; pass the same value to
                :meth:`cancel` to abort this stream.
            model: Model identifier.
            system: System prompt text (plain string; caching is applied
                by the plugin).
            messages: Conversation history in chronological order.
            tools: Tool specifications the model may invoke.
            cache_breakpoints: Indices into ``messages`` whose content
                should be marked with ``cache_control`` for prompt caching.

        Yields:
            StreamEvent: :class:`TokenDelta`, :class:`ToolCallEvent`, and
            finally :class:`TurnEnd`.
        """

    @abstractmethod
    async def cancel(self, stream_id: str) -> None:
        """Cancel an in-flight stream within 1 second.

        Args:
            stream_id: ID from the matching :meth:`stream_query` call.
        """
