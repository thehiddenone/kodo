"""Google Gemini LLM plugin — streaming via Gemini's OpenAI-Chat-Completions-compatible endpoint."""

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

__all__ = ["GeminiPlugin"]

_log = logging.getLogger(__name__)

# Gemini's OpenAI-compatible endpoint (https://ai.google.dev/gemini-api/docs/openai)
# -- reached by pointing openai.AsyncOpenAI at this base_url with a Gemini API
# key instead of an OpenAI one, same pattern kodo/llms/meta/_muse.py already
# uses for Meta's Model API.
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Kōdo's four capability tiers map onto two real Gemini model IDs -- Flash
# covers medium/high/max (see kodo/server/_config.py's models.cloud.google
# defaults), Flash-Lite is reserved for low. Same "one fixed reasoning effort
# per MODEL, not per kodo tier" rationale as kodo/llms/openai/_gpt.py's own
# table: there is no per-tier knob to thread through the silent-turn helpers
# for one vendor's benefit alone.
_REASONING_EFFORT: dict[str, str] = {
    "gemini-3.6-flash": "medium",
    "gemini-3.5-flash-lite": "low",
}
_DEFAULT_REASONING_EFFORT = "medium"


def _reasoning_effort_for(model: str) -> str:
    """Fixed reasoning-effort tier for *model* (Chat Completions ``reasoning_effort``)."""
    return _REASONING_EFFORT.get(model, _DEFAULT_REASONING_EFFORT)


def _map_finish_reason(reason: str | None) -> str:
    """Map a Chat Completions ``finish_reason`` to kodo's canonical stop-reason vocabulary.

    Identical mapping to :func:`kodo.llms.llamacpp._llama._map_finish_reason` --
    both providers speak the same OpenAI Chat Completions wire shape.
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


class GeminiPlugin(LLMPlugin):
    """Google implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``openai`` Python SDK pointed at Gemini's OpenAI-
    compatible Chat Completions endpoint (documented as a drop-in SDK
    target), with the same exponential-backoff retries and cancellation
    support as the OpenAI/Meta plugins. Prompt caching is automatic on
    Gemini's side -- there is no explicit cache-breakpoint mechanism to
    implement, matching OpenAI's/Meta's Responses-API plugins even though
    the wire shape here is Chat Completions, not Responses.

    Unlike the OpenAI/Meta plugins (Responses-API-shaped), this plugin's
    message/tool conversion and streaming loop are adapted from
    :mod:`kodo.llms.llamacpp._llama` -- the only other Chat-Completions-
    shaped plugin in this codebase -- stripped of every local-only concern
    (no ``<think>``-tag parsing, no malformed-tool-call salvage, no
    per-request ``max_tokens`` cap): Gemini reports reasoning text on its own
    ``delta.reasoning_content`` field directly, with no local-model tag
    convention to parse out of the content channel.
    """

    __client: openai.AsyncOpenAI
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with a Google Gemini API key.

        Args:
            api_key (str): Gemini API key (not written to disk per NFR-06).
        """
        self.__client = openai.AsyncOpenAI(api_key=api_key, base_url=_BASE_URL)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "google"

    @property
    def supported_models(self) -> list[str]:
        return ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

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
        """Stream a Gemini response via Chat Completions, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Gemini model identifier.
            system (str): System prompt text (sent as the first ``system`` message).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored -- Gemini's prompt caching is automatic.

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
        del cache_breakpoints  # Gemini's prompt caching is automatic -- see _convert.py

        oai_messages = build_chat_messages(system, messages)
        oai_tools = build_openai_tools(tools)

        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        tool_signatures: dict[int, str] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0

        # model/messages/tools are plain dicts/strings built above (not the
        # SDK's own narrow param TypedDicts/Literals), so this can't
        # statically match the .create() overload -- same imprecision
        # llamacpp/_llama.py accepts for its own SDK call.
        response = await self.__client.chat.completions.create(  # type: ignore[call-overload]
            model=model,
            messages=oai_messages,
            tools=oai_tools if oai_tools else openai.NOT_GIVEN,
            reasoning_effort=_reasoning_effort_for(model),
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
                    # Gemini 3.x thinking models attach a per-call signature
                    # to the tool call (`extra_content.google.thought_signature`
                    # -- not part of the OpenAI Chat Completions spec, but the
                    # openai SDK's response models allow arbitrary extra
                    # fields, so it survives parsing and is reachable via
                    # plain attribute access). It is required verbatim on any
                    # later request that replays this call, or Gemini rejects
                    # the whole request with HTTP 400 -- see _convert.py's
                    # replay side and _turns.py's persistence of it onto the
                    # tool_use block. Not every fragment carries it, so this
                    # keeps whichever one did.
                    extra_content = getattr(tc, "extra_content", None)
                    if isinstance(extra_content, dict):
                        google_extra = extra_content.get("google")
                        if isinstance(google_extra, dict):
                            signature = google_extra.get("thought_signature")
                            if isinstance(signature, str):
                                tool_signatures[idx] = signature
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
                thought_signature=tool_signatures.get(idx),
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
