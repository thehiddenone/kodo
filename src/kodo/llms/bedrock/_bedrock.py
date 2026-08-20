"""AWS Bedrock LLM plugin — streams via Bedrock's Converse API (boto3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import boto3
from botocore.config import Config

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

from ._convert import build_converse_messages, build_converse_tool_config, build_system_blocks
from ._credentials import parse_bedrock_credentials
from ._reasoning import max_tokens_for, reasoning_fields_for
from ._retry import with_retry_iter
from ._stream import aiter_converse_stream

__all__ = ["BedrockPlugin"]

_log = logging.getLogger(__name__)

# The service name of Bedrock's data plane. The control plane ("bedrock",
# used by kodo.llms._bedrock_catalog for ListFoundationModels) is a separate
# client with separate IAM actions -- see doc/LLM_REGISTRY.md §3b.
_RUNTIME_SERVICE = "bedrock-runtime"

# botocore's own retry layer must be off: kodo.llms._provider_retry and
# kodo.llms._gateway are the exclusive owners of retry/backoff policy, and an
# SDK retrying underneath them defeats both the (2, 8, 32)s backoff and the
# gateway's exponential 429 backoff (see _provider_retry's module docstring,
# doc/LLM_GATEWAY.md). Standard mode counts *total attempts* including the
# first, so 1 -- not 0 -- is the "no retries" value here; boto3's default is 3.
_NO_SDK_RETRIES = {"max_attempts": 1, "mode": "standard"}

# A reasoning model can go a long time between stream events, and botocore's
# read timeout applies per socket read on the event stream, not to the request
# as a whole -- the 60s default would abort a deep-thinking turn mid-flight.
_READ_TIMEOUT_SECONDS = 900
_CONNECT_TIMEOUT_SECONDS = 15

# Converse's own stop-reason vocabulary -> kodo's canonical one. Bedrock
# normalises across providers here, so unlike the Chat-Completions vendors
# there is no per-provider variation to absorb. "max_tokens" is the one value
# with real downstream behavior -- runtime/_engine/_watchdog.py's
# truncated-generation check reads it verbatim.
_STOP_REASONS: dict[str, str] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    "guardrail_intervened": "end_turn",
    "content_filtered": "end_turn",
    "request_cancelled": "end_turn",
}


def _map_stop_reason(reason: str | None) -> str:
    """Map a Converse ``stopReason`` onto kodo's canonical stop-reason vocabulary."""
    return _STOP_REASONS.get(reason or "", reason or "end_turn")


