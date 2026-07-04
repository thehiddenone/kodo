"""LLM plumbing shared by every engine concern.

Plugin/model resolution (fresh settings each dispatch), the per-session LLM
request logs, and the two silent (never-streamed-to-feed) call shapes: the
``return_result`` turn used by the titler / compactor / web summarizer, and
the security layer's SMART-mode intent judge.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from kodo.common import ApiKey
from kodo.llms import (
    LLMPlugin,
    LLMRouting,
    LoggingLLMPlugin,
    Message,
    TokenDelta,
    ToolCallEvent,
    TurnEnd,
    get_llm_registry,
)
from kodo.llms.anthropic import ClaudePlugin
from kodo.llms.llamacpp import LlamaPlugin
from kodo.project import kodo_user_dir
from kodo.subagents import SubAgent
from kodo.tools import tools_for_agent

from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME, _PROBLEM_SOLVER_AGENT_NAME


class LLMPlumbingMixin:
    """Plugin resolution — per-dispatch, reads fresh settings each time."""

    # Declared so the `= None` writes below don't let mypy infer a bare-None
    # class attribute that conflicts with the EngineHost/_core declaration.
    _current_vendor: str | None

    def _resolve_model_key(self: EngineHost, capability: str) -> str:
        """Resolve the registry model key for *capability* from current settings.

        Pure settings lookup (no plugin construction, no key request), so it is
        safe to call synchronously from the context-limit/auto-compaction paths.
        In ``local`` mode every capability maps to the single selected local
        model; otherwise the per-capability cloud model is used (falling back to
        the ``medium`` entry, then the capability name itself).

        Args:
            capability: ``'high'``, ``'medium'``, or ``'low'``.

        Returns:
            str: The registry key (e.g. ``'claude-opus-4-8'``).
        """
        settings = self._get_settings()
        mode = str(settings.get("mode", "cloud"))
        models_map = settings.get("models", {})
        if not isinstance(models_map, dict):
            models_map = {}
        if mode == "local":
            return str(models_map.get("local", "llamacpp-qwen36-27b"))
        return str(models_map.get(capability, models_map.get("medium", capability)))

    async def _resolve_plugin(
        self: EngineHost, capability: str, force_model_key: str | None = None
    ) -> tuple[LLMPlugin, str, LLMRouting]:
        """Resolve an LLM plugin + gateway routing for *capability*.

        Reads fresh settings each call.  The returned :class:`LLMRouting` tells
        the shared :class:`LLMGateway` which feed to schedule the request on
        (local serial gate, or a per-vendor cloud feed).  The API key (cloud) is
        resolved here, per session — the gateway never touches keys.

        Args:
            capability: ``'high'``, ``'medium'``, or ``'low'``.
            force_model_key: When set, use this exact registry key instead of
                resolving from settings — used so a model-switch compaction runs
                on the *previous* model rather than the just-selected one.

        Returns:
            tuple[LLMPlugin, str, LLMRouting]: ``(plugin, model_id, routing)``.

        Raises:
            RuntimeError: If the client rejects or cancels the key request.
        """
        model_key = force_model_key or self._resolve_model_key(capability)

        registry = get_llm_registry()
        entry = registry.get(model_key)
        module = entry.module if entry is not None else "kodo.llms.anthropic"

        if module == "kodo.llms.llamacpp":
            self._current_vendor = None
            plugin: LLMPlugin = LlamaPlugin(sink=self._sink, kodo_dir=kodo_user_dir())
            routing = LLMRouting(residence="local")
            return LoggingLLMPlugin(plugin, self._llm_logs_dir()), model_key, routing

        model_id = entry.model_id if entry is not None else model_key
        vendor = module.rsplit(".", 1)[-1]
        self._current_vendor = vendor

        key_result: ApiKey = await self._key_provider.get_key(vendor)
        if key_result.error:
            raise RuntimeError(f"API key request rejected: {key_result.error}")

        plugin = ClaudePlugin(api_key=key_result.api_key)
        routing = LLMRouting(residence="cloud", vendor=vendor)
        return LoggingLLMPlugin(plugin, self._llm_logs_dir()), model_id, routing

    def _llm_logs_dir(self: EngineHost) -> Path:
        """Per-session LLM request/response log dir (sessions never share one).

        ``~/.kodo/logs/llm_requests/<session_id>/`` — keeps concurrent sessions'
        logs isolated and makes the on-start clear scoped to this session only.
        """
        return self._workspace_layout.llm_requests_dir / (self._orch_session_id or "unbound")

    def _clear_llm_request_logs(self: EngineHost) -> None:
        """Remove this session's previously logged LLM requests/responses."""
        logs_dir = self._llm_logs_dir()
        if not logs_dir.is_dir():
            return
        for entry in logs_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def _entry_agent_name(self: EngineHost) -> str:
        """The top-level entry agent for the current workflow mode."""
        if self._session.workflow_mode == "problem_solving":
            return _PROBLEM_SOLVER_AGENT_NAME
        return _GUIDE_AGENT_NAME

    def _entry_capability(self: EngineHost) -> str:
        """Capability tier of the current entry agent (defaults to medium)."""
        try:
            return self._registry.get(self._entry_agent_name()).capability
        except Exception:  # noqa: BLE001 — unregistered agent → safe default
            return "medium"

    async def _run_silent_return_turn(
        self: EngineHost,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
    ) -> tuple[dict[str, object] | None, str]:
        """One silent (un-streamed-to-feed) LLM turn for an engine-driven agent.

        Grants the agent its tools (for ``compactor`` / ``session_titler`` that is
        just ``return_result``) and captures the ``return_result`` payload, plus
        the concatenated text as a fallback for a model that ignores the tool.
        Returns ``(result_or_None, text)``. The call's USD cost is folded into the
        running total; no stream/thinking events reach the feed.
        """
        text_parts: list[str] = []
        turn_end: TurnEnd | None = None
        result: dict[str, object] | None = None
        async for event in self._gateway.stream_query(
            routing=routing,
            plugin=plugin,
            sink=self._sink,
            stream_id=uuid.uuid4().hex,
            model=model_id,
            system=agent.system_prompt,
            messages=messages,
            tools=tools_for_agent(agent.tools),
            cache_breakpoints=[0],
        ):
            if isinstance(event, TokenDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallEvent):
                if event.tool_name == "return_result" and isinstance(event.tool_input, dict):
                    payload = event.tool_input.get("result")
                    if isinstance(payload, dict):
                        result = payload
            elif isinstance(event, TurnEnd):
                turn_end = event

        if turn_end is not None:
            self._emitters.add_cost(turn_end.usage.usd_cost)
            await self._emitters.emit_cost_only()

        return result, "".join(text_parts)

    async def _security_judge(self: EngineHost, system: str, user: str) -> str:
        """One silent LLM call for the security layer's SMART-mode intent judge.

        Runs on the session's active model (entry-agent capability), with no
        tools and no feed events — only the text verdict is collected. The
        call's USD cost is folded into the running session total (cost-only
        ``usage.update``, no feed entry). Exceptions propagate: the security
        layer treats any failure as an ``ask`` (fail closed).

        Brackets the call with ``security.judging`` (true/false) so the client
        can show an "Evaluating…" indicator — this round streams nothing and
        can take several seconds to tens of seconds, which otherwise looks
        like an unexplained stall.
        """
        await self._emitters.emit_security_judging(True)
        try:
            plugin, model_id, routing = await self._resolve_plugin(self._entry_capability())
            text_parts: list[str] = []
            turn_end: TurnEnd | None = None
            async for event in self._gateway.stream_query(
                routing=routing,
                plugin=plugin,
                sink=self._sink,
                stream_id=uuid.uuid4().hex,
                model=model_id,
                system=system,
                messages=[Message(role="user", content=user)],
                tools=[],
                cache_breakpoints=[0],
            ):
                if isinstance(event, TokenDelta):
                    text_parts.append(event.text)
                elif isinstance(event, TurnEnd):
                    turn_end = event
            if turn_end is not None:
                self._emitters.add_cost(turn_end.usage.usd_cost)
                await self._emitters.emit_cost_only()
            return "".join(text_parts)
        finally:
            await self._emitters.emit_security_judging(False)
