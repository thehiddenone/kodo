"""OpenRouter LLM plugin — streams via OpenRouter's OpenAI-compatible endpoint."""

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

__all__ = ["OpenRouterPlugin"]

_log = logging.getLogger(__name__)

# OpenRouter's own OpenAI-compatible endpoint (https://openrouter.ai/docs/quickstart)
# -- reached by pointing openai.AsyncOpenAI at this base_url with an
# OpenRouter API key instead of an OpenAI one, same pattern
# kodo/llms/google/_gemini.py, kodo/llms/alibaba/_qwen.py,
# kodo/llms/deepseek/_deepseek.py, and kodo/llms/kimi/_kimi.py already use
# for Gemini/Qwen/DeepSeek/Kimi.
_BASE_URL = "https://openrouter.ai/api/v1"

# OpenRouter's unified `reasoning.effort` parameter accepts several literal
# values kodo never sends ("minimal", "xhigh", "none") -- only these four are
# valid `thinking_level`s for the synthetic "openrouter" thinking family
# (kodo/runtime/_engine/_llm.py's _OPENROUTER_THINKING_TIERS), so they're the
# only ones this plugin ever forwards. Unsupported models are documented to
# silently ignore the whole `reasoning` field
# (https://openrouter.ai/docs/use-cases/reasoning-tokens).
_VALID_EFFORTS = frozenset({"low", "medium", "high", "max"})


def _map_finish_reason(reason: str | None) -> str:
    """Map a Chat Completions ``finish_reason`` to kodo's canonical stop-reason vocabulary.

    Identical mapping to :func:`kodo.llms.kimi._kimi._map_finish_reason`/
    :func:`kodo.llms.deepseek._deepseek._map_finish_reason`/
    :func:`kodo.llms.alibaba._qwen._map_finish_reason`/
    :func:`kodo.llms.google._gemini._map_finish_reason`/
    :func:`kodo.llms.llamacpp._llama._map_finish_reason` -- every
    Chat-Completions-shaped provider in this codebase speaks the same wire
    shape here. ``"max_tokens"`` is the one value with real downstream
    behavior -- ``runtime/_engine/_watchdog.py``'s truncated-generation check
    reads it verbatim.
    """
    if reason == "stop":
        return "end_turn"
    if reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    return reason or "end_turn"


