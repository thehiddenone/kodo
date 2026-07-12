"""Transparent LLM request/response logger.

Wraps any LLMPlugin and writes a pair of pretty-printed JSON files for
every stream_query call:

    <log_dir>/{N:04d}_request.json
    <log_dir>/{N:04d}_response.json

N is a process-wide monotonically increasing counter shared across all
plugin instances.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from ._interface import (
    LLMPlugin,
    Message,
    StreamEvent,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
)

__all__ = ["LoggingLLMPlugin"]

_log = logging.getLogger(__name__)
_counter = itertools.count(1)


class LoggingLLMPlugin(LLMPlugin):
    """Decorator that logs every LLM request/response pair to disk."""

    def __init__(self, inner: LLMPlugin, log_dir: Path) -> None:
        self._inner = inner
        self._log_dir = log_dir

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def supported_models(self) -> list[str]:
        return self._inner.supported_models

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
        n = next(_counter)
        return self._logged_stream(
            n=n,
            stream_id=stream_id,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            cache_breakpoints=cache_breakpoints,
            thinking_level=thinking_level,
        )

    async def _logged_stream(
        self,
        *,
        n: int,
        stream_id: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        cache_breakpoints: list[int],
        thinking_level: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{n:04d}"

        request_dt = datetime.now(UTC)
        request_ts = request_dt.strftime("%Y-%m-%d %H:%M:%S")
        request_data: dict[str, object] = {
            "n": n,
            "request_timestamp": request_ts,
            "stream_id": stream_id,
            "model": model,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
            "cache_breakpoints": cache_breakpoints,
            "thinking_level": thinking_level,
        }
        request_path = self._log_dir / f"{prefix}_request.json"
        try:
            request_path.write_text(json.dumps(request_data, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write LLM request log %s: %s", request_path, exc)

        # Only forwarded when set: the base LLMPlugin interface (and
        # ClaudePlugin) has no such parameter at all, so passing it
        # unconditionally would TypeError for a cloud call — thinking_level is
        # non-None only when the caller already knows _inner is a LlamaPlugin
        # (see LLMPlumbingMixin._thinking_kwargs, local-only).
        extra: dict[str, object] = (
            {} if thinking_level is None else {"thinking_level": thinking_level}
        )
        events: list[dict[str, object]] = []
        try:
            async for event in self._inner.stream_query(
                stream_id=stream_id,
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                cache_breakpoints=cache_breakpoints,
                **extra,
            ):
                events.append(_event_to_dict(event))
                yield event
        finally:
            response_path = self._log_dir / f"{prefix}_response.json"
            response_dt = datetime.now(UTC)
            response_data: dict[str, object] = {
                "n": n,
                "request_timestamp": request_ts,
                "response_timestamp": response_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "duration": (response_dt - request_dt).total_seconds(),
                "stream_id": stream_id,
                "model": model,
                "events": events,
            }
            try:
                response_path.write_text(json.dumps(response_data, indent=2), encoding="utf-8")
            except OSError as exc:
                _log.warning("Failed to write LLM response log %s: %s", response_path, exc)

    async def cancel(self, stream_id: str) -> None:
        await self._inner.cancel(stream_id)


def _event_to_dict(event: StreamEvent) -> dict[str, object]:
    if isinstance(event, ThinkingDelta):
        return {"type": "thinking_delta", "text": event.text}
    if isinstance(event, ThinkingSignature):
        return {"type": "thinking_signature", "signature": event.signature}
    if isinstance(event, TokenDelta):
        return {"type": "token_delta", "text": event.text}
    if isinstance(event, ToolCallEvent):
        return {
            "type": "tool_call",
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
        }
    if isinstance(event, TurnEnd):
        u = event.usage
        return {
            "type": "turn_end",
            "stop_reason": event.stop_reason,
            "usage": {
                "model": u.model,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_write_tokens": u.cache_write_tokens,
                "cache_read_tokens": u.cache_read_tokens,
                "usd_cost": u.usd_cost,
            },
        }
    return {"type": type(event).__name__}
