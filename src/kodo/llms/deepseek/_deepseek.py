"""DeepSeek LLM plugin — streaming via DeepSeek's OpenAI-compatible endpoint."""

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
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
    Usage,
)

from ._convert import build_chat_messages, build_openai_tools
from ._retry import with_retry_iter

__all__ = ["DeepSeekPlugin"]

_log = logging.getLogger(__name__)

# DeepSeek's own OpenAI-compatible endpoint
# (https://api-docs.deepseek.com/) -- reached by pointing openai.AsyncOpenAI
# at this base_url with a DeepSeek platform API key instead of an OpenAI one,
# same pattern kodo/llms/google/_gemini.py and kodo/llms/alibaba/_qwen.py
# already use for Gemini/Qwen.
_BASE_URL = "https://api.deepseek.com/v1"

# Kōdo's four capability tiers map onto two real DeepSeek model IDs -- V4 Pro
# covers high/max, V4 Flash covers low/medium (see kodo/server/_config.py's
# models.cloud.deepseek defaults) -- a plain 2-2 split, unlike Google's
# asymmetric 3-1 one, since DeepSeek's naming makes the capability gap
# explicit (Pro is the deep-reasoning flagship, Flash the fast/cheap
# everyday model) rather than "Flash" itself being the strong SKU.
#
# Reasoning ("thinking") is enabled unconditionally for both models -- see
# _extra_body_for below -- with a per-model *graded* reasoning_effort, same
# "one fixed reasoning setting per model, not per kodo tier" rationale as
# kodo/llms/google/_gemini.py's own table (there is no per-tier knob to
# thread through the silent-turn helpers for one vendor's benefit alone).
# DeepSeek's docs (https://api-docs.deepseek.com/guides/thinking_mode)
# describe "low"/"high"/"max" (or, per a since-superseded source, "high"/
# "xhigh") effort strings for both models -- "high" is used here as V4
# Flash's default (fast model, still gets real reasoning) and "max" for V4
# Pro (the flagship reasoning tier), a judgment call given the inconsistent
# naming across sources rather than a documented recommendation.
_REASONING_EFFORT: dict[str, str] = {
    "deepseek-v4-pro": "max",
    "deepseek-v4-flash": "high",
}
_DEFAULT_REASONING_EFFORT = "high"


def _extra_body_for(model: str) -> dict[str, object]:
    """Fixed thinking-mode request body for *model* (DeepSeek's ``extra_body``).

    Unlike Qwen's single boolean ``enable_thinking`` flag or Gemini's direct
    ``reasoning_effort=`` kwarg, DeepSeek nests a graded ``reasoning_effort``
    string inside the same ``extra_body`` dict as its ``thinking.type``
    toggle -- neither is a standard OpenAI Chat Completions parameter.
    """
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": _REASONING_EFFORT.get(model, _DEFAULT_REASONING_EFFORT),
    }


def _map_finish_reason(reason: str | None) -> str:
    """Map a Chat Completions ``finish_reason`` to kodo's canonical stop-reason vocabulary.

    Identical mapping to :func:`kodo.llms.alibaba._qwen._map_finish_reason`/
    :func:`kodo.llms.google._gemini._map_finish_reason`/
    :func:`kodo.llms.llamacpp._llama._map_finish_reason` -- all four
    providers speak the same OpenAI Chat Completions wire shape.
    ``"max_tokens"`` is the one value with real downstream behavior --
    ``runtime/_engine/_watchdog.py``'s truncated-generation check reads it
    verbatim.
    """
    if reason == "stop":
        return "end_turn"
    if reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    return reason or "end_turn"


class DeepSeekPlugin(LLMPlugin):
    """DeepSeek implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK pointed at DeepSeek's own
    OpenAI-compatible Chat Completions endpoint (documented as a drop-in SDK
    target), with the same exponential-backoff retries and cancellation
    support as the OpenAI/Meta/Google/Alibaba plugins. Prompt caching is
    automatic on DeepSeek's side (its disk-backed context cache) -- there is
    no explicit cache-breakpoint mechanism to implement, matching the other
    cloud plugins.

    Like Google/Alibaba (and unlike Anthropic/OpenAI/Meta), this plugin's
    message/tool conversion and streaming loop are Chat-Completions-shaped --
    adapted from :mod:`kodo.llms.alibaba._qwen`, itself adapted from
    :mod:`kodo.llms.google._gemini`. DeepSeek reports reasoning text on its
    own ``delta.reasoning_content`` field, the same field name/shape Gemini
    and Qwen use, so the streaming loop is nearly identical -- the one real
    difference is DeepSeek's request body (see ``_extra_body_for`` above) and
    the absence of Gemini's mandatory per-tool-call ``thought_signature``
    replay requirement (see kodo/llms/deepseek/_convert.py's module
    docstring).
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with a DeepSeek platform API key.

        Args:
            api_key (str): DeepSeek API key (not written to disk per NFR-06).
        """
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def supported_models(self) -> list[str]:
        return ["deepseek-v4-pro", "deepseek-v4-flash"]

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
        """Stream a DeepSeek response via Chat Completions, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): DeepSeek model identifier.
            system (str): System prompt text (sent as the first ``system`` message).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- DeepSeek's context caching is automatic.

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
        del cache_breakpoints  # DeepSeek's context caching is automatic -- see _convert.py

        oai_messages = build_chat_messages(system, messages)
        oai_tools = build_openai_tools(tools)

        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0

        # model/messages/tools are plain dicts/strings built above (not the
        # SDK's own narrow param TypedDicts/Literals), so this can't
        # statically match the .create() overload -- same imprecision
        # alibaba/_qwen.py accepts for its own SDK call.
        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            extra_body=_extra_body_for(model),
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
                details = chunk.usage.prompt_tokens_details
                cache_read_tokens = details.cached_tokens if details is not None else 0

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
                yield TokenDelta(text=delta.content)

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
                cache_read_tokens=cache_read_tokens,
                model=model,
            ),
            stop_reason=_map_finish_reason(finish_reason),
        )
