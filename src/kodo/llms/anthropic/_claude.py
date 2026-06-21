"""Claude LLM plugin — streaming, prompt caching, retries, and usage tracking."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import anthropic
from anthropic.types import (
    InputJSONDelta,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    SignatureDelta,
    TextDelta,
    ToolUseBlock,
)
from anthropic.types import ThinkingDelta as RawThinkingDelta  # SDK's own, distinct from ours

from kodo.llms._interface import (
    LLMPlugin,
    Message,
    StreamEvent,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
)

from ._cache import build_message_params, build_system_blocks
from ._retry import UnrecoverableError, with_retry_iter

__all__ = ["ClaudePlugin", "UnrecoverableError"]

_log = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8192

# Extended thinking: budget_tokens must be >=1024 and < max_tokens, leaving
# headroom in _DEFAULT_MAX_TOKENS for the visible response.
_THINKING_BUDGET_TOKENS = 4096


class ClaudePlugin(LLMPlugin):
    """Anthropic Claude implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``anthropic`` Python SDK with prompt caching,
    exponential-backoff retries (FR-LLM-05), and cancellation support
    (FR-LLM-07).
    """

    __client: anthropic.AsyncAnthropic
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with an Anthropic API key.

        Args:
            api_key (str): Anthropic API key (not written to disk per NFR-06).
        """
        self.__client = anthropic.AsyncAnthropic(api_key=api_key)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supported_models(self) -> list[str]:
        return [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]

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
        """Stream a Claude response with prompt caching and retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Claude model identifier.
            system (str): System prompt text.
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Message indices to cache.

        Yields:
            StreamEvent: Token deltas, tool calls, then :class:`TurnEnd`.
        """
        return self.__stream_with_retry(
            stream_id=stream_id,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            cache_breakpoints=cache_breakpoints,
        )

    async def cancel(self, stream_id: str) -> None:
        """Signal an in-flight stream to stop within 1 second.

        Args:
            stream_id (str): ID from the matching :meth:`stream_query` call.
        """
        event = self.__cancel_events.get(stream_id)
        if event is not None:
            event.set()
            _log.debug("Cancel signal sent for stream %s", stream_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def __stream_with_retry(
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
    ) -> AsyncIterator[StreamEvent]:
        cancel_event = asyncio.Event()
        self.__cancel_events[stream_id] = cancel_event
        try:
            async for event in with_retry_iter(
                lambda: self.__raw_stream(
                    cancel_event=cancel_event,
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    cache_breakpoints=cache_breakpoints,
                )
            ):
                yield event
        finally:
            self.__cancel_events.pop(stream_id, None)

    async def __raw_stream(
        self,
        *,
        cancel_event: asyncio.Event,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
    ) -> AsyncIterator[StreamEvent]:
        system_blocks = build_system_blocks(system)
        msg_params = build_message_params(messages, cache_breakpoints)

        tool_defs: list[dict[str, object]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        current_tool_use_id: str | None = None
        current_tool_name: str | None = None
        current_tool_input_parts: list[str] = []

        async with self.__client.messages.stream(
            model=model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            system=system_blocks,  # type: ignore[arg-type]
            messages=msg_params,  # type: ignore[arg-type]
            thinking={"type": "enabled", "budget_tokens": _THINKING_BUDGET_TOKENS},
            **({"tools": tool_defs} if tool_defs else {}),  # type: ignore[arg-type]
        ) as stream:
            async for raw_event in stream:
                if cancel_event.is_set():
                    _log.debug("Stream cancelled by caller")
                    return

                if isinstance(raw_event, RawContentBlockStartEvent):
                    block = raw_event.content_block
                    if isinstance(block, ToolUseBlock):
                        current_tool_use_id = block.id
                        current_tool_name = block.name
                        current_tool_input_parts = []
                    # RedactedThinkingBlock (safety-flagged reasoning, no plain
                    # text) is intentionally not surfaced or persisted — it is
                    # rare and has no human-readable content to show or replay.

                elif isinstance(raw_event, RawContentBlockDeltaEvent):
                    delta = raw_event.delta
                    if isinstance(delta, TextDelta):
                        yield TokenDelta(text=delta.text)
                    elif isinstance(delta, InputJSONDelta):
                        current_tool_input_parts.append(delta.partial_json)
                    elif isinstance(delta, RawThinkingDelta):
                        yield ThinkingDelta(text=delta.thinking)
                    elif isinstance(delta, SignatureDelta):
                        yield ThinkingSignature(signature=delta.signature)

                elif raw_event.type == "content_block_stop":
                    if current_tool_use_id is not None and current_tool_name is not None:
                        raw_json = "".join(current_tool_input_parts)
                        try:
                            tool_input: dict[str, object] = json.loads(raw_json) if raw_json else {}
                        except json.JSONDecodeError:
                            tool_input = {"_raw": raw_json}
                        yield ToolCallEvent(
                            tool_use_id=current_tool_use_id,
                            tool_name=current_tool_name,
                            tool_input=tool_input,
                        )
                        current_tool_use_id = None
                        current_tool_name = None
                        current_tool_input_parts = []

            if not cancel_event.is_set():
                final = await stream.get_final_message()
                raw_usage = final.usage
                usage = Usage(
                    input_tokens=raw_usage.input_tokens,
                    output_tokens=raw_usage.output_tokens,
                    cache_write_tokens=(getattr(raw_usage, "cache_creation_input_tokens", 0) or 0),
                    cache_read_tokens=(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
                    model=model,
                )
                yield TurnEnd(usage=usage, stop_reason=str(final.stop_reason or "end_turn"))
