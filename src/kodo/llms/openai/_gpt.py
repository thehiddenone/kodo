"""OpenAI GPT LLM plugin — Responses API streaming, reasoning, and usage tracking."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import openai
from openai.types.responses import (
    Response,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from kodo.llms._interface import (
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
)

from ._convert import build_input_items, build_tool_defs
from ._retry import with_retry_iter

__all__ = ["GPTPlugin"]

_log = logging.getLogger(__name__)

# Kōdo's four capability tiers (low/medium/high/max) map onto three real
# model IDs this generation -- Terra covers both "medium" and "high", Sol is
# reserved for "max" (see kodo/server/_config.py's models.cloud.openai
# defaults). There is therefore no per-kodo-tier reasoning-effort knob to
# thread through here (that would need capability plumbed all the way through
# _run_silent_return_turn/_run_silent_tool_loop_turn/the main turn loop) --
# each MODEL instead gets one fixed reasoning effort, matching its own
# positioning (Luna: lightweight/fast, Terra: balanced, Sol: flagship).
_REASONING_EFFORT: dict[str, str] = {
    "gpt-5.6-luna": "minimal",
    "gpt-5.6-terra": "medium",
    "gpt-5.6-sol": "high",
}
_DEFAULT_REASONING_EFFORT = "medium"


def _reasoning_effort_for(model: str) -> str:
    """Fixed reasoning-effort tier for *model* (Responses API ``reasoning.effort``)."""
    return _REASONING_EFFORT.get(model, _DEFAULT_REASONING_EFFORT)


def _map_stop_reason(response: Response, made_tool_call: bool) -> str:
    """Map a finished Response to kodo's canonical stop-reason vocabulary.

    ``"max_tokens"`` is the one value with real downstream behavior --
    ``runtime/_engine/_watchdog.py``'s truncated-generation check reads it
    verbatim, matching what llama.cpp's plugin already reports for the same
    condition.
    """
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        return "max_tokens" if reason == "max_output_tokens" else "incomplete"
    return "tool_use" if made_tool_call else "end_turn"


class GPTPlugin(LLMPlugin):
    """OpenAI implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK's Responses API, with
    exponential-backoff retries (mirroring the Anthropic plugin's FR-LLM-05)
    and cancellation support (FR-LLM-07). Prompt caching is automatic on
    OpenAI's side -- there is no explicit cache-breakpoint mechanism to
    implement, unlike the Anthropic plugin.
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with an OpenAI API key.

        Args:
            api_key (str): OpenAI API key (not written to disk per NFR-06).
        """
        # max_retries=0: kodo.llms._provider_retry/_gateway own retry/backoff.
        self.__client = openai.AsyncOpenAI(api_key=api_key, max_retries=0)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supported_models(self) -> list[str]:
        return ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

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
        """Stream a GPT response via the Responses API, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): GPT model identifier.
            system (str): System prompt text (sent as ``instructions=``).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- Responses API caching is automatic.

        Yields:
            StreamEvent: Token/reasoning deltas, tool calls, then :class:`TurnEnd`.
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
        del cache_breakpoints  # Responses API caching is automatic -- see _convert.py

        input_items = build_input_items(messages)
        tool_defs = build_tool_defs(tools)

        item_names: dict[str, str] = {}  # streaming item id -> tool name
        made_tool_call = False

        # model/input/tools are plain dicts/strings built above (not the SDK's
        # own narrow param TypedDicts/Literals), so this can't statically match
        # either .stream() overload -- same imprecision _claude.py accepts for
        # its own SDK calls.
        async with self.__client.responses.stream(  # type: ignore[call-overload]
            model=model,
            instructions=system,
            input=input_items,
            reasoning={"effort": _reasoning_effort_for(model), "summary": "auto"},
            # kodo resends full conversation history every turn (like the
            # Anthropic plugin) and never uses previous_response_id chaining,
            # so there is no reason to let OpenAI retain this response
            # server-side -- privacy parity with Anthropic's Messages API,
            # which has no persistent-storage concept at all.
            store=False,
            **({"tools": tool_defs} if tool_defs else {}),
        ) as stream:
            async for raw_event in stream:
                if cancel_event.is_set():
                    _log.debug("Stream cancelled by caller")
                    return

                if isinstance(raw_event, ResponseTextDeltaEvent):
                    yield TokenDelta(text=raw_event.delta)
                elif isinstance(raw_event, ResponseReasoningSummaryTextDeltaEvent):
                    yield ThinkingDelta(text=raw_event.delta)
                elif isinstance(raw_event, ResponseOutputItemAddedEvent):
                    item = raw_event.item
                    if isinstance(item, ResponseFunctionToolCall) and item.id:
                        item_names[item.id] = item.name
                elif isinstance(raw_event, ResponseFunctionCallArgumentsDeltaEvent):
                    yield ToolCallArgDelta(
                        tool_name=item_names.get(raw_event.item_id, ""),
                        text=raw_event.delta,
                    )
                elif isinstance(raw_event, ResponseOutputItemDoneEvent):
                    item = raw_event.item
                    if isinstance(item, ResponseFunctionToolCall):
                        made_tool_call = True
                        try:
                            tool_input: dict[str, object] = (
                                json.loads(item.arguments) if item.arguments else {}
                            )
                        except json.JSONDecodeError:
                            tool_input = {"_raw": item.arguments}
                        yield ToolCallEvent(
                            tool_use_id=item.call_id,
                            tool_name=item.name,
                            tool_input=tool_input,
                        )

            if not cancel_event.is_set():
                final = await stream.get_final_response()
                raw_usage = final.usage
                usage = Usage(
                    input_tokens=raw_usage.input_tokens if raw_usage else 0,
                    output_tokens=raw_usage.output_tokens if raw_usage else 0,
                    cache_write_tokens=0,
                    cache_read_tokens=(
                        raw_usage.input_tokens_details.cached_tokens if raw_usage else 0
                    ),
                    model=model,
                )
                yield TurnEnd(usage=usage, stop_reason=_map_stop_reason(final, made_tool_call))
