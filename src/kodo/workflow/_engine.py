"""Workflow engine: single async worker driving a stage-spec pipeline.

The workflow is defined as a tuple of :class:`~._spec.StageSpec` entries in
:data:`~._spec.PROJECT_STAGES`.  The engine iterates over the specs and calls
the generic :meth:`WorkflowEngine.__run_stage` for each one — adding a stage
in M4+ means adding a new ``StageSpec``, not new engine code.

Each stage follows the same shape:

1. Run Author (tool-use loop; re-run with reminder if artifact not written).
2. If ``spec.critic`` is set: run Critic silently; loop up to ``_MAX_AR_ITER``
   times on ``FEEDBACK`` responses.
3. Run any post-author hook (e.g. scaffold component dirs after architecture).
4. Fire approval gate → Agree / Feedback / Stop.
5. On Agree: mirror checkpoint, advance.  On Feedback: re-run from step 1
   with Dev feedback appended.  On Stop: raise :exc:`_GateStoppedError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kodo.llms._interface import (
    Message,
    StreamEvent,
    TokenDelta,
    ToolCallEvent,
    ToolSpec,
    TurnEnd,
)
from kodo.llms.anthropic import UnrecoverableError
from kodo.transport._envelope import Envelope
from kodo.transport._messages import (
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_ERROR,
    EVT_FILE_CHANGE,
    EVT_STATE,
    EVT_USAGE_UPDATE,
)

from ._gates import GateOrchestrator
from ._session import SessionState
from ._spec import PROJECT_STAGES, StageSpec, build_component_stages
from ._stages import Stage

if TYPE_CHECKING:
    from kodo.agents._loader import Agent
    from kodo.agents._registry import AgentRegistry
    from kodo.llms._interface import LLMPlugin
    from kodo.mirror._checkpoints import CheckpointManager
    from kodo.project._layout import ProjectLayout
    from kodo.state._transient import TransientStore
    from kodo.transport._ws import AppState

__all__ = ["WorkflowEngine"]

_log = logging.getLogger(__name__)

_MAX_AR_ITER = 5  # max Author/Critic iterations before forcing a gate


class _GateStoppedError(Exception):
    """Raised when the developer clicks Stop at an approval gate."""


# ---------------------------------------------------------------------------
# Inline file-I/O tool specs (pre-MCP; replaced by full MCP in M4)
# ---------------------------------------------------------------------------

_FILEIO_READ = ToolSpec(
    name="fileio_read_file",
    description=(
        "Read the contents of a file inside the project directory. "
        "Path is relative to the project root (e.g. 'src/narrative.kd')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to project root.",
            }
        },
        "required": ["path"],
    },
)

_FILEIO_WRITE = ToolSpec(
    name="fileio_write_file",
    description=(
        "Write content to a file inside the project directory, creating "
        "parent directories as needed. Path is relative to the project root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to project root.",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write.",
            },
        },
        "required": ["path", "content"],
    },
)

_TOOLS_BY_NAME: dict[str, ToolSpec] = {
    _FILEIO_READ.name: _FILEIO_READ,
    _FILEIO_WRITE.name: _FILEIO_WRITE,
}


class WorkflowEngine:
    """Single-worker workflow engine driven by a :class:`~._spec.StageSpec` pipeline.

    Args:
        app_state: WebSocket state for event emission.
        llm: LLM provider plugin.
        transient: Append-only JSONL session store.
        layout: Project filesystem layout.
        registry: Loaded agent file registry.
        mirror: Mirror checkpoint manager.
        default_model: Model identifier used for agent lookups.
    """

    __app_state: AppState
    __llm: LLMPlugin
    __transient: TransientStore
    __layout: ProjectLayout
    __registry: AgentRegistry
    __mirror: CheckpointManager
    __gate: GateOrchestrator
    __default_model: str
    __queue: asyncio.Queue[dict[str, object]]
    __session: SessionState
    __worker: asyncio.Task[None] | None
    __cumulative_usd: float

    def __init__(
        self,
        app_state: AppState,
        llm: LLMPlugin,
        transient: TransientStore,
        layout: ProjectLayout,
        registry: AgentRegistry,
        mirror: CheckpointManager,
        default_model: str = "claude-sonnet-4-6",
    ) -> None:
        self.__app_state = app_state
        self.__llm = llm
        self.__transient = transient
        self.__layout = layout
        self.__registry = registry
        self.__mirror = mirror
        self.__gate = GateOrchestrator(app_state)
        self.__default_model = default_model
        self.__queue = asyncio.Queue()
        self.__session = SessionState()
        self.__worker = None
        self.__cumulative_usd = 0.0

    @property
    def session(self) -> SessionState:
        """Current session state snapshot."""
        return self.__session

    @property
    def gate(self) -> GateOrchestrator:
        """Gate orchestrator (needed by the approval handler in _app.py)."""
        return self.__gate

    async def start(self) -> None:
        """Start the single worker coroutine and initialise the mirror."""
        await self.__mirror.ensure_initialized()
        self.__worker = asyncio.create_task(
            self.__run_worker(), name="kodo-worker"
        )
        _log.info("Workflow worker started (session=%s)", self.__session.session_id)

    async def stop(self) -> None:
        """Cancel the worker and leave the session in STOPPED state."""
        if self.__worker is not None:
            self.__worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.__worker
            self.__worker = None
        self.__session.stage = Stage.STOPPED
        await self.__emit_state()
        _log.info("Workflow worker stopped")

    async def handle_prompt_submit(self, text: str, request_id: str) -> None:
        """Enqueue a user prompt for processing."""
        await self.__queue.put({"text": text, "request_id": request_id})

    async def handle_mode_set(self, autonomous: bool) -> None:
        """Toggle autonomous mode."""
        self.__session.autonomous = autonomous
        self.__transient.meta.update(autonomous=autonomous)
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def __run_worker(self) -> None:
        while True:
            task = await self.__queue.get()
            text = str(task.get("text", ""))
            request_id = str(task.get("request_id", uuid.uuid4().hex))
            try:
                await self.__process_prompt(text, request_id)
            except asyncio.CancelledError:
                raise
            except _GateStoppedError:
                _log.info("Workflow stopped by developer at gate")
                self.__session.stage = Stage.STOPPED
                self.__session.agent = None
                await self.__emit_state()
            except UnrecoverableError as exc:
                _log.error("Unrecoverable LLM error (HTTP %d): %s", exc.status_code, exc)
                await self.__emit_error(str(exc), recoverable=False)
                self.__session.stage = Stage.ERROR
                self.__session.agent = None
                await self.__emit_state()
            except Exception as exc:
                _log.exception("Unhandled error in workflow worker: %s", exc)
                await self.__emit_error(str(exc), recoverable=True)
                self.__session.stage = Stage.IDLE
                self.__session.agent = None
                await self.__emit_state()
            finally:
                self.__queue.task_done()

    # ------------------------------------------------------------------
    # Top-level prompt dispatch
    # ------------------------------------------------------------------

    async def __process_prompt(self, text: str, request_id: str) -> None:
        self.__layout.src_dir.mkdir(parents=True, exist_ok=True)
        self.__layout.gen_dir.mkdir(parents=True, exist_ok=True)

        # Context grows as stages complete: artifact path → file text.
        # The original prompt is always available under the "prompt" key.
        context: dict[str, str] = {"prompt": text}

        # Queue-based dispatch: after ARCHITECTURE completes, per-component
        # specs are prepended so they run before any remaining project stages.
        pending: list[StageSpec] = list(PROJECT_STAGES)
        while pending:
            spec = pending.pop(0)
            await self.__run_stage(spec, context)
            artifact = self.__layout.root / spec.artifact
            if artifact.exists():
                context[spec.artifact] = artifact.read_text(encoding="utf-8")

            if spec.stage == Stage.ARCHITECTURE:
                component_specs = self.__build_component_stages_from_dag()
                pending = list(component_specs) + pending

        self.__session.stage = Stage.IDLE
        self.__session.agent = None
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Generic stage runner
    # ------------------------------------------------------------------

    async def __run_stage(self, spec: StageSpec, context: dict[str, str]) -> None:
        """Execute one stage: Author → Critic loop → Gate (Agree/Feedback/Stop)."""
        self.__session.stage = spec.stage
        self.__session.agent = spec.author
        self.__session.component = spec.component
        await self.__emit_state()

        artifact_path = self.__layout.root / spec.artifact
        author_agent = self.__registry.get(spec.author, self.__default_model)
        critic_agent = (
            self.__registry.get(spec.critic, self.__default_model)
            if spec.critic
            else None
        )

        messages: list[Message] = [
            Message(role="user", content=spec.build_task_message(context))
        ]

        approved = False
        while not approved:
            messages = await self.__author_reviewer_loop(
                author_agent=author_agent,
                critic_agent=critic_agent,
                artifact_path=artifact_path,
                messages=messages,
            )

            await self.__post_stage_hook(spec)

            summary = _first_line(artifact_path)
            gate_response = await self.__gate.fire(
                spec.gate_type,
                artifact_path=artifact_path,
                summary=summary,
                component=spec.component,
            )

            if gate_response.action == "agree":
                sha = await self.__mirror.create_checkpoint(
                    spec.gate_type, spec.component
                )
                _log.info("Checkpoint [%s]: %s", spec.gate_type, sha[:8])
                approved = True
            elif gate_response.action == "stop":
                raise _GateStoppedError()
            else:
                messages = messages + [
                    Message(
                        role="user",
                        content=f"## Developer Feedback\n\n{gate_response.feedback}",
                    )
                ]

    # ------------------------------------------------------------------
    # Author / Critic loop
    # ------------------------------------------------------------------

    async def __author_reviewer_loop(
        self,
        *,
        author_agent: Agent,
        critic_agent: Agent | None,
        artifact_path: Path,
        messages: list[Message],
    ) -> list[Message]:
        """Run Author → Critic up to ``_MAX_AR_ITER`` times.

        * If the Author's turn ends without writing the artifact, a reminder
          is injected and the Author is re-run (no Critic involved).
        * If ``critic_agent`` is ``None``, the loop exits as soon as the
          artifact exists — no review phase.

        Returns the updated messages list.
        """
        try:
            rel_path = artifact_path.relative_to(self.__layout.root).as_posix()
        except ValueError:
            rel_path = artifact_path.name

        for _ in range(_MAX_AR_ITER):
            self.__session.agent = author_agent.name
            await self.__emit_state()

            stream_id = uuid.uuid4().hex
            await self.__emit_agent_started(author_agent.name)
            messages, _ = await self.__run_agent(author_agent, messages, stream_id)
            await self.__app_state.send(Envelope.make_stream_end(stream_id))
            await self.__emit_agent_finished(author_agent.name)

            if not artifact_path.exists():
                messages = messages + [
                    Message(
                        role="user",
                        content=(
                            "## Reminder\n\n"
                            f"You have not yet written `{rel_path}`. "
                            "Please use the `fileio_write_file` tool to create "
                            "that file before we can proceed."
                        ),
                    )
                ]
                continue

            if critic_agent is None:
                break

            self.__session.agent = critic_agent.name
            await self.__emit_state()
            await self.__emit_agent_started(critic_agent.name)
            verdict = await self.__run_critic(critic_agent, artifact_path)
            await self.__emit_agent_finished(critic_agent.name)

            if verdict == "ACCEPT":
                break

            messages = messages + [
                Message(
                    role="user",
                    content=f"## Reviewer Feedback\n\n{verdict}",
                )
            ]

        return messages

    # ------------------------------------------------------------------
    # Post-stage hook (stage-specific side-effects after author/critic)
    # ------------------------------------------------------------------

    async def __post_stage_hook(self, spec: StageSpec) -> None:
        """Run any side-effects needed after an author/critic loop completes."""
        if spec.stage == Stage.ARCHITECTURE:
            await self.__scaffold_components()

    # ------------------------------------------------------------------
    # Agent execution (multi-turn tool-use loop)
    # ------------------------------------------------------------------

    async def __run_agent(
        self,
        agent: Agent,
        messages: list[Message],
        stream_id: str,
    ) -> tuple[list[Message], list[Path]]:
        """Run ``agent`` to completion, handling file-I/O tool calls inline."""
        tools = [_TOOLS_BY_NAME[t] for t in agent.tools if t in _TOOLS_BY_NAME]
        files_written: list[Path] = []

        while True:
            call_start = datetime.now(tz=UTC).isoformat()
            text_parts: list[str] = []
            tool_calls: list[ToolCallEvent] = []
            turn_end: TurnEnd | None = None

            try:
                async for event in self.__llm.stream_query(
                    stream_id=stream_id,
                    model=agent.model,
                    system=agent.system_prompt,
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
                await self.__app_state.send(Envelope.make_stream_end(stream_id))
                raise

            if turn_end is not None:
                self.__cumulative_usd += turn_end.usage.usd_cost
                await self.__emit_usage(turn_end)
                await self.__transient.write_agent_record(
                    agent.name,
                    {
                        "call_start": call_start,
                        "call_end": datetime.now(tz=UTC).isoformat(),
                        "model": agent.model,
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
                result_text, new_files = await self.__execute_tool(tc)
                files_written.extend(new_files)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.tool_use_id,
                        "content": result_text,
                    }
                )
            messages = messages + [Message(role="user", content=tool_results)]

        return messages, files_written

    # ------------------------------------------------------------------
    # Silent critic run
    # ------------------------------------------------------------------

    async def __run_critic(self, agent: Agent, artifact_path: Path) -> str:
        """Run the critic agent silently; return ``'ACCEPT'`` or feedback."""
        if not artifact_path.exists():
            return "ACCEPT"

        content = artifact_path.read_text(encoding="utf-8")
        messages: list[Message] = [
            Message(
                role="user",
                content=(
                    f"## Artifact to Review\n\n"
                    f"**File:** `{artifact_path.name}`\n\n"
                    f"{content}"
                ),
            )
        ]

        stream_id = uuid.uuid4().hex
        text_parts: list[str] = []
        call_start = datetime.now(tz=UTC).isoformat()

        async for event in self.__llm.stream_query(
            stream_id=stream_id,
            model=agent.model,
            system=agent.system_prompt,
            messages=messages,
            tools=[],
            cache_breakpoints=[],
        ):
            if isinstance(event, TokenDelta):
                text_parts.append(event.text)
            elif isinstance(event, TurnEnd):
                self.__cumulative_usd += event.usage.usd_cost
                await self.__emit_usage(event)
                await self.__transient.write_agent_record(
                    agent.name,
                    {
                        "call_start": call_start,
                        "call_end": datetime.now(tz=UTC).isoformat(),
                        "model": agent.model,
                        "input_tokens": event.usage.input_tokens,
                        "output_tokens": event.usage.output_tokens,
                        "cache_write_tokens": event.usage.cache_write_tokens,
                        "cache_read_tokens": event.usage.cache_read_tokens,
                        "usd_cost": event.usage.usd_cost,
                        "cumulative_usd": self.__cumulative_usd,
                        "stop_reason": event.stop_reason,
                    },
                )

        response = "".join(text_parts).strip()
        return "ACCEPT" if "ACCEPT" in response.upper() else response

    # ------------------------------------------------------------------
    # Inline tool executor
    # ------------------------------------------------------------------

    async def __execute_tool(
        self, event: ToolCallEvent
    ) -> tuple[str, list[Path]]:
        if event.tool_name == _FILEIO_WRITE.name:
            return await self.__tool_write_file(event)
        if event.tool_name == _FILEIO_READ.name:
            return self.__tool_read_file(event), []
        _log.warning("Unknown tool requested: %s", event.tool_name)
        return f"Error: unknown tool '{event.tool_name}'", []

    async def __tool_write_file(
        self, event: ToolCallEvent
    ) -> tuple[str, list[Path]]:
        rel_path = str(event.tool_input.get("path", "")).strip()
        content = str(event.tool_input.get("content", ""))
        if not rel_path:
            return "Error: 'path' is required", []

        target = (self.__layout.root / rel_path).resolve()
        if not target.is_relative_to(self.__layout.root.resolve()):
            return "Error: path is outside the project root", []

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _log.info("Agent wrote file: %s", rel_path)

        await self.__emit_file_change(target)
        return "File written successfully.", [target]

    def __tool_read_file(self, event: ToolCallEvent) -> str:
        rel_path = str(event.tool_input.get("path", "")).strip()
        if not rel_path:
            return "Error: 'path' is required"

        target = (self.__layout.root / rel_path).resolve()
        if not target.is_relative_to(self.__layout.root.resolve()):
            return "Error: path is outside the project root"

        if not target.exists():
            return f"Error: file '{rel_path}' does not exist"
        return target.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Component scaffolding (post-architecture hook)
    # ------------------------------------------------------------------

    async def __scaffold_components(self) -> None:
        dag_path = self.__layout.src_dir / "responsibilities.dag.json"
        if not dag_path.exists():
            return
        try:
            dag = json.loads(dag_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Could not parse responsibilities.dag.json: %s", exc)
            return

        for comp in dag.get("components", []):
            name = str(comp.get("name", "")).strip()
            if not name:
                continue
            # Create directory only — agents write the .kd files themselves.
            (self.__layout.src_dir / name).mkdir(exist_ok=True)

    def __build_component_stages_from_dag(self) -> tuple[StageSpec, ...]:
        """Read responsibilities.dag.json and return per-component stage specs."""
        dag_path = self.__layout.src_dir / "responsibilities.dag.json"
        if not dag_path.exists():
            _log.warning("No responsibilities.dag.json — skipping per-component stages")
            return ()
        try:
            dag = json.loads(dag_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Could not parse responsibilities.dag.json: %s", exc)
            return ()
        names = [
            str(c.get("name", "")).strip()
            for c in dag.get("components", [])
            if str(c.get("name", "")).strip()
        ]
        return build_component_stages(names)

    # ------------------------------------------------------------------
    # Event emitters
    # ------------------------------------------------------------------

    async def __handle_stream_event(
        self, event: StreamEvent, stream_id: str
    ) -> None:
        if isinstance(event, TokenDelta):
            await self.__app_state.send(
                Envelope.make_stream_chunk(stream_id, event.text)
            )

    async def __emit_state(self) -> None:
        await self.__app_state.send(
            Envelope.make_event(EVT_STATE, self.__session.to_dict())
        )
        self.__transient.meta.update(stage=self.__session.stage.value)

    async def __emit_usage(self, turn_end: TurnEnd) -> None:
        await self.__app_state.send(
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
                    "breakdown": {},
                },
            )
        )

    async def __emit_error(self, message: str, *, recoverable: bool) -> None:
        await self.__app_state.send(
            Envelope.make_event(
                EVT_ERROR,
                {
                    "code": "workflow_error",
                    "message": message,
                    "recoverable": recoverable,
                },
            )
        )

    async def __emit_agent_started(self, agent_name: str) -> None:
        await self.__app_state.send(
            Envelope.make_event(
                EVT_AGENT_STARTED,
                {"agent": agent_name, "component": self.__session.component},
            )
        )

    async def __emit_agent_finished(self, agent_name: str) -> None:
        await self.__app_state.send(
            Envelope.make_event(
                EVT_AGENT_FINISHED,
                {
                    "agent": agent_name,
                    "component": self.__session.component,
                    "status": "ok",
                },
            )
        )

    async def __emit_file_change(self, path: Path) -> None:
        try:
            rel = path.relative_to(self.__layout.root)
        except ValueError:
            rel = path
        await self.__app_state.send(
            Envelope.make_event(
                EVT_FILE_CHANGE,
                {"kind": "add", "path": rel.as_posix()},
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_line(path: Path) -> str:
    """Return the first non-empty line of ``path``, or an empty string."""
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
