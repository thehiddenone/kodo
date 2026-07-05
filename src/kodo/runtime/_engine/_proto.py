"""The ``EngineHost`` protocol — the typed seam between the engine's mixins.

:class:`~._core.WorkflowEngine` is assembled from behaviour mixins (worker
loop, LLM plumbing, turn loop, sub-agent dispatch, crash resume) that share
one instance's state. Every mixin method annotates ``self: EngineHost`` (the
mypy-documented mixin pattern), so this protocol is the single, explicit map
of the state and cross-module methods the mixins may touch. If a mixin needs
something not listed here, add it here first — that keeps the coupling
visible instead of implicit.

The narrow, per-collaborator host protocols (:class:`~._compaction.CompactorHost`,
:class:`~._titling.TitlerHost`, :class:`~._checkpointing.CheckpointHost`) live
next to their collaborators; this one is only for the mixins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from kodo.common import ApiKeyProvider, MessageSink
from kodo.llms import (
    LLMGateway,
    LLMPlugin,
    LLMRouting,
    Message,
    ToolCallEvent,
    ToolCallLogger,
    ToolSpec,
)
from kodo.project import ProjectLayout, WorkspaceLayout
from kodo.security import SecurityLayer
from kodo.state import TransientStore
from kodo.subagents import AgentRegistry, SubAgent
from kodo.tools import PathResolver, RootPath, ToolDispatcher

from .._checkpoints import CheckpointRef
from .._gates import GateOrchestrator
from .._session import SessionState
from ._checkpointing import CheckpointCoordinator
from ._compaction import ContextCompactor
from ._events import EngineEmitters
from ._services import _EngineServices
from ._shared import _GUIDE_AGENT_NAME
from ._titling import SessionTitler


class EngineHost(Protocol):
    """Shared state + cross-module methods available to every engine mixin."""

    # -- injected collaborators / infrastructure ------------------------------
    _sink: MessageSink
    _gate: GateOrchestrator
    _key_provider: ApiKeyProvider
    _get_settings: Callable[[], dict[str, object]]
    _transient: TransientStore
    _workspace_layout: WorkspaceLayout
    _gateway: LLMGateway
    _registry: AgentRegistry
    _security: SecurityLayer
    _services: _EngineServices
    _emitters: EngineEmitters
    _compactor: ContextCompactor
    _titler: SessionTitler
    _checkpoints: CheckpointCoordinator

    # -- mutable session state -------------------------------------------------
    _layout: ProjectLayout | None
    _current_project: dict[str, str] | None
    _queue: asyncio.Queue[dict[str, object]]
    _session: SessionState
    _main_messages: list[Message]
    _orch_session_id: str
    _current_vendor: str | None
    _replay_subsessions: list[dict[str, object]] | None
    _resume_subsession_pending: bool

    # -- core helpers (defined in _core) ---------------------------------------
    def _agent_available(self, name: str) -> bool: ...

    def _require_layout(self) -> ProjectLayout: ...

    def _freeze_effective_modes(self) -> None: ...

    async def _finalize_document(self, path: str) -> None: ...

    def _make_resolver(self) -> PathResolver: ...

    def _root_paths(self) -> tuple[RootPath, ...]: ...

    @staticmethod
    def _util_paths() -> dict[str, Path]: ...

    # -- LLM plumbing (defined in _llm) -----------------------------------------
    def _resolve_model_key(self, capability: str) -> str: ...

    async def _resolve_plugin(
        self, capability: str, force_model_key: str | None = None
    ) -> tuple[LLMPlugin, str, LLMRouting]: ...

    def _llm_logs_dir(self) -> Path: ...

    async def _run_silent_return_turn(
        self,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
    ) -> tuple[dict[str, object] | None, str]: ...

    async def _run_silent_tool_loop_turn(
        self,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
        dispatcher: ToolDispatcher,
        deadline: float,
        max_rounds: int = 60,
        on_round_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, object] | None: ...

    def _entry_agent_name(self) -> str: ...

    def _entry_capability(self) -> str: ...

    # -- worker (defined in _worker) --------------------------------------------
    async def _handle_input_no_agent(self, name: str, text: str) -> None: ...

    # -- turn loop (defined in _turns) -------------------------------------------
    async def _run_guide_with_input(
        self, text: str, attachments: list[str] | None = None
    ) -> None: ...

    async def _run_problem_solver_with_input(
        self, text: str, attachments: list[str] | None = None
    ) -> None: ...

    async def _run_entry_agent(
        self, agent_name: str, text: str, attachments: list[str] | None = None
    ) -> None: ...

    async def _store_attachments(
        self, paths: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]: ...

    def _persist_main_messages(self, entry_agent: str) -> Callable[[list[Message]], None]: ...

    async def _run_agent_turn(
        self,
        llm: LLMPlugin,
        routing: LLMRouting,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_dispatch: Callable[[str, dict[str, object], str], Awaitable[str]],
        stream_id: str,
        agent_name: str = _GUIDE_AGENT_NAME,
        stop_after_tools: Callable[[], bool] | None = None,
        persist: Callable[[list[Message]], None] | None = None,
        flush_before_dispatch: bool = False,
        track_context: bool = False,
    ) -> tuple[list[Message], list[Path]]: ...

    @staticmethod
    def _thinking_block(thinking: str, signature: str | None) -> dict[str, object]: ...

    def _partial_assistant_message(
        self,
        text_parts: list[str],
        thinking_parts: list[str],
        thinking_signature: str | None,
        tool_calls: list[ToolCallEvent],
    ) -> Message | None: ...

    async def _dispatch_tool_calls(
        self,
        calls: list[tuple[str, str, dict[str, object]]],
        tool_dispatch: Callable[[str, dict[str, object], str], Awaitable[str]],
        tool_desc: dict[str, str],
        tool_logger: ToolCallLogger,
        agent_name: str,
    ) -> list[dict[str, object]]: ...

    async def _finalize_tool_result(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, object],
        result_text: str,
        checkpoint: CheckpointRef | None = None,
        agent_name: str = _GUIDE_AGENT_NAME,
    ) -> str: ...

    def _make_dispatcher(
        self, agent_name: str, session_id: str, deadline: float | None = None
    ) -> ToolDispatcher: ...

    # -- sub-agent dispatch (defined in _subagents) --------------------------------
    def _assert_can_spawn(self, caller: str, *names: str) -> None: ...

    async def _spawn_subagent(
        self, name: str, task_input: dict[str, object]
    ) -> dict[str, object]: ...

    async def _drive_subsession(
        self, name: str, subsession_id: str, messages: list[Message]
    ) -> dict[str, object]: ...

    async def _open_subsession(
        self, name: str, subsession_id: str, task_content: str = ""
    ) -> None: ...

    async def _close_subsession(
        self, name: str, subsession_id: str, output: dict[str, object]
    ) -> None: ...

    async def _replay_next_subsession(self, name: str) -> dict[str, object]: ...

    def _display_name(self, agent_name: str) -> str: ...

    @staticmethod
    def _render_task_input(task_input: dict[str, object]) -> str: ...

    # -- crash resume (defined in _resume) ------------------------------------------
    def _has_dangling_tool_use(self) -> bool: ...

    def _persist_interrupted_turn(self, entry_agent: str) -> None: ...

    def _last_entry_agent(self) -> str: ...

    async def _resume_main_turn(self) -> None: ...

    @staticmethod
    def _interrupted_tool_result(
        tool_use_id: str, tool_name: str, reason: str = "restart"
    ) -> dict[str, object]: ...

    def _build_replay_ledger(self) -> list[dict[str, object]]: ...