class OpenRouterPlugin(LLMPlugin):
    """OpenRouter implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK pointed at OpenRouter's own
    OpenAI-compatible Chat Completions endpoint, with the same
    exponential-backoff retries and cancellation support as the other cloud
    plugins. Unlike every other vendor here, ``model`` is not drawn from a
    small compiled-in set (:mod:`kodo.llms._cloud_registry`) — it is either
    the special router pseudo-model ``"openrouter/auto"`` or any of the 400+
    models in OpenRouter's own dynamically fetched catalog
    (:mod:`kodo.llms._openrouter_catalog`), so this plugin never assumes
    anything about which specific model it's talking to.

    Two consequences of that follow through the streaming loop:

    * **Reasoning effort is session-controlled, not model-fixed.** Every
      other Chat-Completions-shaped plugin here (Kimi/DeepSeek/Alibaba/
      Gemini) hardcodes one reasoning setting per model, since each has only
      a handful of known models. OpenRouter instead accepts a
      ``thinking_level`` parameter — the same mechanism
      :class:`~kodo.llms.llamacpp._llama.LlamaPlugin` uses for local models,
      extended to this one cloud vendor (see
      ``kodo.runtime._engine._llm.LLMPlumbingMixin._thinking_kwargs``) —
      mapped directly onto OpenRouter's own unified ``reasoning.effort``
      parameter (https://openrouter.ai/docs/use-cases/reasoning-tokens).
    * **Cost is read off the response, not computed from a pricing table.**
      Every other vendor's ``compute_cost`` multiplies token counts by a
      hand-maintained per-model USD/token table. That doesn't scale to 400+
      models, and can't work at all for ``openrouter/auto`` (its own catalog
      entry reports ``-1`` pricing — its real per-token rate depends on
      whichever model it routes a request to). OpenRouter's own Chat
      Completions response reports the exact USD cost for every call in the
      final chunk's ``usage.cost`` field
      (https://openrouter.ai/docs/use-cases/usage-accounting), which this
      plugin reads directly into ``Usage.provider_reported_cost``.

    Reasoning text arrives on ``delta.reasoning_details`` (a list of typed
    objects), not the flat ``delta.reasoning_content`` string
    Kimi/DeepSeek/Alibaba/Gemini use — see :meth:`__extract_reasoning`.
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with an OpenRouter API key.

        Args:
            api_key (str): OpenRouter API key (not written to disk per NFR-06).
        """
        # max_retries=0: kodo.llms._provider_retry/_gateway own retry/backoff.
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL, max_retries=0)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def supported_models(self) -> list[str]:
        # Unlike every other vendor here, OpenRouter's real model list is
        # fetched/cached dynamically (kodo.llms._openrouter_catalog), not a
        # small compiled-in set. The router pseudo-model is always valid
        # without needing a catalog fetch, so it's the one entry listed here.
        return ["openrouter/auto"]

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
        """Stream an OpenRouter response via Chat Completions, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): OpenRouter model identifier (``"openrouter/auto"``
                or any catalog model id, e.g. ``"anthropic/claude-sonnet-4"``).
            system (str): System prompt text (sent as the first ``system`` message).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- see the class docstring.
            thinking_level (str | None): The session's reasoning-effort
                tier for the synthetic "openrouter" thinking family
                (``"low"``/``"medium"``/``"high"``/``"max"``), or ``None``.
                Forwarded verbatim as OpenRouter's ``reasoning.effort``.

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
        # OpenRouter's upstream caching is provider-specific -- see _convert.py
        del cache_breakpoints

        oai_messages = build_chat_messages(system, messages)
        oai_tools = build_openai_tools(tools)

        extra_body: dict[str, object] = {}
        if thinking_level in _VALID_EFFORTS:
            extra_body["reasoning"] = {"effort": thinking_level}

        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        # The model actually asked for is what we fall back to if a chunk
        # never carries its own `model` field for some reason -- in
        # practice every chunk does, and for "openrouter/auto" this is
        # overwritten with whichever upstream model really answered.
        served_model = model
        provider_cost: float | None = None

        # model/messages/tools are plain dicts/strings built above (not the
        # SDK's own narrow param TypedDicts/Literals), so this can't
        # statically match the .create() overload -- same imprecision
        # kimi/_kimi.py's/deepseek/_deepseek.py's own SDK calls accept.
        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
        )
        async for chunk in response:
            if cancel_event.is_set():
                _log.debug("Stream cancelled by caller")
                return

            if chunk.model:
                served_model = chunk.model

            if chunk.usage is not None:
                input_tokens = chunk.usage.prompt_tokens
                output_tokens = chunk.usage.completion_tokens
                details = chunk.usage.prompt_tokens_details
                cache_read_tokens = details.cached_tokens if details is not None else 0
                # Not part of the OpenAI SDK's typed Usage model -- an
                # OpenRouter extension field
                # (https://openrouter.ai/docs/use-cases/usage-accounting),
                # present on the final chunk's usage object.
                raw_cost = getattr(chunk.usage, "cost", None)
                if isinstance(raw_cost, int | float):
                    provider_cost = float(raw_cost)

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = choice.delta
            reasoning_text = self.__extract_reasoning(delta)
            if reasoning_text:
                yield ThinkingDelta(text=reasoning_text)

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
                model=served_model,
                provider_reported_cost=provider_cost,
            ),
            stop_reason=_map_finish_reason(finish_reason),
        )

    @staticmethod
    def __extract_reasoning(delta: object) -> str:
        """Concatenate reasoning text off one Chat Completions stream delta.

        OpenRouter's unified reasoning-tokens shape puts reasoning text in
        ``delta.reasoning_details`` (a list of typed objects; only entries
        with ``type == "reasoning.text"`` carry a ``text`` field) rather than
        the flat ``delta.reasoning_content`` string every other
        Chat-Completions-shaped vendor here uses
        (https://openrouter.ai/docs/use-cases/reasoning-tokens). A flat
        ``delta.reasoning`` string is also checked as a defensive fallback,
        since an upstream model proxied through OpenRouter's own
        compatibility shims might emit the simpler shape instead. Neither
        field is part of the OpenAI SDK's typed ``ChoiceDelta`` model, hence
        ``getattr`` throughout.
        """
        details = getattr(delta, "reasoning_details", None)
        if isinstance(details, list):
            parts: list[str] = []
            for item in details:
                item_type = (
                    item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
                )
                if item_type != "reasoning.text":
                    continue
                text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                if isinstance(text, str) and text:
                    parts.append(text)
            if parts:
                return "".join(parts)
        flat = getattr(delta, "reasoning", None)
        return flat if isinstance(flat, str) else ""
