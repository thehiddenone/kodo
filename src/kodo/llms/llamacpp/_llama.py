"""LlamaPlugin: llama.cpp (llama-server) implementation of LLMPlugin.

Uses the OpenAI-compatible REST API exposed by llama-server.
Local inference has no prompt cache and zero dollar cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import openai

from kodo.llms._interface import (
    LLMPlugin,
    Message,
    StreamEvent,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
)

__all__ = ["LlamaPlugin"]

_log = logging.getLogger(__name__)
_DEFAULT_MAX_TOKENS = 8192
_API_KEY = "key_is_not_required_for_local_inference"


# ---------------------------------------------------------------------------
# Message-format conversion: kodo Message → OpenAI chat messages
# ---------------------------------------------------------------------------


def _flatten_content(content: object) -> str:
    """Reduce nested Anthropic-style content blocks to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return " ".join(parts)
    return str(content)


def _expand_assistant(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
    msg: dict[str, object] = {
        "role": "assistant",
        "content": " ".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return [msg]


def _expand_user(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    text_parts: list[str] = []
    for block in blocks:
        if block.get("type") == "tool_result":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _flatten_content(block.get("content", "")),
                }
            )
        elif block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
    if text_parts:
        result.append({"role": "user", "content": " ".join(text_parts)})
    return result


def _expand_message(msg: Message) -> list[dict[str, object]]:
    if isinstance(msg.content, str):
        return [{"role": msg.role, "content": msg.content}]
    blocks = msg.content
    if msg.role == "assistant":
        return _expand_assistant(blocks)
    if msg.role == "user":
        return _expand_user(blocks)
    text = " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
    return [{"role": msg.role, "content": text}]


def _build_oai_messages(system: str, messages: list[Message]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = [{"role": "system", "content": system}]
    for msg in messages:
        result.extend(_expand_message(msg))
    return result


def _map_finish_reason(reason: str | None) -> str:
    if reason == "stop":
        return "end_turn"
    if reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    return reason or "end_turn"


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class LlamaPlugin(LLMPlugin):
    """llama.cpp (llama-server) implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Connects to a running llama-server via its OpenAI-compatible REST API.
    Prompt caching is not available; token cost is always zero.
    """

    __client: openai.AsyncOpenAI
    __model_names: list[str]
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, base_url: str, model_names: list[str] | None = None) -> None:
        """Initialise the plugin.

        Args:
            base_url (str): Full base URL of the llama-server ``/v1`` endpoint
                (e.g. ``'http://127.0.0.1:8080/v1'``).
            model_names (list[str] | None): Names reported by :attr:`supported_models`.
                Defaults to ``['local']``.
        """
        self.__client = openai.AsyncOpenAI(api_key=_API_KEY, base_url=base_url)
        self.__model_names = list(model_names) if model_names else ["local"]
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "llamacpp"

    @property
    def supported_models(self) -> list[str]:
        return list(self.__model_names)

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
        """Stream a llama-server response.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Model identifier forwarded to llama-server (the server ignores
                it but the value is preserved in the returned :class:`Usage`).
            system (str): System prompt text.
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Ignored — llama-server has no equivalent.

        Yields:
            StreamEvent: Token deltas, tool calls, then :class:`TurnEnd`.
        """
        return self.__stream(
            stream_id=stream_id,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
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

    async def __stream(
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]:
        cancel_event = asyncio.Event()
        self.__cancel_events[stream_id] = cancel_event
        try:
            async for event in self.__raw_stream(
                cancel_event=cancel_event,
                model=model,
                system=system,
                messages=messages,
                tools=tools,
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
    ) -> AsyncIterator[StreamEvent]:
        oai_messages = _build_oai_messages(system, messages)
        oai_tools: list[dict[str, object]] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0

        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in response:
            if cancel_event.is_set():
                _log.debug("Stream cancelled by caller")
                return

            if chunk.usage is not None:
                input_tokens = chunk.usage.prompt_tokens
                output_tokens = chunk.usage.completion_tokens

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = choice.delta
            if delta.content:
                yield TokenDelta(text=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if tc.id:
                        tool_ids[idx] = tc.id
                    if tc.function and tc.function.name:
                        tool_names[idx] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_arg_parts.setdefault(idx, []).append(tc.function.arguments)

        if cancel_event.is_set():
            return

        for idx in sorted(tool_ids):
            raw_json = "".join(tool_arg_parts.get(idx, []))
            try:
                tool_input: dict[str, object] = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                tool_input = {"_raw": raw_json}
            yield ToolCallEvent(
                tool_use_id=tool_ids[idx],
                tool_name=tool_names.get(idx, ""),
                tool_input=tool_input,
            )

        yield TurnEnd(
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_write_tokens=0,
                cache_read_tokens=0,
                model=model,
            ),
            stop_reason=_map_finish_reason(finish_reason),
        )
