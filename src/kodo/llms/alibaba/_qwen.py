"""Alibaba Qwen LLM plugin — streaming via Model Studio's OpenAI-compatible endpoint."""

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

__all__ = ["QwenPlugin"]

_log = logging.getLogger(__name__)

# Alibaba Cloud Model Studio's OpenAI-compatible endpoint
# (https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)
# -- reached by pointing openai.AsyncOpenAI at this base_url with a DashScope
# (Model Studio) API key instead of an OpenAI one, same pattern
# kodo/llms/google/_gemini.py already uses for Gemini. This is the
# international endpoint; DashScope also has a Beijing-region one
# (https://dashscope.aliyuncs.com/compatible-mode/v1) not used here.
_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# Whether to request thinking output for a given model, via the
# ``enable_thinking`` field DashScope's docs describe as passed through
# ``extra_body`` (not a standard OpenAI Chat Completions parameter, see
# https://www.alibabacloud.com/help/en/model-studio/deep-thinking). Every
# model kodo registers today is one of Qwen's "hybrid" models -- Alibaba's
# own docs describe these as thinking-enabled by default from the Qwen3.5
# generation onward -- so this is set unconditionally True and passed
# explicitly rather than relied on as an undocumented default, same
# "one fixed reasoning setting per model, not per kodo tier" rationale as
# kodo/llms/google/_gemini.py's own table (there is no per-tier knob to
# thread through the silent-turn helpers for one vendor's benefit alone).
# kodo always streams (stream=True below), which sidesteps a DashScope
# quirk where some non-streaming thinking-model calls require the caller to
# pass enable_thinking=False explicitly.
_ENABLE_THINKING: dict[str, bool] = {
    "qwen3.8-max": True,
    "qwen3.8-plus": True,
    "qwen3.8-flash": True,
}
_DEFAULT_ENABLE_THINKING = True


def _enable_thinking_for(model: str) -> bool:
    """Fixed thinking-enabled flag for *model* (DashScope ``enable_thinking``)."""
    return _ENABLE_THINKING.get(model, _DEFAULT_ENABLE_THINKING)


# *How hard* to think, on top of the always-on `enable_thinking` switch above,
# is session-controlled: the engine passes the session's thinking_level
# (kodo.llms._cloud_thinking's "alibaba" family) on every call and it rides
# DashScope's own `reasoning_effort` field, nested in `extra_body` alongside
# `enable_thinking` since neither is a standard OpenAI Chat Completions
# parameter (https://www.alibabacloud.com/help/en/model-studio/deep-thinking).
#
# Qwen3.8's scale has a hole where "high" would be -- the documented levels
# are "low"/"medium"/"xhigh" -- so this is the one vendor here whose tier list
# is not a prefix of the usual effort ladder. Default "xhigh" is Qwen's own
# documented default, matching the always-thinking posture the fixed
# `enable_thinking=True` above already encoded.
#
# The other DashScope depth control, `thinking_budget` (1-32768 reasoning
# tokens), is deliberately never sent: qwen3.8-max rejects a request carrying
# both, and a graded effort maps onto kodo's tier control without a second
# token-budget scale to reconcile.
_REASONING_EFFORTS = frozenset({"low", "medium", "xhigh"})
_DEFAULT_REASONING_EFFORT = "xhigh"


def _reasoning_effort_for(thinking_level: str | None) -> str:
    """DashScope ``reasoning_effort`` for this request's thinking tier.

    Args:
        thinking_level (str | None): Session tier slug, or ``None``. Anything
            outside :data:`_REASONING_EFFORTS` falls back to the family
            default rather than being forwarded to the API.

    Returns:
        str: A valid ``reasoning_effort`` value.
    """
    if thinking_level in _REASONING_EFFORTS:
        return str(thinking_level)
    return _DEFAULT_REASONING_EFFORT


def _map_finish_reason(reason: str | None) -> str:
    """Map a Chat Completions ``finish_reason`` to kodo's canonical stop-reason vocabulary.

    Identical mapping to :func:`kodo.llms.google._gemini._map_finish_reason`/
    :func:`kodo.llms.llamacpp._llama._map_finish_reason` -- all three
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


class QwenPlugin(LLMPlugin):
    """Alibaba implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK pointed at Alibaba Cloud Model
    Studio's OpenAI-compatible Chat Completions endpoint (documented as a
    drop-in SDK target), with the same exponential-backoff retries and
    cancellation support as the OpenAI/Meta/Google plugins. Prompt caching is
    automatic on Alibaba's side (its "context cache" feature) -- there is no
    explicit cache-breakpoint mechanism to implement, matching the other
    cloud plugins.

    Like Google (and unlike Anthropic/OpenAI/Meta), this plugin's message/tool
    conversion and streaming loop are Chat-Completions-shaped -- adapted from
    :mod:`kodo.llms.google._gemini`, itself adapted from
    :mod:`kodo.llms.llamacpp._llama`. Qwen reports reasoning text on its own
    ``delta.reasoning_content`` field, the same field name/shape Gemini uses,
    so the streaming loop is nearly identical -- the one real difference is
    Qwen's ``enable_thinking`` request flag (nested in ``extra_body``, since
    it is not a standard OpenAI parameter) in place of Gemini's direct
    ``reasoning_effort=`` kwarg, and the absence of Gemini's mandatory
    per-tool-call ``thought_signature`` replay requirement (see
    kodo/llms/alibaba/_convert.py's module docstring).
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with an Alibaba Cloud Model Studio (DashScope) API key.

        Args:
            api_key (str): Model Studio API key (not written to disk per NFR-06).
        """
        # max_retries=0: kodo.llms._provider_retry/_gateway own retry/backoff.
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL, max_retries=0)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "alibaba"

    @property
    def supported_models(self) -> list[str]:
        return ["qwen3.8-max", "qwen3.8-plus", "qwen3.8-flash"]

    def stream_query(
        self,
        *,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
        thinking_level: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a Qwen response via Chat Completions, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Qwen model identifier.
            system (str): System prompt text (sent as the first ``system`` message).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- Alibaba's context-cache prompt caching is automatic.
            thinking_level (str | None): The session's reasoning tier for the
                ``"alibaba"`` thinking family (``"low"``/``"medium"``/
                ``"xhigh"``), or ``None`` for the family default. Forwarded as
                DashScope's ``reasoning_effort`` inside ``extra_body``.
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
            thinking_level=thinking_level,
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
        thinking_level: str | None,
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
                    thinking_level=thinking_level,
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
        thinking_level: str | None,
    ) -> AsyncIterator[StreamEvent]:
        del cache_breakpoints  # Alibaba's context-cache caching is automatic -- see _convert.py

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
        # google/_gemini.py accepts for its own SDK call.
        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            extra_body={
                "enable_thinking": _enable_thinking_for(model),
                "reasoning_effort": _reasoning_effort_for(thinking_level),
            },
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
