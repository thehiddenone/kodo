"""Kodo runtime engine — single async worker hosting the Orchestrator session.

The engine is a thin substrate.  It does not contain a stage machine, a
scheduler, or a workflow DAG.  Every decision about what runs when is the
Orchestrator's, encoded in its system prompt and carried out via the 8-tool
surface in :mod:`._tool_surface`.

Architecture (DESIGN.md §5):
- One ``asyncio.Queue`` + one worker coroutine (FR-WF-02).
- The worker drives the Orchestrator LLM: builds the turn, dispatches tool
  calls to :class:`._tool_surface.ToolSurface`, appends results, repeats until
  the model emits no more tool calls.
- User prompts (via ``prompt.submit``) are fed to the Orchestrator as new user
  messages between turns.
- Approval/question blocking happens inside ToolSurface handlers which
  ``await`` a :class:`asyncio.Future` resolved by the WS dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from kodo.common import ApiKey, ApiKeyProvider, Envelope, MessageSink
from kodo.llms import LLMPlugin, get_llm_registry
from kodo.llms._interface import (
    Message,
    StreamEvent,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
)
from kodo.llms.anthropic import ClaudePlugin, UnrecoverableError
from kodo.llms.llamacpp import LlamaPlugin
from kodo.mirror import CheckpointManager
from kodo.project import ProjectLayout
from kodo.state import TransientStore
from kodo.subagents import AgentRegistry
from kodo.subagents._loader import AgentLoadError
from kodo.transport import (
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_API_KEY_REVOKE,
    EVT_ERROR,
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_STATE,
    EVT_USAGE_UPDATE,
)
from kodo.workspace import ArtifactType, Workspace

from ._bootstrap import ProjectBootstrap
from ._gates import GateOrchestrator
from ._index import ProjectIndex
from ._rollback import Rollback
from ._session import SessionState
from ._subagent_dispatch import SubagentDispatcher, tools_for_agent
from ._tool_surface import (
    ORCHESTRATOR_TOOLS,
    ToolSurface,
)

__all__ = ["WorkflowEngine"]

_log = logging.getLogger(__name__)

_ORCHESTRATOR_AGENT_NAME = "orchestrator"
_DEFAULT_LLAMACPP_BASE_URL = "http://127.0.0.1:8080/v1"


class WorkflowEngine:
    """Single-worker runtime engine hosting the Orchestrator session.

    Args:
        sink: Message sink for sending events to the connected client.
        gate: Gate orchestrator for approval and question prompts.
        key_provider: Provider for cloud API keys.
        get_settings: Callable returning the current merged settings dict.
        transient: Append-only JSONL session store.
        layout: Project filesystem layout.
        registry: Loaded subagent file registry.
        mirror: Mirror checkpoint manager.
    """

    __sink: MessageSink
    __gate: GateOrchestrator
    __key_provider: ApiKeyProvider
    __get_settings: Callable[[], dict[str, object]]
    __transient: TransientStore
    __layout: ProjectLayout
    __registry: AgentRegistry
    __mirror: CheckpointManager
    __workspace: Workspace
    __queue: asyncio.Queue[dict[str, object]]
    __session: SessionState
    __index: ProjectIndex
    __tool_surface: ToolSurface
    __worker: asyncio.Task[None] | None
    __cumulative_usd: float
    __orch_messages: list[Message]
    __orch_session_id: str
    __current_vendor: str | None

    def __init__(
        self,
        sink: MessageSink,
        gate: GateOrchestrator,
        key_provider: ApiKeyProvider,
        get_settings: Callable[[], dict[str, object]],
        transient: TransientStore,
        layout: ProjectLayout,
        registry: AgentRegistry,
        mirror: CheckpointManager,
    ) -> None:
        """Initialise the runtime engine.

        Args:
            sink (MessageSink): Sends outbound envelopes to the client.
            gate (GateOrchestrator): Handles approval / question gates.
            key_provider (ApiKeyProvider): Retrieves cloud API keys on demand.
            get_settings (Callable): Returns fresh merged settings on each call.
            transient (TransientStore): Append-only JSONL session store.
            layout (ProjectLayout): Project filesystem layout.
            registry (AgentRegistry): Loaded subagent file registry.
            mirror (CheckpointManager): Mirror checkpoint manager.
        """
        self.__sink = sink
        self.__gate = gate
        self.__key_provider = key_provider
        self.__get_settings = get_settings
        self.__transient = transient
        self.__layout = layout
        self.__registry = registry
        self.__mirror = mirror
        self.__workspace = Workspace(layout.root)
        self.__queue = asyncio.Queue()
        self.__session = SessionState()
        self.__index = ProjectIndex()
        self.__worker = None
        self.__cumulative_usd = 0.0
        self.__orch_messages = []
        self.__orch_session_id = ""
        self.__current_vendor = None
        self.__tool_surface = self.__make_tool_surface()

    @property
    def session(self) -> SessionState:
        """Current session state snapshot."""
        return self.__session

    @property
    def gate(self) -> GateOrchestrator:
        """Gate orchestrator (needed by the approval handler in _app.py)."""
        return self.__gate

    async def start(self) -> None:
        """Start the worker coroutine.

        Initialises the mirror, runs the four-phase bootstrap, and starts
        the worker task.
        """
        await self.__mirror.ensure_initialized()

        result = ProjectBootstrap(
            mirror_dir=self.__layout.checkpoints_dir,
            workspace_dir=self.__layout.workspace_dir,
            sessions_dir=self.__layout.sessions_dir,
            kodo_dir=self.__layout.kodo_dir,
        ).run()

        self.__index = result.index
        self.__orch_session_id = result.orchestrator_session_id
        self.__tool_surface = self.__make_tool_surface()

        self.__transient.attach_session(result.orchestrator_session_id, result.orchestrator_resumed)

        if result.orchestrator_resumed:
            self.__orch_messages = self.__load_orch_messages()

        self.__worker = asyncio.create_task(self.__run_worker(), name="kodo-worker")
        _log.info(
            "Runtime worker started (orchestrator_session=%s resumed=%s messages=%d)",
            self.__orch_session_id,
            result.orchestrator_resumed,
            len(self.__orch_messages),
        )

    async def stop(self) -> None:
        """Cancel the worker and transition the session to stopped state."""
        if self.__worker is not None:
            self.__worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.__worker
            self.__worker = None
        self.__session.phase = "stopped"
        self.__session.agent = None
        await self.__emit_state()
        _log.info("Runtime worker stopped")

    async def handle_prompt_submit(self, text: str, request_id: str) -> None:
        """Enqueue a user prompt for the Orchestrator to process.

        Args:
            text: The user's prompt text.
            request_id: Envelope ID of the originating request.
        """
        self.__transient.update(prompt=text)
        await self.__queue.put({"text": text, "request_id": request_id})

    async def handle_mode_set(self, autonomous: bool) -> None:
        """Toggle autonomous mode.

        Args:
            autonomous: New autonomous mode value.
        """
        self.__session.autonomous = autonomous
        self.__transient.update(autonomous=autonomous)
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Plugin resolution — per-dispatch, reads fresh settings each time
    # ------------------------------------------------------------------

    async def __resolve_plugin(self, capability: str) -> tuple[LLMPlugin, str]:
        """Resolve an LLM plugin for *capability* using current settings.

        Args:
            capability: ``'high'``, ``'medium'``, or ``'low'``.

        Returns:
            tuple[LLMPlugin, str]: ``(plugin, model_id)``

        Raises:
            RuntimeError: If the client rejects or cancels the key request.
        """
        settings = self.__get_settings()
        mode = str(settings.get("mode", "cloud"))
        models_map = settings.get("models", {})
        if not isinstance(models_map, dict):
            models_map = {}

        if mode == "local":
            model_key = str(models_map.get("local", "llamacpp-qwen36-27b"))
        else:
            model_key = str(models_map.get(capability, models_map.get("medium", capability)))

        registry = get_llm_registry()
        entry = registry.get(model_key)
        module = entry.module if entry is not None else "kodo.llms.anthropic"

        if module == "kodo.llms.llamacpp":
            self.__current_vendor = None
            return LlamaPlugin(
                base_url=_DEFAULT_LLAMACPP_BASE_URL, model_names=[model_key]
            ), model_key

        model_id = entry.model_id if entry is not None else model_key
        vendor = module.rsplit(".", 1)[-1]
        self.__current_vendor = vendor

        key_result: ApiKey = await self.__key_provider.get_key(vendor)
        if key_result.error:
            raise RuntimeError(f"API key request rejected: {key_result.error}")

        return ClaudePlugin(api_key=key_result.api_key), model_id

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def __run_worker(self) -> None:
        try:
            orchestrator_available = self.__orchestrator_available()
        except Exception:
            orchestrator_available = False

        if not orchestrator_available:
            _log.warning(
                "Orchestrator agent %r not found in registry — "
                "runtime will echo prompts only until the agent file is added",
                _ORCHESTRATOR_AGENT_NAME,
            )

        while True:
            task = await self.__queue.get()
            text = str(task.get("text", ""))
            try:
                if orchestrator_available:
                    await self.__run_orchestrator_with_input(text)
                else:
                    await self.__handle_input_no_orchestrator(text)

                if self.__session.phase == "done":
                    _log.info("Project finalized — worker exiting")
                    break

            except asyncio.CancelledError:
                raise
            except UnrecoverableError as exc:
                _log.error("Unrecoverable LLM error (HTTP %d): %s", exc.status_code, exc)
                if exc.status_code == 401 and self.__current_vendor:
                    _log.warning(
                        "API key rejected (401) for vendor=%r — sending revoke to client",
                        self.__current_vendor,
                    )
                    await self.__sink.send(
                        Envelope.make_event(EVT_API_KEY_REVOKE, {"vendor": self.__current_vendor})
                    )
                await self.__emit_error(str(exc), recoverable=False)
                self.__session.phase = "stopped"
                self.__session.agent = None
                await self.__emit_state()
            except Exception as exc:
                _log.exception("Unhandled error in runtime worker: %s", exc)
                await self.__emit_error(str(exc), recoverable=True)
                self.__session.agent = None
                await self.__emit_state()
            finally:
                self.__queue.task_done()

    def __orchestrator_available(self) -> bool:
        try:
            self.__registry.get(_ORCHESTRATOR_AGENT_NAME)
            return True
        except AgentLoadError:
            return False

    async def __handle_input_no_orchestrator(self, text: str) -> None:
        self.__session.phase = "running"
        await self.__emit_state()
        _log.info(
            "Prompt received (len=%d) — no orchestrator agent; add subagent_%s.md to register one",
            len(text),
            _ORCHESTRATOR_AGENT_NAME,
        )
        self.__session.phase = "intake"
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Orchestrator LLM loop
    # ------------------------------------------------------------------

    async def __run_orchestrator_with_input(self, text: str) -> None:
        agent = self.__registry.get(_ORCHESTRATOR_AGENT_NAME)
        plugin, model_id = await self.__resolve_plugin(agent.capability)

        pre_turn_len = len(self.__orch_messages)
        if text:
            self.__orch_messages = self.__orch_messages + [Message(role="user", content=text)]

        self.__session.phase = "running"
        self.__session.agent = _ORCHESTRATOR_AGENT_NAME
        await self.__emit_state()
        await self.__emit_agent_started(_ORCHESTRATOR_AGENT_NAME)

        stream_id = uuid.uuid4().hex
        self.__orch_messages, _ = await self.__run_agent_turn(
            llm=plugin,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=self.__orch_messages,
            tools=list(ORCHESTRATOR_TOOLS),
            tool_dispatch=self.__dispatch_orchestrator_tool,
            stream_id=stream_id,
        )
        await self.__sink.send(Envelope.make_stream_end(stream_id))
        await self.__emit_agent_finished(_ORCHESTRATOR_AGENT_NAME)

        for msg in self.__orch_messages[pre_turn_len:]:
            self.__transient.append_message(msg.role, msg.content)

        if self.__session.phase != "done":
            self.__session.phase = "awaiting_user"
        self.__session.agent = None
        await self.__emit_state()

    async def __dispatch_orchestrator_tool(
        self, tool_name: str, tool_input: dict[str, object]
    ) -> str:
        return await self.__tool_surface.dispatch(tool_name, tool_input)

    # ------------------------------------------------------------------
    # Generic agent turn (single LLM call + tool loop)
    # ------------------------------------------------------------------

    async def __run_agent_turn(
        self,
        llm: LLMPlugin,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_dispatch: Callable[[str, dict[str, object]], Awaitable[str]],
        stream_id: str,
        agent_name: str = _ORCHESTRATOR_AGENT_NAME,
        stop_after_tools: Callable[[], bool] | None = None,
    ) -> tuple[list[Message], list[Path]]:
        """Run one LLM turn with tool-use loop until the model stops calling tools.

        Args:
            llm: LLM plugin to use for this turn.
            model: Model identifier string passed to the plugin.
            system_prompt: The agent's system prompt.
            messages: Current message history.
            tools: Tool specs exposed to the model.
            tool_dispatch: Async function dispatching tool calls to handlers.
            stream_id: Stream identifier for token events.
            agent_name: Agent name used in usage records.
            stop_after_tools: When provided and returns ``True`` after a tool
                batch, the loop exits without calling the LLM again.

        Returns:
            tuple[list[Message], list[Path]]: Updated messages and (unused) files.
        """
        files_written: list[Path] = []

        while True:
            call_start = datetime.now(tz=UTC).isoformat()
            text_parts: list[str] = []
            tool_calls: list[ToolCallEvent] = []
            turn_end: TurnEnd | None = None

            try:
                async for event in llm.stream_query(
                    stream_id=stream_id,
                    model=model,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                    cache_breakpoints=[0],
                ):
                    await self.__handle_stream_event(event, stream_id)
                    if isinstance(event, TokenDelta):
                        text_parts.append(event.text)
                    elif isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                    elif isinstance(event, TurnEnd):
                        turn_end = event
            except Exception:
                await self.__sink.send(Envelope.make_stream_end(stream_id))
                raise

            if turn_end is not None:
                self.__cumulative_usd += turn_end.usage.usd_cost
                await self.__emit_usage(turn_end, model)
                await self.__transient.write_agent_record(
                    agent_name,
                    {
                        "call_start": call_start,
                        "call_end": datetime.now(tz=UTC).isoformat(),
                        "model": model,
                        "input_tokens": turn_end.usage.input_tokens,
                        "output_tokens": turn_end.usage.output_tokens,
                        "cache_write_tokens": turn_end.usage.cache_write_tokens,
                        "cache_read_tokens": turn_end.usage.cache_read_tokens,
                        "usd_cost": turn_end.usage.usd_cost,
                        "cumulative_usd": self.__cumulative_usd,
                        "stop_reason": turn_end.stop_reason,
                    },
                )

            if not tool_calls:
                messages = messages + [
                    Message(role="assistant", content="".join(text_parts) or "(no text)")
                ]
                break

            assistant_content: list[dict[str, object]] = []
            if text_parts:
                assistant_content.append({"type": "text", "text": "".join(text_parts)})
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
                result_text = await tool_dispatch(tc.tool_name, tc.tool_input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.tool_use_id,
                        "content": result_text,
                    }
                )
            messages = messages + [Message(role="user", content=tool_results)]

            if stop_after_tools is not None and stop_after_tools():
                break

        return messages, files_written

    # ------------------------------------------------------------------
    # ToolSurface factory
    # ------------------------------------------------------------------

    def __make_tool_surface(self) -> ToolSurface:
        return ToolSurface(
            index=self.__index,
            gate=self.__gate,
            session=self.__session,
            run_subagent_fn=self.__run_subagent,
            run_author_critic_fn=self.__run_author_critic_iteration,
            rollback_fn=self.__run_rollback,
        )

    # ------------------------------------------------------------------
    # Subagent dispatch
    # ------------------------------------------------------------------

    async def __run_subagent(
        self, name: str, task_message: str, input_artifact_ids: list[str]
    ) -> list[str]:
        """Invoke a leaf sub-agent and return the artifact IDs it published.

        Args:
            name: Sub-agent name from the registry.
            task_message: Task message injected as the initial user turn.
            input_artifact_ids: IDs the agent may reference via read_artifact.

        Returns:
            list[str]: Artifact IDs published during the run.
        """
        agent = self.__registry.get(name)
        plugin, model_id = await self.__resolve_plugin(agent.capability)

        session_id = uuid.uuid4().hex
        dispatcher = SubagentDispatcher(
            workspace=self.__workspace,
            gate=self.__gate,
            agent_name=name,
            session_id=session_id,
        )
        leaf_tools = tools_for_agent(agent)

        parts = [task_message] if task_message else []
        if input_artifact_ids:
            parts.append(
                "\n## Input Artifact IDs\n" + "\n".join(f"- {aid}" for aid in input_artifact_ids)
            )
        initial_content = "\n".join(parts) or "(no task message)"
        messages: list[Message] = [Message(role="user", content=initial_content)]

        self.__session.phase = "running"
        self.__session.agent = name
        await self.__emit_state()

        stream_id = uuid.uuid4().hex
        await self.__emit_agent_started(name)

        messages, _ = await self.__run_agent_turn(
            llm=plugin,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=messages,
            tools=leaf_tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=name,
            stop_after_tools=lambda: dispatcher.stop_requested,
        )

        await self.__sink.send(Envelope.make_stream_end(stream_id))
        await self.__emit_agent_finished(name)
        _log.info(
            "start_subagent completed: name=%s published=%s",
            name,
            dispatcher.published_ids,
        )
        return dispatcher.published_ids

    # ------------------------------------------------------------------
    # Author/Critic iteration
    # ------------------------------------------------------------------

    async def __run_author_critic_iteration(
        self,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        previous_artifact_id: str | None,
    ) -> dict[str, object]:
        """Execute one Author/Critic round and return verdict + concerns.

        Args:
            author_name: Author sub-agent name.
            critic_name: Critic sub-agent name.
            input_artifact_ids: Input artifact IDs for the Author.
            previous_artifact_id: Prior Author output to revise (optional).

        Returns:
            dict: ``{artifact_id, verdict, concerns}`` from the Critic's feedback.
        """
        author_task_parts = ["Produce your artifact."]
        if previous_artifact_id:
            author_task_parts.append(
                f"\nPrior version to revise: artifact_id={previous_artifact_id}"
            )
        author_task = "\n".join(author_task_parts)

        author_ids = await self.__run_subagent(author_name, author_task, input_artifact_ids)

        primary_id: str | None = None
        for aid in reversed(author_ids):
            arts = await self.__workspace.read(artifact_id=aid)
            if arts and arts[0].type != ArtifactType.FEEDBACK:
                primary_id = aid
                break

        if primary_id is None:
            _log.warning(
                "run_author_critic_iteration: %s produced no non-feedback artifact", author_name
            )
            return {"artifact_id": None, "verdict": "accepted", "concerns": []}

        await self.__sink.send(
            Envelope.make_event(
                EVT_REVIEW_STARTED,
                {
                    "reviewer_name": critic_name,
                    "target_filename": primary_id[:8],
                    "target_type": "artifact",
                },
            )
        )

        critic_task = (
            f"Review artifact {primary_id} and publish a feedback artifact "
            f"with reviewed_artifact_id={primary_id}."
        )
        critic_ids = await self.__run_subagent(critic_name, critic_task, [primary_id])

        verdict = "accepted"
        concerns: list[dict[str, object]] = []
        feedback_art_id: str | None = next((aid for aid in reversed(critic_ids)), None)
        if feedback_art_id:
            feedback_arts = await self.__workspace.read(artifact_id=feedback_art_id)
            if feedback_arts:
                fa = feedback_arts[0]
                verdict = fa.verdict.value if fa.verdict else "accepted"
                concerns = [{"kind": c.kind, "description": c.description} for c in fa.concerns]

        await self.__sink.send(
            Envelope.make_event(
                EVT_REVIEW_VERDICT,
                {
                    "reviewer_name": critic_name,
                    "target_filename": primary_id[:8],
                    "verdict": verdict,
                    "concern_count": len(concerns),
                },
            )
        )

        return {"artifact_id": primary_id, "verdict": verdict, "concerns": concerns}

    # ------------------------------------------------------------------
    # Rollback callback
    # ------------------------------------------------------------------

    async def __run_rollback(self, target_sha: str) -> None:
        """Execute rollback, rebuild the index, and start a fresh Orchestrator session.

        Args:
            target_sha: Mirror commit SHA to roll back to.
        """
        _log.info("Rollback initiated: target_sha=%s", target_sha[:12])
        rollback = Rollback(self.__layout.root, self.__mirror.repo)
        result = await rollback.execute(target_sha)

        self.__index = result.index
        self.__orch_session_id = result.orchestrator_session_id
        self.__orch_messages = []
        self.__tool_surface = self.__make_tool_surface()
        self.__transient.attach_session(result.orchestrator_session_id, result.orchestrator_resumed)
        _log.info("Post-rollback Orchestrator session: %s", self.__orch_session_id)

    def __load_orch_messages(self) -> list[Message]:
        raw = self.__transient.read_messages()
        messages: list[Message] = []
        for item in raw:
            try:
                role = str(item["role"])
                content = item["content"]
                if isinstance(content, (str, list)):
                    messages.append(Message(role=role, content=content))
            except (KeyError, TypeError):
                _log.warning("Skipping malformed message in session.jsonl")
        return messages

    # ------------------------------------------------------------------
    # Event emitters
    # ------------------------------------------------------------------

    async def __handle_stream_event(self, event: StreamEvent, stream_id: str) -> None:
        if isinstance(event, TokenDelta):
            await self.__sink.send(Envelope.make_stream_chunk(stream_id, event.text))

    async def __emit_state(self) -> None:
        await self.__sink.send(Envelope.make_event(EVT_STATE, self.__session.to_dict()))

    async def __emit_usage(self, turn_end: TurnEnd, model: str) -> None:
        await self.__sink.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self.__cumulative_usd, 6),
                    "last_call_tokens": {
                        "input": turn_end.usage.input_tokens,
                        "output": turn_end.usage.output_tokens,
                        "cache_write": turn_end.usage.cache_write_tokens,
                        "cache_read": turn_end.usage.cache_read_tokens,
                    },
                    "model": model,
                    "breakdown": {},
                },
            )
        )

    async def __emit_error(self, message: str, *, recoverable: bool) -> None:
        await self.__sink.send(
            Envelope.make_event(
                EVT_ERROR,
                {
                    "code": "runtime_error",
                    "message": message,
                    "recoverable": recoverable,
                },
            )
        )

    async def __emit_agent_started(self, agent_name: str) -> None:
        await self.__sink.send(
            Envelope.make_event(
                EVT_AGENT_STARTED,
                {"agent": agent_name, "component": self.__session.component},
            )
        )

    async def __emit_agent_finished(self, agent_name: str) -> None:
        await self.__sink.send(
            Envelope.make_event(
                EVT_AGENT_FINISHED,
                {
                    "agent": agent_name,
                    "component": self.__session.component,
                    "status": "ok",
                },
            )
        )
