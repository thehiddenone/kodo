"""Meta Muse LLM plugin -- Meta Model API streaming via the Responses API shape."""

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

__all__ = ["MusePlugin"]

_log = logging.getLogger(__name__)

# Meta's Model API (https://dev.meta.ai/docs/) -- OpenAI SDK compatible,
# reached by pointing openai.AsyncOpenAI at this base_url with a Meta-issued
# key instead of an OpenAI one.
_BASE_URL = "https://api.meta.ai/v1"

# Meta has no effort-tiered lineup (kodo/llms/_cloud_registry.py's
# _META_MODELS docstring) -- Muse Spark 1.2 is one model spanning all four of
# kodo's effort tiers, so unlike OpenAI's per-model _REASONING_EFFORT table
# there is no model identity to pick an effort from. A single fixed Responses
# API `reasoning.effort` applies to every call, same "not worth threading
# kodo's own capability tier through the silent-turn helpers for one vendor"
# rationale as kodo/llms/openai/_gpt.py's own table.
_REASONING_EFFORT = "medium"

# The suffix Meta's Model API model id gets when the account-level
# "contributor" tier is active (settings.json's meta_contributor_tier,
# threaded in via MusePlugin's constructor) -- see kodo/llms/meta/_usage.py
# for the discounted pricing row it selects.
_CONTRIBUTOR_SUFFIX = "-contributor"


def _map_stop_reason(response: Response, made_tool_call: bool) -> str:
    """Map a finished Response to kodo's canonical stop-reason vocabulary.

    Identical to :func:`kodo.llms.openai._gpt._map_stop_reason` -- Meta's
    Model API is Responses-API-shaped, so the same ``incomplete_details``
    convention applies. ``"max_tokens"`` is the one value with real
    downstream behavior (``runtime/_engine/_watchdog.py``'s truncated-
    generation check reads it verbatim).
    """
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        return "max_tokens" if reason == "max_output_tokens" else "incomplete"
    return "tool_use" if made_tool_call else "end_turn"


class MusePlugin(LLMPlugin):
    """Meta implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Reuses the official ``openai`` Python SDK pointed at Meta's Model API
    base URL (documented as drop-in OpenAI-SDK compatible), with the same
    exponential-backoff retries and cancellation support as the OpenAI
    plugin. Prompt caching is automatic, same as OpenAI's Responses API.

    The account-level "contributor" tier (heavily discounted pricing in
    exchange for permission to train future Meta models on the traffic) is
    not a separate registry model -- it is a per-plugin-instance flag,
    resolved from ``settings.json``'s ``meta_contributor_tier`` by
    ``kodo/runtime/_engine/_llm.py``'s vendor factory and passed in here.
    When set, every outbound call's model id gets a ``-contributor`` suffix
    (both the actual API request and the ``Usage.model`` reported back, so
    :mod:`kodo.llms.meta._usage` prices it at the discounted rate) --
    ``supported_models``/the cloud registry still only ever expose the bare
    ``muse-spark-1.2`` id, since the toggle is account-wide, not a model a
    user picks per effort tier.
    """

    __client: openai.AsyncOpenAI
    __contributor: bool
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str, *, contributor: bool = False) -> None:
        """Initialise with a Meta Model API key.

        Args:
            api_key (str): Meta Model API key (not written to disk per NFR-06).
            contributor (bool): Whether the account-level "contributor" tier
                is active -- see the class docstring.
        """
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL)
        self.__contributor = contributor
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "meta"

    @property
    def supported_models(self) -> list[str]:
        return ["muse-spark-1.2"]

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
        """Stream a Muse response via the Responses API, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Muse model identifier, e.g. ``"muse-spark-1.2"`` --
                rewritten with the ``-contributor`` suffix before it reaches
                the API when this plugin instance was constructed with
                ``contributor=True``.
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

        api_model = f"{model}{_CONTRIBUTOR_SUFFIX}" if self.__contributor else model
        input_items = build_input_items(messages)
        tool_defs = build_tool_defs(tools)

        item_names: dict[str, str] = {}  # streaming item id -> tool name
        made_tool_call = False

        # model/input/tools are plain dicts/strings built above (not the SDK's
        # own narrow param TypedDicts/Literals), so this can't statically match
        # either .stream() overload -- same imprecision _gpt.py accepts for
        # its own SDK calls.
        async with self.__client.responses.stream(  # type: ignore[call-overload]
            model=api_model,
            instructions=system,
            input=input_items,
            reasoning={"effort": _REASONING_EFFORT, "summary": "auto"},
            # kodo resends full conversation history every turn (like the
            # Anthropic and OpenAI plugins) and never uses
            # previous_response_id chaining, so there is no reason to let
            # Meta retain this response server-side.
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
                    # api_model (not the caller's nominal `model`) so a
                    # contributor-tier call prices at the discounted rate --
                    # see kodo.llms.meta._usage.compute_cost.
                    model=api_model,
                )
                yield TurnEnd(usage=usage, stop_reason=_map_stop_reason(final, made_tool_call))
