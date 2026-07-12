"""LLM plumbing shared by every engine concern.

Plugin/model resolution (fresh settings each dispatch), the per-session LLM
request logs, and the two silent (never-streamed-to-feed) call shapes: the
``return_result`` turn used by the titler / compactor / web summarizer, and
the security layer's SMART-mode intent judge.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from kodo.common import ApiKey
from kodo.llms import (
    LLMPlugin,
    LLMRouting,
    LoggingLLMPlugin,
    Message,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    TurnEnd,
    default_cache_breakpoints,
    get_cloud_registry,
    get_cloud_vendor_module,
    get_local_registry,
    local_thinking_default_tier,
    local_thinking_tiers,
)
from kodo.llms.anthropic import ClaudePlugin
from kodo.llms.llamacpp import LlamaPlugin
from kodo.project import kodo_user_dir
from kodo.subagents import SubAgent
from kodo.tools import ToolDispatcher, tools_for_agent

from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME, _JUDGE_AGENT_NAME, _PROBLEM_SOLVER_AGENT_NAME


def _find_cloud_vendor_for_model_id(model_id: str) -> str | None:
    """Return the vendor key whose hardcoded registry contains *model_id*."""
    for vendor, models in get_cloud_registry().items():
        if any(m.model_id == model_id for m in models):
            return vendor
    return None


class LLMPlumbingMixin:
    """Plugin resolution — per-dispatch, reads fresh settings each time."""

    # Declared so the `= None` writes below don't let mypy infer a bare-None
    # class attribute that conflicts with the EngineHost/_core declaration.
    _current_vendor: str | None
    # Same reason: _sync_thinking_level_to_model/handle_thinking_level_set's
    # `str` writes would otherwise make mypy infer a bare `str` here,
    # conflicting with the `str | None` declared on EngineHost/_core.
    _last_thinking_base_llm: str | None

    def _resolve_model_key(self: EngineHost, capability: str) -> str:
        """Resolve the registry model key for *capability* from current settings.

        Pure settings lookup (no plugin construction, no key request), so it is
        safe to call synchronously from the context-limit/auto-compaction paths.
        In ``local`` mode every capability maps to the single selected local
        model; otherwise the per-vendor, per-capability cloud model is used,
        falling back through the other capability tiers (``medium`` first,
        since that's the safest default) and finally to the vendor's first
        hardcoded registry model. A bare capability name (``"high"``, etc.)
        is never returned as a model key — an incomplete or stale
        ``models.cloud.<vendor>`` map (e.g. one predating a newly added
        tier) would otherwise 404 against the provider.

        Args:
            capability: ``'max'``, ``'high'``, ``'medium'``, or ``'low'``.

        Returns:
            str: A local registry name (local mode) or a cloud ``model_id``
                (cloud mode).
        """
        settings = self._get_settings()
        mode = str(settings.get("mode", "cloud"))
        models_map = settings.get("models", {})
        if not isinstance(models_map, dict):
            models_map = {}
        if mode == "local":
            return str(models_map.get("local", "llamacpp-qwen36-27b-q4-k-xl"))

        vendor = str(settings.get("active_cloud_vendor", "anthropic"))
        cloud_map = models_map.get("cloud", {})
        vendor_map = cloud_map.get(vendor, {}) if isinstance(cloud_map, dict) else {}
        if not isinstance(vendor_map, dict):
            vendor_map = {}
        for key in (capability, "medium", "high", "max", "low"):
            model_id = vendor_map.get(key)
            if model_id:
                return str(model_id)
        registry_models = get_cloud_registry().get(vendor, ())
        if registry_models:
            return registry_models[0].model_id
        return capability

    async def _resolve_plugin(
        self: EngineHost, capability: str, force_model_key: str | None = None
    ) -> tuple[LLMPlugin, str, LLMRouting]:
        """Resolve an LLM plugin + gateway routing for *capability*.

        Reads fresh settings each call. The returned :class:`LLMRouting` tells
        the shared :class:`LLMGateway` which feed to schedule the request on
        (local serial gate, or a per-vendor cloud feed). The API key (cloud) is
        resolved here, per session — the gateway never touches keys.

        Residence (local vs. cloud) is determined by registry membership of
        the resolved key, not by the *current* ``mode`` setting — this matters
        for ``force_model_key``, used so a model-switch compaction runs on the
        *previous* model even if ``mode`` itself changed since that model was
        selected (see ``ContextCompactor.handle_config_changed``).

        Args:
            capability: ``'max'``, ``'high'``, ``'medium'``, or ``'low'``.
            force_model_key: When set, use this exact key instead of resolving
                from settings.

        Returns:
            tuple[LLMPlugin, str, LLMRouting]: ``(plugin, model_id, routing)``.

        Raises:
            RuntimeError: If the client rejects or cancels the key request, or
                the resolved cloud vendor has no registered plugin.
        """
        settings = self._get_settings()
        model_key = force_model_key or self._resolve_model_key(capability)

        kodo_dir = kodo_user_dir()
        if model_key in get_local_registry(kodo_dir):
            self._current_vendor = None
            plugin: LLMPlugin = LlamaPlugin(sink=self._sink, kodo_dir=kodo_dir)
            routing = LLMRouting(residence="local")
            return LoggingLLMPlugin(plugin, self._llm_logs_dir()), model_key, routing

        vendor = _find_cloud_vendor_for_model_id(model_key) or str(
            settings.get("active_cloud_vendor", "anthropic")
        )
        self._current_vendor = vendor

        module = get_cloud_vendor_module(vendor)
        if module != "kodo.llms.anthropic":
            raise RuntimeError(f"Unsupported cloud vendor: {vendor!r}")

        key_result: ApiKey = await self._key_provider.get_key(vendor)
        if key_result.error:
            raise RuntimeError(f"API key request rejected: {key_result.error}")

        plugin = ClaudePlugin(api_key=key_result.api_key)
        routing = LLMRouting(residence="cloud", vendor=vendor)
        return LoggingLLMPlugin(plugin, self._llm_logs_dir()), model_key, routing

    def _current_base_llm(self: EngineHost) -> str:
        """``base_llm`` of the session's currently active *local* model.

        Reads fresh settings each call (same as :meth:`_resolve_model_key`).
        ``""`` whenever the session is on a cloud model, or a local registry
        entry with no ``base_llm`` (``custom_hf``/``custom_file``/
        ``custom_server_url``) — i.e. whenever there is no thinking-tier
        mechanism to apply. Used to keep ``_session.thinking_level`` in sync
        with whatever model is actually selected (doc/SESSIONS.md).
        """
        model_key = self._resolve_model_key(self._entry_capability())
        entry = get_local_registry(kodo_user_dir()).get(model_key)
        return entry.base_llm if entry is not None else ""

    def _thinking_kwargs(self: EngineHost, routing: LLMRouting) -> dict[str, object]:
        """``{"thinking_level": ...}`` to splice into a local ``stream_query`` call.

        Applies the session's ``thinking_level`` to every LLM call this
        session makes on its active *local* model — not just the main turn,
        but every silent call too (compaction, the security judge,
        ``web_search``'s tool loop) — since it is a session-wide setting, not
        a per-call one (doc/SESSIONS.md). ``{}`` for a cloud call (no
        thinking-tier mechanism; ``ClaudePlugin.stream_query`` has no such
        parameter) or when the active local model has no thinking family
        (``_session.thinking_level`` is already ``""`` in that case).
        """
        if routing.residence != "local" or not self._session.thinking_level:
            return {}
        return {"thinking_level": self._session.thinking_level}

    def _thinking_level_for_model(self: EngineHost, base_llm: str, prefer: str | None) -> str:
        """*prefer* if it is a valid thinking-tier value for *base_llm*, else the family default.

        Shared reconciliation used whenever ``_session.thinking_level`` needs
        a value for a (possibly new) *base_llm*: a brand-new session prefers
        an explicit seed (``WorkflowEngine.start``'s ``thinking_level``
        argument, used by the validator's RVP judge — WS_PROTOCOL.md §4.1),
        a resumed session prefers its persisted value, and both fall back to
        *base_llm*'s family default when *prefer* is ``None`` or no longer
        valid for it (e.g. the active model changed underneath a resumed
        session while it was closed).
        """
        tiers = local_thinking_tiers(base_llm)
        if prefer is not None and (prefer in tiers if tiers else prefer == ""):
            return prefer
        return local_thinking_default_tier(base_llm)

    async def _sync_thinking_level_to_model(self: EngineHost) -> None:
        """Re-derive ``thinking_level`` when the active model's identity changed.

        Fired alongside :meth:`ContextCompactor.handle_config_changed` for
        every live session on ``config.reload`` (i.e. whenever *any* window
        switches the shared local/cloud model selection — there is one
        active model, not one per session). Detects a switch the same way
        the compactor does, but reacts to thinking-family identity rather
        than context window: since the tier set is model-dependent, a switch
        to a different base model always resets to the new model's family
        default (``""`` for a non-thinking/cloud model) rather than trying to
        carry over a value that may not even be a valid tier for it.
        """
        base_llm = self._current_base_llm()
        if base_llm == self._last_thinking_base_llm:
            return
        self._last_thinking_base_llm = base_llm
        self._session.thinking_level = self._thinking_level_for_model(base_llm, prefer=None)
        self._transient.update(thinking_level=self._session.thinking_level)
        await self._emitters.emit_state()

    async def handle_thinking_level_set(self: EngineHost, value: str) -> bool:
        """Set the session's thinking-tier level, client-requested.

        Unlike ``edit_control``/``command_control`` (a fixed 3-way enum the
        engine always mirrors, coercing anything unrecognised to a safe
        default) the valid value set here depends on the session's currently
        active local model, so an invalid request is rejected outright rather
        than silently coerced — the client already knows the valid tier set
        (the server's ``thinking_families`` payload, doc/LLM_REGISTRY.md
        §4.5) and only ever sends a value from it, so rejection here is a
        safety net against a stale/racing client, not the normal path.

        Args:
            value: A tier slug valid for the active model's thinking family,
                or ``""`` if it has none.

        Returns:
            bool: ``True`` if applied, ``False`` if rejected.
        """
        base_llm = self._current_base_llm()
        tiers = local_thinking_tiers(base_llm)
        if tiers:
            if value not in tiers:
                return False
        elif value != "":
            return False
        self._session.thinking_level = value
        self._last_thinking_base_llm = base_llm
        self._transient.update(thinking_level=value)
        await self._emitters.emit_state()
        return True

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
        if self._session.workflow_mode == "judge":
            return _JUDGE_AGENT_NAME
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

        Grants the agent its tools (for ``compactor`` that is just
        ``return_result``) and captures the ``return_result`` payload, plus
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
            cache_breakpoints=default_cache_breakpoints(messages),
            **self._thinking_kwargs(routing),
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

    async def _run_silent_tool_loop_turn(
        self: EngineHost,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
        dispatcher: ToolDispatcher,
        deadline: float,
        max_rounds: int = 60,
        on_round_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, object] | None:
        """Drive a silent, multi-round tool-calling turn for an engine-driven agent.

        Unlike :meth:`_run_silent_return_turn` (one call, no dispatch — used by
        ``compactor``), this actually dispatches the agent's
        tool calls and loops until it returns a result. Unlike
        :meth:`_drive_subsession` (a real subsession: feed events, markers, a
        genuine subsession slot), nothing here reaches the feed and no
        subsession is opened. ``web_search`` needs the former's tool loop and
        the latter's invisibility: it is typically called *from* a sub-agent
        (the investigator), and subsessions cannot nest.

        Bounded two ways: *deadline* (a wall-clock unix timestamp — once
        reached, the agent gets one final forced turn to call
        ``return_result`` with whatever it has) and *max_rounds* (a hard
        safety valve independent of the clock, in case of a runaway loop).
        Cost is folded into the session total exactly like
        :meth:`_run_silent_return_turn`; no stream/thinking/tool-call events
        reach the feed, and tool dispatch skips the checkpoint/tool-call-card
        machinery :meth:`_dispatch_tool_calls` uses for a visible turn — the
        caller's *dispatcher* still runs every call through the real security
        gate and :class:`~kodo.tools.Tool` handler, just without the UI side
        effects.

        Args:
            dispatcher: Pre-built for the ``web_search`` agent, with
                ``ToolContext.deadline`` already set to *deadline*.
            deadline: Unix timestamp this run must wrap up by.
            max_rounds: Hard cap on LLM round-trips, independent of *deadline*.
            on_round_text: Called with a round's assistant text, whenever the
                round produced any (whether or not it also made tool calls),
                right before that round's tool calls (if any) are dispatched.
                Lets a caller surface the agent's own narration of its
                actions/decisions (doc/WEB_SEARCH.md §6) without this method
                knowing anything about feed events or persistence — the
                caller (``_run_web_search_agent``) owns both. ``None`` skips
                narration entirely (this loop shape has no other consumer today).

        Returns:
            dict[str, object] | None: The sub-agent's ``return_result``
            payload (already validated/normalized against its output schema
            by the ``return_result`` tool), or ``None`` if it never produced
            one even after the final forced turn.
        """
        tools = tools_for_agent(agent.tools)

        for _round in range(max_rounds):
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            thinking_signature: str | None = None
            tool_calls: list[ToolCallEvent] = []
            turn_end: TurnEnd | None = None

            async for event in self._gateway.stream_query(
                routing=routing,
                plugin=plugin,
                sink=self._sink,
                stream_id=uuid.uuid4().hex,
                model=model_id,
                system=agent.system_prompt,
                messages=messages,
                tools=tools,
                cache_breakpoints=default_cache_breakpoints(messages),
                **self._thinking_kwargs(routing),
            ):
                if isinstance(event, TokenDelta):
                    text_parts.append(event.text)
                elif isinstance(event, ThinkingDelta):
                    thinking_parts.append(event.text)
                elif isinstance(event, ThinkingSignature):
                    thinking_signature = event.signature
                elif isinstance(event, ToolCallEvent):
                    tool_calls.append(event)
                elif isinstance(event, TurnEnd):
                    turn_end = event

            if turn_end is not None:
                self._emitters.add_cost(turn_end.usage.usd_cost)
                await self._emitters.emit_cost_only()

            thinking_text = "".join(thinking_parts)
            assistant_content: list[dict[str, object]] = []
            if thinking_text:
                assistant_content.append(self._thinking_block(thinking_text, thinking_signature))

            if not tool_calls:
                # No tool call this round — nudge the model rather than
                # silently ending the loop on a stray text-only reply.
                round_text = "".join(text_parts)
                if on_round_text is not None and round_text:
                    await on_round_text(round_text)
                assistant_content.append({"type": "text", "text": round_text or "(no text)"})
                messages = messages + [Message(role="assistant", content=assistant_content)]
                messages = messages + [
                    Message(
                        role="user",
                        content=(
                            "Continue the search, or call return_result if you already "
                            "have enough to produce the report."
                        ),
                    )
                ]
                if time.time() >= deadline:
                    break
                continue

            if text_parts:
                round_text = "".join(text_parts)
                if on_round_text is not None and round_text:
                    await on_round_text(round_text)
                assistant_content.append({"type": "text", "text": round_text})
            for tc in tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tc.tool_use_id,
                        "name": tc.tool_name,
                        "input": tc.tool_input,
                    }
                )
            messages = messages + [Message(role="assistant", content=assistant_content)]

            tool_results: list[dict[str, object]] = []
            for tc in tool_calls:
                result_text = await dispatcher.dispatch(tc.tool_name, tc.tool_input, tc.tool_use_id)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tc.tool_use_id, "content": result_text}
                )
            messages = messages + [Message(role="user", content=tool_results)]

            if dispatcher.stop_requested:
                return dispatcher.returned_output
            if time.time() >= deadline:
                break

        if dispatcher.stop_requested:
            return dispatcher.returned_output

        # Time or rounds ran out without a result — one last forced turn:
        # tell the model plainly, and dispatch only a return_result call if
        # it makes one (this is the final word either way).
        messages = messages + [
            Message(
                role="user",
                content=(
                    "Time is up. Call return_result immediately with your best report "
                    "from what you have gathered so far — do not call any other tool."
                ),
            )
        ]
        final_calls: list[ToolCallEvent] = []
        final_turn_end: TurnEnd | None = None
        async for event in self._gateway.stream_query(
            routing=routing,
            plugin=plugin,
            sink=self._sink,
            stream_id=uuid.uuid4().hex,
            model=model_id,
            system=agent.system_prompt,
            messages=messages,
            tools=tools,
            cache_breakpoints=[0],
            **self._thinking_kwargs(routing),
        ):
            if isinstance(event, ToolCallEvent):
                final_calls.append(event)
            elif isinstance(event, TurnEnd):
                final_turn_end = event
        if final_turn_end is not None:
            self._emitters.add_cost(final_turn_end.usage.usd_cost)
            await self._emitters.emit_cost_only()
        for tc in final_calls:
            if tc.tool_name == "return_result":
                await dispatcher.dispatch(tc.tool_name, tc.tool_input, tc.tool_use_id)
                break
        return dispatcher.returned_output

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
                cache_breakpoints=default_cache_breakpoints([Message(role="user", content=user)]),
                **self._thinking_kwargs(routing),
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