class BedrockPlugin(LLMPlugin):
    """Amazon Bedrock implementation of :class:`~kodo.llms._interface.LLMPlugin`.

    The second **aggregator** vendor here, after OpenRouter, and deliberately
    built to the same set of decisions (doc/LLM_REGISTRY.md §3b): no
    compiled-in model tuple, a catalog fetched at runtime
    (:mod:`kodo.llms._bedrock_catalog`), a session-level thinking tier instead
    of a per-model constant, and prompt-cache breakpoints ignored because
    support varies across the catalog. Three things are genuinely different,
    each forced by AWS rather than chosen:

    * **Credentials are a pair, not a string.** A long-term IAM user access
      key means an access key id *and* a secret access key, which kodo-vsix
      packs into one JSON blob to fit the existing one-secret-per-vendor pull
      protocol unchanged (:mod:`kodo.llms.bedrock._credentials`). The
      **region** is not part of it — it rides in settings as
      ``bedrock_region`` and arrives as this plugin's second constructor
      argument.
    * **The SDK is synchronous.** boto3 has no async client, so the blocking
      ``converse_stream`` call and its ``EventStream`` iteration run on a
      worker thread feeding an :class:`asyncio.Queue`
      (:mod:`kodo.llms.bedrock._stream`).
    * **There is no cost in the response, and no pricing table.** Bedrock
      reports token counts but never a price, and its 110+ models across 18
      providers are priced per-region through a separate AWS Price List API
      (a different service, a different IAM grant). So unlike OpenRouter --
      whose whole cost story is ``Usage.provider_reported_cost`` -- Bedrock
      leaves that ``None`` *and* has no ``compute_cost``: this vendor is
      absent from ``_CLOUD_VENDOR_MODEL_PREFIX``, so
      :func:`kodo.llms._pricing.compute_cost` returns ``0.0`` and the UI
      shows tokens without a dollar figure. Documented, not accidental.
    """

    __client: Any
    __region: str
    __cancel_events: dict[str, asyncio.Event]

    def __init__(self, api_key: str, region: str) -> None:
        """Initialise with an IAM access-key blob and an AWS region.

        Args:
            api_key (str): The JSON credential blob kodo-vsix returns for the
                ``bedrock`` vendor (not written to disk per NFR-06) — see
                :func:`~kodo.llms.bedrock._credentials.parse_bedrock_credentials`.
            region (str): AWS region to call Bedrock in, e.g. ``"us-east-1"``.
                Bedrock is regional: which models exist, and which inference
                profiles can serve them, both depend on this.

        Raises:
            InvalidCredentialsError: *api_key* is not a usable access-key pair.
        """
        credentials = parse_bedrock_credentials(api_key)
        self.__region = region
        self.__client = boto3.client(
            _RUNTIME_SERVICE,
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            region_name=region,
            config=Config(
                retries=_NO_SDK_RETRIES,
                read_timeout=_READ_TIMEOUT_SECONDS,
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self.__cancel_events = {}

    @property
    def name(self) -> str:
        return "bedrock"

    @property
    def supported_models(self) -> list[str]:
        # Like OpenRouter, Bedrock's real model list is fetched/cached
        # dynamically (kodo.llms._bedrock_catalog), not a compiled-in set --
        # and unlike OpenRouter there is no always-valid router pseudo-model
        # to name here, since every Bedrock call targets a concrete model or
        # inference profile. An empty list is the honest answer.
        return []

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
        """Stream a Bedrock response via the Converse API, with retry.

        Args:
            stream_id (str): Caller-supplied ID; pass to :meth:`cancel` to abort.
            model (str): A Bedrock model id (``"anthropic.claude-opus-5"``) or,
                more usually, a cross-region inference-profile id
                (``"us.anthropic.claude-opus-5"``) — many models cannot be
                invoked on demand any other way (doc/LLM_REGISTRY.md §3b).
            system (str): System prompt text (Converse's own ``system`` field).
            messages (list[Message]): Conversation history.
            tools (list[ToolSpec]): Tools the model may invoke.
            cache_breakpoints (list[int]): Accepted for interface parity;
                ignored — see :mod:`kodo.llms.bedrock._convert`.
            thinking_level (str | None): The session's ``bedrock_effort`` tier
                (``"low"``/``"medium"``/``"high"``/``"xhigh"``/``"max"``), or
                ``None``. Translated per model family — see
                :mod:`kodo.llms.bedrock._reasoning`.

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

    def __build_request(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        thinking_level: str | None,
    ) -> dict[str, object]:
        """Assemble the ``converse_stream`` keyword arguments.

        Every optional Converse field is omitted rather than sent empty:
        ``system=[]``, ``toolConfig={"tools": []}`` and an empty
        ``additionalModelRequestFields`` are all ``ValidationException``s, not
        no-ops.
        """
        request: dict[str, object] = {
            "modelId": model,
            "messages": build_converse_messages(messages),
        }
        system_blocks = build_system_blocks(system)
        if system_blocks:
            request["system"] = system_blocks
        tool_config = build_converse_tool_config(tools)
        if tool_config:
            request["toolConfig"] = tool_config
        reasoning = reasoning_fields_for(model, thinking_level)
        if reasoning:
            request["additionalModelRequestFields"] = reasoning
        max_tokens = max_tokens_for(model, thinking_level)
        if max_tokens is not None:
            request["inferenceConfig"] = {"maxTokens": max_tokens}
        return request

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
        # Bedrock's cachePoint support is per-model -- see _convert.py.
        del cache_breakpoints

        request = self.__build_request(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            thinking_level=thinking_level,
        )

        # Converse addresses content blocks by index and interleaves them, so
        # a single turn's tool calls are accumulated per index and flushed at
        # the end -- same shape as the Chat-Completions plugins' own
        # per-index tool accumulation.
        tool_ids: dict[int, str] = {}
        tool_names: dict[int, str] = {}
        tool_arg_parts: dict[int, list[str]] = {}
        stop_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0

        # `aclosing` is load-bearing, not tidiness: this generator is
        # abandoned mid-iteration on cancellation, and without a deterministic
        # close its teardown (which is what closes the HTTP stream, and so
        # what lets the reader thread exit) would wait on garbage collection.
        stream = aiter_converse_stream(self.__client, request, cancel_event)
        async with contextlib.aclosing(stream) as events:
            async for event in events:
                if cancel_event.is_set():
                    _log.debug("Stream cancelled by caller")
                    return

                start = event.get("contentBlockStart")
                if isinstance(start, dict):
                    index = _block_index(start)
                    tool_use = start.get("start", {})
                    tool_use = tool_use.get("toolUse") if isinstance(tool_use, dict) else None
                    if isinstance(tool_use, dict):
                        tool_ids[index] = str(tool_use.get("toolUseId", ""))
                        tool_names[index] = str(tool_use.get("name", ""))
                        # Announce the call as soon as its name is known, so the
                        # UI shows a live "generating" indicator while what can be
                        # a very large argument payload streams in.
                        yield ToolCallArgDelta(tool_name=tool_names[index], text="")

                delta_event = event.get("contentBlockDelta")
                if isinstance(delta_event, dict):
                    index = _block_index(delta_event)
                    delta = delta_event.get("delta")
                    delta = delta if isinstance(delta, dict) else {}

                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        yield TokenDelta(text=text)

                    reasoning_text = _reasoning_text(delta)
                    if reasoning_text:
                        yield ThinkingDelta(text=reasoning_text)

                    tool_use = delta.get("toolUse")
                    if isinstance(tool_use, dict):
                        fragment = tool_use.get("input")
                        if isinstance(fragment, str) and fragment:
                            tool_arg_parts.setdefault(index, []).append(fragment)
                            yield ToolCallArgDelta(
                                tool_name=tool_names.get(index, ""),
                                text=fragment,
                            )

                message_stop = event.get("messageStop")
                if isinstance(message_stop, dict):
                    stop_reason = str(message_stop.get("stopReason", "")) or None

                metadata = event.get("metadata")
                if isinstance(metadata, dict):
                    usage = metadata.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = _int_field(usage, "inputTokens")
                        output_tokens = _int_field(usage, "outputTokens")
                        cache_read_tokens = _int_field(usage, "cacheReadInputTokens")
                        cache_write_tokens = _int_field(usage, "cacheWriteInputTokens")

        if cancel_event.is_set():
            return

        for index in sorted(tool_ids):
            yield ToolCallEvent(
                tool_use_id=tool_ids[index],
                tool_name=tool_names.get(index, ""),
                tool_input=_parse_tool_input("".join(tool_arg_parts.get(index, []))),
            )

        yield TurnEnd(
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_read_tokens=cache_read_tokens,
                model=model,
                # Bedrock reports no cost -- see the class docstring.
                provider_reported_cost=None,
            ),
            stop_reason=_map_stop_reason(stop_reason),
        )

    @property
    def region(self) -> str:
        """The AWS region this plugin instance calls Bedrock in."""
        return self.__region


def _block_index(event: dict[str, Any]) -> int:
    """The ``contentBlockIndex`` of a Converse stream event, ``0`` if absent."""
    raw = event.get("contentBlockIndex")
    return raw if isinstance(raw, int) else 0


def _int_field(mapping: dict[str, Any], key: str) -> int:
    raw = mapping.get(key)
    return raw if isinstance(raw, int) else 0


def _reasoning_text(delta: dict[str, Any]) -> str:
    """Extract reasoning text from one ``contentBlockDelta``.

    Converse wraps reasoning in a tagged union: ``reasoningContent`` carries
    either a ``reasoningText`` (``text`` plus a provider ``signature``) or a
    ``redactedContent`` blob the provider encrypted for safety. Only the
    former has anything to show; the signature is read but not kept, since
    kodo drops persisted thinking blocks on replay rather than round-tripping
    them (see :mod:`kodo.llms.bedrock._convert`).
    """
    reasoning = delta.get("reasoningContent")
    if not isinstance(reasoning, dict):
        return ""
    # Streaming deltas put the text directly under reasoningContent; the
    # non-streaming shape nests it one level deeper under reasoningText.
    text = reasoning.get("text")
    if isinstance(text, str):
        return text
    nested = reasoning.get("reasoningText")
    if isinstance(nested, dict):
        nested_text = nested.get("text")
        if isinstance(nested_text, str):
            return nested_text
    return ""


def _parse_tool_input(raw_json: str) -> dict[str, object]:
    """Parse accumulated ``toolUse.input`` fragments into the tool's arguments.

    Converse streams tool arguments as JSON *text* fragments even though the
    non-streaming shape returns a real object, so this is the same
    accumulate-then-parse the Chat-Completions plugins do — including the
    ``{"_raw": ...}`` fallback, which keeps a malformed payload visible to the
    agent instead of turning it into an empty call.
    """
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"_raw": raw_json}
    return parsed if isinstance(parsed, dict) else {"_raw": raw_json}
