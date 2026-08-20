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
from kodo.toolspecs import tool_description

from ._cache import build_message_params, build_system_blocks
from ._retry import UnrecoverableError, with_retry_iter

__all__ = ["ClaudePlugin", "UnrecoverableError"]

_log = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8192

# Models on the newer "adaptive thinking" tier manage their own reasoning
# budget and reject the classic thinking={"type": "enabled", "budget_tokens"}
# shape with a 400. Earlier-generation models still require that shape —
# they have no adaptive mode.
_ADAPTIVE_THINKING_MODELS = frozenset(
    {
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)

# ----------------------------------------------------------------------
# Thinking tiers (kodo.llms._cloud_thinking's "anthropic" family)
# ----------------------------------------------------------------------
# The session's thinking_level (doc/SESSIONS.md) reaches this plugin as one of
# the family's five tier slugs, and lands in *one of two* request shapes
# depending on which mode the model is on -- the split above. Both are driven
# by the same tier, so "Thinking: High" means the same intent on every Claude
# model even though the wire shape differs.
#
# Adaptive models take it as `output_config.effort`
# (https://platform.claude.com/docs/en/build-with-claude/effort), whose five
# levels the tier slugs match 1:1 -- no translation table. `high` is both the
# family default and the API's own default ("setting effort to high produces
# exactly the same behavior as omitting the parameter entirely"), so an
# untouched session sends what this plugin effectively sent before the control
# existed.
_DEFAULT_EFFORT = "high"

# `xhigh` is the one level not offered by every effort-capable model (the
# 4.6 generation has `max` but no `xhigh`), so a model listed here gets it
# clamped to `high` rather than 400ing. Empty in practice today -- every
# model currently in _ADAPTIVE_THINKING_MODELS accepts all five levels -- but
# the clamp is the guard rail for the next 4.6-shaped model that joins that
# set (e.g. if claude-sonnet-4-6/claude-opus-4-6 are migrated off manual
# budgets, which their generation supports).
_NO_XHIGH_EFFORT_MODELS: frozenset[str] = frozenset()

# Manual ("extended thinking") models have no effort parameter at all, so the
# same tier is translated into a `budget_tokens` value instead
# (https://platform.claude.com/docs/en/build-with-claude/extended-thinking).
# `medium` deliberately reproduces the fixed 4096 budget this plugin used for
# every manual-mode request before the control existed. The API floor is 1024
# tokens; the budget is a target, not a hard cap (max_tokens is the real
# ceiling).
_THINKING_BUDGET_TOKENS: dict[str, int] = {
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}
_DEFAULT_THINKING_BUDGET_TOKENS = _THINKING_BUDGET_TOKENS["high"]

# budget_tokens must be < max_tokens, and the difference is all the room the
# visible answer has. 4096 is exactly the headroom _DEFAULT_MAX_TOKENS left
# over the old fixed 4096-token budget, so the low/medium tiers still send
# max_tokens=8192 unchanged; only the tiers that outgrow it raise the ceiling.
_RESPONSE_TOKEN_HEADROOM = 4096

# Adaptive models get no budget to size against, but the deeper tiers still
# need room to land: Anthropic's own guidance is to raise max_tokens when
# running at xhigh/max effort, since it is a hard limit on thinking *plus*
# response text and a truncated turn trips kodo's watchdog
# (runtime/_engine/_watchdog.py reads the "max_tokens" stop reason). Tiers up
# to and including the `high` default keep _DEFAULT_MAX_TOKENS, so nothing
# changes for a session that never touches the control.
_ADAPTIVE_MAX_TOKENS: dict[str, int] = {
    "xhigh": 16384,
    "max": 32768,
}


def _resolve_tier(thinking_level: str | None) -> str:
    """The tier slug to use for this request — *thinking_level* or the default.

    Args:
        thinking_level (str | None): Caller-supplied tier, or ``None`` when the
            caller has none (e.g. a session whose thinking level was never
            resolved). Anything outside the family's tier set falls back to the
            default rather than being forwarded to the API.

    Returns:
        str: One of :data:`_THINKING_BUDGET_TOKENS`' keys.
    """
    if thinking_level in _THINKING_BUDGET_TOKENS:
        return str(thinking_level)
    return _DEFAULT_EFFORT


def _thinking_param(model: str, tier: str) -> dict[str, object]:
    """Return the right ``thinking`` request shape for *model* at *tier*."""
    if model in _ADAPTIVE_THINKING_MODELS:
        return {"type": "adaptive"}
    return {
        "type": "enabled",
        "budget_tokens": _THINKING_BUDGET_TOKENS.get(tier, _DEFAULT_THINKING_BUDGET_TOKENS),
    }


def _effort_param(model: str, tier: str) -> dict[str, object]:
    """``{"output_config": {...}}`` for an adaptive model, ``{}`` otherwise.

    Manual-mode models express the tier through ``budget_tokens`` instead
    (:func:`_thinking_param`); sending an effort alongside it would be a second
    control over the same intent, and the two oldest registered models don't
    accept the parameter at all.
    """
    if model not in _ADAPTIVE_THINKING_MODELS:
        return {}
    effort = "high" if tier == "xhigh" and model in _NO_XHIGH_EFFORT_MODELS else tier
    return {"output_config": {"effort": effort}}


def _max_tokens_for(model: str, tier: str) -> int:
    """Output ceiling for this request — see :data:`_RESPONSE_TOKEN_HEADROOM`."""
    if model in _ADAPTIVE_THINKING_MODELS:
        return _ADAPTIVE_MAX_TOKENS.get(tier, _DEFAULT_MAX_TOKENS)
    budget = _THINKING_BUDGET_TOKENS.get(tier, _DEFAULT_THINKING_BUDGET_TOKENS)
    return max(_DEFAULT_MAX_TOKENS, budget + _RESPONSE_TOKEN_HEADROOM)


class ClaudePlugin(LLMPlugin):
    """Anthropic Claude implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    Uses the official ``anthropic`` Python SDK with prompt caching,
    exponential-backoff retries (FR-LLM-05), and cancellation support
    (FR-LLM-07).

    Reasoning depth is **session-controlled**, not fixed per model: the
    engine passes the session's ``thinking_level``
    (:data:`kodo.llms._cloud_thinking.CLOUD_THINKING_FAMILIES`'s
    ``"anthropic"`` family) on every call, and this plugin translates that one
    tier into whichever request shape the target model speaks — adaptive
    models take ``output_config.effort``, extended-thinking-only models take
    ``thinking.budget_tokens`` — sizing ``max_tokens`` to match. See the
    module-level tier tables.
    """

    __client: anthropic.AsyncAnthropic
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str) -> None:
        """Initialise with an Anthropic API key.

        Args:
            api_key (str): Anthropic API key (not written to disk per NFR-06).
        """
        # max_retries=0: kodo.llms._provider_retry/_gateway own retry/backoff.
        self.__client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supported_models(self) -> list[str]:
        return [
            "claude-fable-5",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-5",
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
        thinking_level: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a Claude response with prompt caching and retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): Claude model identifier.
            system (str): System prompt text.
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Message indices to cache.
            thinking_level (str | None): The session's reasoning tier for the
                ``"anthropic"`` thinking family (``"low"``/``"medium"``/
                ``"high"``/``"xhigh"``/``"max"``), or ``None`` for the family
                default. Applied as ``output_config.effort`` on an adaptive
                model and as ``thinking.budget_tokens`` on a manual one — see
                :func:`_thinking_param`/:func:`_effort_param`.

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
        system_blocks = build_system_blocks(system)
        msg_params = build_message_params(messages, cache_breakpoints)

        tool_defs: list[dict[str, object]] = [
            {
                "name": t.name,
                # The spec's prose plus its dense output-schema sketch: the API
                # tool definition has no output-schema field, so the result shape
                # only reaches the model through the description.
                "description": tool_description(t),
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        current_tool_use_id: str | None = None
        current_tool_name: str | None = None
        current_tool_input_parts: list[str] = []

        tier = _resolve_tier(thinking_level)
        async with self.__client.messages.stream(
            model=model,
            max_tokens=_max_tokens_for(model, tier),
            system=system_blocks,  # type: ignore[arg-type]
            messages=msg_params,  # type: ignore[arg-type]
            thinking=_thinking_param(model, tier),  # type: ignore[arg-type]
            **_effort_param(model, tier),  # type: ignore[arg-type]
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
