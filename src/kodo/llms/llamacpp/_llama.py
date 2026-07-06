"""LlamaPlugin: llama.cpp (llama-server) implementation of LLMPlugin.

Uses the OpenAI-compatible REST API exposed by llama-server.
Local inference has no prompt cache and zero dollar cost.

On first use, if llama-server is not running, the plugin starts it
automatically via :func:`._manager.ensure_llama_running` and emits
:data:`kodo.transport.EVT_LLAMA_STATE` events so the VSIX sidebar reflects
the updated state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import openai

from kodo.common import Envelope, MessageSink
from kodo.llms import (
    LLMPlugin,
    Message,
    StreamEvent,
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
    get_local_registry,
    strip_kodo_callouts,
)
from kodo.transport import EVT_LLAMA_STATE

from ._llama_server import LlamaServer
from ._manager import ensure_llama_running

__all__ = ["LlamaPlugin"]

_log = logging.getLogger(__name__)
_DEFAULT_MAX_TOKENS = 8192
_API_KEY = "key_is_not_required_for_local_inference"

# Force an uncompressed response body. llama-server talks to us over loopback,
# so compression buys nothing — but if httpx negotiates gzip/zstd, the SSE
# stream is delivered in whole compression blocks instead of per token, which
# shows up as the stream stalling for a beat and then dumping many tokens at
# once. Asking for ``identity`` keeps each chunk flushing as it is produced.
_NO_COMPRESSION_HEADERS = {"Accept-Encoding": "identity"}


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
        block_type = block.get("type")
        if block_type == "thinking":
            # Re-wrap in the model's own <think> convention so reasoning from
            # an earlier turn (this provider's or another's) is real context
            # again, not just a UI artifact. The signature (if any, e.g. from
            # a Claude-origin turn in a mixed local/cloud session) is provider
            # metadata llama.cpp has no use for and is dropped here.
            text_parts.append(f"<think>{block.get('thinking', '')}</think>")
        elif block_type == "text":
            # One-way notifications to the user; never replayed as context.
            text_parts.append(strip_kodo_callouts(str(block.get("text", ""))))
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
        content = strip_kodo_callouts(msg.content) if msg.role == "assistant" else msg.content
        return [{"role": msg.role, "content": content}]
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
# Thinking stream parser
# ---------------------------------------------------------------------------

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _partial_tag_suffix_len(buffer: str, tag: str) -> int:
    """Length of the buffer's tail that could be the start of ``tag``.

    Returns the largest ``k`` in ``1..len(tag)-1`` such that ``buffer`` ends
    with ``tag[:k]`` (i.e. an *incomplete* tag that the next chunk might
    finish), else ``0``. Only this tail needs to be withheld; the rest is safe
    to emit now. This is what lets a normal sentence ending (e.g. ``"both."``)
    flush immediately instead of being held until end-of-stream — the symptom
    where the last few characters of a sentence only appeared minutes later,
    alongside the tool call.
    """
    for k in range(min(len(tag) - 1, len(buffer)), 0, -1):
        if buffer.endswith(tag[:k]):
            return k
    return 0


class ThinkingStreamParser:
    """Splits a streamed text into ThinkingDelta and TokenDelta events.

    Qwen3 (and similar models) embed chain-of-thought reasoning inside
    ``<think>...</think>`` tags in the assistant text.  Because tag
    boundaries can fall in the middle of a streamed chunk, the parser
    buffers up to ``len(tag) - 1`` characters while deciding whether an
    upcoming ``<`` starts a tag or is plain text.
    """

    def __init__(self) -> None:
        self._in_thinking = False
        self._buffer = ""

    def feed(self, text: str) -> list[StreamEvent]:
        """Process an incoming chunk and return zero or more stream events."""
        self._buffer += text
        events: list[StreamEvent] = []
        while True:
            if self._in_thinking:
                idx = self._buffer.find(_THINK_CLOSE)
                if idx >= 0:
                    if idx > 0:
                        events.append(ThinkingDelta(text=self._buffer[:idx]))
                    self._buffer = self._buffer[idx + len(_THINK_CLOSE) :]
                    self._in_thinking = False
                else:
                    hold = _partial_tag_suffix_len(self._buffer, _THINK_CLOSE)
                    safe = len(self._buffer) - hold
                    if safe > 0:
                        events.append(ThinkingDelta(text=self._buffer[:safe]))
                        self._buffer = self._buffer[safe:]
                    break
            else:
                idx = self._buffer.find(_THINK_OPEN)
                if idx >= 0:
                    if idx > 0:
                        events.append(TokenDelta(text=self._buffer[:idx]))
                    self._buffer = self._buffer[idx + len(_THINK_OPEN) :]
                    self._in_thinking = True
                else:
                    hold = _partial_tag_suffix_len(self._buffer, _THINK_OPEN)
                    safe = len(self._buffer) - hold
                    if safe > 0:
                        events.append(TokenDelta(text=self._buffer[:safe]))
                        self._buffer = self._buffer[safe:]
                    break
        return events

    def flush(self) -> list[StreamEvent]:
        """Emit any buffered remainder at end of stream."""
        if not self._buffer:
            return []
        event: StreamEvent = (
            ThinkingDelta(text=self._buffer) if self._in_thinking else TokenDelta(text=self._buffer)
        )
        self._buffer = ""
        return [event]


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class LlamaPlugin(LLMPlugin):
    """llama.cpp (llama-server) implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Connects to a running llama-server via its OpenAI-compatible REST API.
    If llama-server is not running when :meth:`stream_query` is called, it is
    started automatically.  Prompt caching is not available; token cost is
    always zero.
    """

    __sink: MessageSink
    __kodo_dir: Path
    __client: openai.AsyncOpenAI | None
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, sink: MessageSink, kodo_dir: Path) -> None:
        """Initialise the plugin.

        Args:
            sink (MessageSink): Channel for emitting :data:`EVT_LLAMA_STATE`
                events to the VSIX client.
            kodo_dir (Path): User-level ``~/.kodo`` directory used to locate
                the llama.cpp install and downloaded models.
        """
        self.__sink = sink
        self.__kodo_dir = kodo_dir
        self.__client = None
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "llamacpp"

    @property
    def supported_models(self) -> list[str]:
        server = LlamaServer.get_active_llama_server()
        if server is not None and server.is_running and server.model_name:
            return [server.model_name]
        return ["local"]

    async def stream_query(
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
    ) -> AsyncIterator[StreamEvent]:
        """Stream a llama-server response, starting the server if needed.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Model registry name (e.g. ``'llamacpp-qwen36-27b'``).
            system (str): System prompt text.
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Ignored — llama-server has no equivalent.

        Yields:
            StreamEvent: Token deltas, tool calls, then :class:`TurnEnd`.

        Raises:
            RuntimeError: If llama-server cannot be started (not installed,
                model not downloaded, or startup timeout).
        """
        await self.__ensure_running(model)
        async for event in self.__stream(
            stream_id=stream_id,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
        ):
            yield event

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

    async def __ensure_running(self, model_name: str) -> None:
        registry = get_local_registry(self.__kodo_dir)
        entry = registry.get(model_name)

        if entry is not None and entry.kind == "custom_server_url":
            # Externally-managed server: stop kodo's own managed process (if
            # any) and never start one — it stays stopped until the user picks
            # a kodo-managed local model again (see doc/LLM_REGISTRY.md).
            managed = LlamaServer.get_active_llama_server()
            if managed is not None and managed.is_running:
                await managed.stop()
                await self.__sink.send(
                    Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
                )
            self.__client = openai.AsyncOpenAI(
                api_key=_API_KEY,
                base_url=f"{entry.url}/v1",
                default_headers=_NO_COMPRESSION_HEADERS,
            )
            return

        server = LlamaServer.get_active_llama_server()
        if server is not None and server.is_running and server.model_name == model_name:
            if self.__client is None:
                self.__client = openai.AsyncOpenAI(
                    api_key=_API_KEY,
                    base_url=f"{server.base_url}/v1",
                    default_headers=_NO_COMPRESSION_HEADERS,
                )
            return

        await self.__sink.send(
            Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "starting": True})
        )

        if entry is None:
            error = f"Unknown local model: {model_name!r}"
            await self.__sink.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": error}
                )
            )
            raise RuntimeError(error)

        try:
            server = await ensure_llama_running(entry, self.__kodo_dir)
        except Exception as exc:
            await self.__sink.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": str(exc)}
                )
            )
            raise

        self.__client = openai.AsyncOpenAI(
            api_key=_API_KEY,
            base_url=f"{server.base_url}/v1",
            default_headers=_NO_COMPRESSION_HEADERS,
        )
        await self.__sink.send(
            Envelope.make_event(
                EVT_LLAMA_STATE,
                {"running": True, "model": server.model_name, "port": server.port},
            )
        )

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
        assert self.__client is not None
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
        parser = ThinkingStreamParser()

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
            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content:
                yield ThinkingDelta(text=reasoning_content)

            if delta.content:
                for event in parser.feed(delta.content):
                    yield event

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if tc.id:
                        tool_ids[idx] = tc.id
                    if tc.function and tc.function.name:
                        tool_names[idx] = tc.function.name
                    if tc.function:
                        # Surface progress as the (possibly huge) arguments
                        # stream in, so the UI shows a live "generating"
                        # indicator instead of freezing. The name may not have
                        # arrived yet on the first fragment ("" until it does);
                        # the client keeps the first non-empty name it sees.
                        fragment = tc.function.arguments or ""
                        if tc.function.name or fragment:
                            yield ToolCallArgDelta(
                                tool_name=tool_names.get(idx, ""),
                                text=fragment,
                            )
                        if fragment:
                            tool_arg_parts.setdefault(idx, []).append(fragment)

        if cancel_event.is_set():
            return

        for event in parser.flush():
            yield event

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
