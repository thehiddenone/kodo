"""Kimi (Moonshot AI) LLM plugin — streaming via Kimi's OpenAI-compatible endpoint."""

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

__all__ = ["KimiPlugin"]

_log = logging.getLogger(__name__)

# Moonshot AI's own OpenAI-compatible endpoint
# (https://platform.moonshot.ai/docs/guide/migrating-from-openai-to-kimi) --
# reached by pointing openai.AsyncOpenAI at this base_url with a Moonshot
# platform API key instead of an OpenAI one, same pattern
# kodo/llms/google/_gemini.py, kodo/llms/alibaba/_qwen.py, and
# kodo/llms/deepseek/_deepseek.py already use for Gemini/Qwen/DeepSeek.
_BASE_URL = "https://api.moonshot.ai/v1"

# Kōdo's four capability tiers map onto two real Kimi model IDs. Moonshot's
# current (2026-08-16) lineup per its own model list
# (https://platform.kimi.ai/docs/models) has three non-deprecated SKUs --
# kimi-k3 (flagship, 2.8T-param MoE, 1M context, always-on max reasoning),
# kimi-k2.6 (general-purpose, vision + text, 256K context, thinking
# toggleable), and kimi-k2.7-code (coding-tuned, 256K context, thinking
# toggleable, same $0.95/$4.00 price as K2.6) -- plus kimi-k2.5, which is
# still live for existing keys but no longer offered to new signups ahead of
# a full platform sunset on 2026-08-31, and the kimi-k2 series, discontinued
# outright on 2026-05-25. kimi-k2.5 and kimi-k2 are deliberately not
# registered here, same "don't register a model already on its way out"
# posture as deepseek/_deepseek.py's dropped legacy aliases.
#
# Of the two current non-flagship SKUs, kimi-k2.7-code is registered here and
# kimi-k2.6 is not -- a judgment call, not a documented Moonshot
# recommendation: the two are identically priced and comparably capable, but
# Kōdo is a coding agent with no use for K2.6's vision input, and K2.7 Code's
# own docs claim higher success rates on coding tasks specifically, so it is
# the more relevant "everyday" model for this product. This makes for a
# plain 2-2 split -- kimi-k2.7-code covers low/medium, kimi-k3 covers
# high/max (kodo/server/_config.py's models.cloud.kimi defaults) -- the same
# shape as DeepSeek's, for a similar underlying reason: Kimi's real price-tier
# count today is two, not three.
#
# Reasoning ("thinking") is enabled unconditionally for both models, same
# posture as DeepSeek/Alibaba -- see _reasoning_kwargs_for below for the
# (genuinely different) mechanism each model uses.
_REASONING_EFFORT: dict[str, str] = {
    "kimi-k3": "max",
}
_DEFAULT_REASONING_EFFORT = "max"


def _reasoning_kwargs_for(model: str) -> dict[str, object]:
    """Extra ``chat.completions.create()`` kwargs enabling reasoning for *model*.

    Kimi's two model families do not share a reasoning-config shape.
    ``kimi-k3`` (thinking permanently on) takes a graded ``reasoning_effort``
    as a **top-level** keyword argument -- the same direct-kwarg placement
    :mod:`kodo.llms.google._gemini` uses for Gemini, *not* nested in
    ``extra_body`` like DeepSeek's/Qwen's own thinking toggles (confirmed
    against Moonshot's own K3 quickstart docs:
    https://platform.kimi.ai/docs/guide/kimi-k3-quickstart -- "configure
    reasoning effort with the top-level reasoning_effort request field...; do
    not reuse the K2.x thinking parameter"). Every other registered model
    (today, only ``kimi-k2.7-code``) instead takes a boolean ``thinking.type``
    switch nested inside ``extra_body`` -- the same field name/shape
    DeepSeek's own ``thinking.type`` toggle uses -- since it is not a standard
    Chat Completions parameter (confirmed against
    https://platform.kimi.ai/docs/guide/use-kimi-k2-thinking-model); fixed at
    ``"enabled"`` (always-on), same "one fixed reasoning setting per model,
    not per kodo tier" posture as every other vendor's table here.
    """
    if model == "kimi-k3":
        return {"reasoning_effort": _REASONING_EFFORT.get(model, _DEFAULT_REASONING_EFFORT)}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


def _map_finish_reason(reason: str | None) -> str:
    """Map a Chat Completions ``finish_reason`` to kodo's canonical stop-reason vocabulary.

    Identical mapping to :func:`kodo.llms.deepseek._deepseek._map_finish_reason`/
    :func:`kodo.llms.alibaba._qwen._map_finish_reason`/
    :func:`kodo.llms.google._gemini._map_finish_reason`/
    :func:`kodo.llms.llamacpp._llama._map_finish_reason` -- all five
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


class KimiPlugin(LLMPlugin):
    """Moonshot AI Kimi implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK pointed at Moonshot's own
    OpenAI-compatible Chat Completions endpoint (documented as a drop-in SDK
    target), with the same exponential-backoff retries and cancellation
    support as the OpenAI/Meta/Google/Alibaba/DeepSeek plugins. Prompt
    caching is automatic on Kimi's side (its prefix-based context cache) --
    there is no explicit cache-breakpoint mechanism to implement, matching the
    other cloud plugins.

    Like Google/Alibaba/DeepSeek (and unlike Anthropic/OpenAI/Meta), this
    plugin's message/tool conversion and streaming loop are
    Chat-Completions-shaped -- adapted from :mod:`kodo.llms.deepseek._deepseek`,
    itself adapted from :mod:`kodo.llms.alibaba._qwen`. Kimi reports reasoning
    text on its own ``delta.reasoning_content`` field, the same field
    name/shape Gemini/Qwen/DeepSeek use, so the streaming loop is nearly
    identical -- the one real difference is that Kimi's two model families use
    two genuinely different reasoning-config mechanisms on the *request* side
    (see ``_reasoning_kwargs_for`` above), and, like DeepSeek, there is no
    Gemini-style mandatory per-tool-call ``thought_signature`` replay
    requirement (see kodo/llms/kimi/_convert.py's module docstring).
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with a Moonshot AI platform API key.

        Args:
            api_key (str): Kimi/Moonshot API key (not written to disk per NFR-06).
        """
        # max_retries=0: kodo.llms._provider_retry/_gateway own retry/backoff.
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL, max_retries=0)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def supported_models(self) -> list[str]:
        return ["kimi-k3", "kimi-k2.7-code"]

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
        """Stream a Kimi response via Chat Completions, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Kimi model identifier.
            system (str): System prompt text (sent as the first ``system`` message).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- Kimi's context caching is automatic.

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
        del cache_breakpoints  # Kimi's context caching is automatic -- see _convert.py

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
        # alibaba/_qwen.py's and deepseek/_deepseek.py's own SDK calls accept.
        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            stream=True,
            stream_options={"include_usage": True},
            **_reasoning_kwargs_for(model),
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
