"""Kodo runtime engine — single async worker hosting the Guide session.

The engine is a thin substrate.  It does not contain a stage machine, a
scheduler, or a workflow DAG.  Every decision about what runs when is the
Guide's, encoded in its system prompt and carried out via the unified
tool surface in :mod:`kodo.tools`.

Architecture (DESIGN.md §5):
- One ``asyncio.Queue`` + one worker coroutine (FR-WF-02).
- The worker drives the Guide LLM: builds the turn, dispatches tool
  calls through a per-run :class:`kodo.tools.ToolDispatcher`, appends results,
  repeats until the model emits no more tool calls.  Leaf sub-agents run the
  same loop with their own dispatcher — the only difference is the tool set.
- User prompts (via ``prompt.submit``) are fed to the Guide as new user
  messages between turns.
- Approval/question blocking happens inside the gate-backed tool handlers
  which ``await`` a :class:`asyncio.Future` resolved by the WS dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from kodo.binutils import find_util
from kodo.common import ApiKey, ApiKeyProvider, Envelope, MessageSink
from kodo.llms import (
    LLMGateway,
    LLMPlugin,
    LLMRouting,
    LoggingLLMPlugin,
    Message,
    StreamEvent,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallArgDelta,
    ToolCallEvent,
    ToolCallLogger,
    ToolSpec,
    TurnEnd,
    get_context_window,
    get_llm_registry,
)
from kodo.llms.anthropic import ClaudePlugin, UnrecoverableError
from kodo.llms.llamacpp import LlamaPlugin
from kodo.project import (
    ProjectLayout,
    ProjectLayoutError,
    SessionWorkspace,
    WorkspaceLayout,
    kodo_user_dir,
)
from kodo.state import TransientStore, read_diff_files, render_tool_call_markdown
from kodo.subagents import AgentLoadError, AgentRegistry, SubAgent
from kodo.toolchains import ToolchainPlugin, select_toolchain
from kodo.tools import (
    LogicalPathResolver,
    PathResolver,
    ProjectPathResolver,
    RootPath,
    ToolDispatcher,
    tools_for_agent,
)
from kodo.toolspecs import (
    ALL_TOOLS,
    SCHEMA_COMPLIANCE_KEY,
    build_detail_rows,
    normalize_output,
    tool_result_succeeded,
)
from kodo.transport import (
    EVT_AGENT_FINISHED,
    EVT_AGENT_STARTED,
    EVT_AGENT_TOOL_CALL,
    EVT_AGENT_TOOL_CALL_DETAIL,
    EVT_API_KEY_REVOKE,
    EVT_AUTONOMOUS_CHANGED,
    EVT_CONTEXT_COMPACTED,
    EVT_CONTEXT_COMPACTING,
    EVT_CONTEXT_STATS,
    EVT_ERROR,
    EVT_LLM_TURN_START,
    EVT_POST_UPDATE,
    EVT_PROJECT_BOUND,
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_SESSION_NAME,
    EVT_SESSION_NAMING,
    EVT_STATE,
    EVT_SUBSESSION_ENDED,
    EVT_SUBSESSION_STARTED,
    EVT_TOOL_INCOMPLIANT,
    EVT_USAGE_UPDATE,
    EVT_USER_ATTACHMENTS,
)
from kodo.workspace import (
    ArtifactType,
    CheckpointManager,
    ComponentRegistry,
    ProjectIndex,
    Promoter,
    PromoterError,
    Workspace,
    materialization_path,
)

from ._attachments import (
    MAX_ATTACHMENTS,
    AttachmentError,
    inject_attachments,
    load_attachment,
    parse_attachment_marker,
)
from ._bootstrap import ProjectBootstrap
from ._gates import GateOrchestrator
from ._rollback import Rollback
from ._session import SessionState

__all__ = ["WorkflowEngine"]

_log = logging.getLogger(__name__)

_GUIDE_AGENT_NAME = "guide"
_PROBLEM_SOLVER_AGENT_NAME = "problem_solver"
_SESSION_TITLER_AGENT_NAME = "session_titler"
_COMPACTOR_AGENT_NAME = "compactor"

# Sub-agents that the engine drives directly and that must never be reachable
# through the ``run_subagent`` tool (the Guide/Problem Solver cannot
# invoke them).
_DIRECT_ONLY_AGENTS = frozenset({_SESSION_TITLER_AGENT_NAME, _COMPACTOR_AGENT_NAME})

# Context compaction. The live main context is measured in tokens after every
# entry-agent turn; once it reaches ``_COMPACTION_THRESHOLD`` of the current
# model's context window (the per-model ``context_window`` in the LLM registry,
# resolved via ``__context_limit``) the engine runs the ``compactor`` sub-agent
# to summarise the context and reset it in place. The user can also trigger this
# manually while idle (``compact.now``). A model switch to a smaller window can
# trigger it immediately (``handle_config_changed``).
_COMPACTION_THRESHOLD = 0.9

# The two top-level entry agents share one agent-agnostic main message history
# (``__main_messages``); switching workflow mode only swaps the system prompt
# and tool set, so the conversation continues seamlessly across a mode change.
#
# Tools whose dispatch spawns an isolated sub-agent subsession. When the main
# agent calls one, the turn's message prefix (including the spawning assistant
# message) is flushed to ``session.jsonl`` BEFORE dispatch, so a crash mid-
# subagent leaves the dangling ``tool_use`` on disk and the run can be resumed.
_SUBAGENT_SPAWNING_TOOLS = frozenset({"run_subagent", "run_author_critic_iteration"})

# Maximum length of a generated session title, in characters.
_MAX_TITLE_LEN = 60
# A usable title must name the subject in at least this many words. Weak titler
# models sometimes collapse to a single bare token (e.g. the implementation
# language, "python"); such answers are rejected and re-generated once.
_MIN_TITLE_WORDS = 2
_MAX_TITLE_WORDS = 8
# How much of a compaction summary travels in the ``context.compacted`` event as
# a feed-divider excerpt (the full summary lives in the session.jsonl marker).
_COMPACTION_EXCERPT_LEN = 280

# Every tool spec keyed by name — used to normalize each tool's output against
# its declared schema and to project the customer-visible detail rows.
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


def _history_attachment_links(attachments: object, session_dir: Path) -> list[dict[str, str]]:
    """Resolve a persisted message's attachment links for the client feed.

    Each ``{"name", "stored"}`` link is turned into ``{"name", "path"}`` with an
    absolute path to the session's stored copy, so the WebView chip opens the
    durable snapshot regardless of what happened to the original file.
    """
    if not isinstance(attachments, list):
        return []
    links: list[dict[str, str]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        stored = str(att.get("stored", ""))
        if not stored:
            continue
        links.append(
            {"name": str(att.get("name", "attachment")), "path": str(session_dir / stored)}
        )
    return links


class _EngineServices:
    """Adapts the engine's operations to the tools ``EngineServices`` protocol.

    Every engine-side action a tool can trigger — spawning sub-agents, rolling
    back, promoting a completed artifact, disabling autonomous mode, and
    pushing client updates — is funnelled through this single adapter. It lets
    the tools depend only on the protocol declared in :mod:`kodo.tools` while
    agent loading and the LLM tool-loop stay in the engine. The engine builds
    one instance and injects it into every per-run :class:`ToolDispatcher`.
    """

    def __init__(
        self,
        *,
        run_subagent: Callable[[str, str, dict[str, object]], Awaitable[dict[str, object]]],
        run_author_critic: Callable[
            [str, str, str, list[str], list[str]], Awaitable[dict[str, object]]
        ],
        rollback: Callable[[str], Awaitable[None]],
        complete_artifact: Callable[[str], Awaitable[None]],
        disable_autonomous: Callable[[], Awaitable[None]],
        post_update: Callable[[str], Awaitable[None]],
    ) -> None:
        self.__run_subagent = run_subagent
        self.__run_author_critic = run_author_critic
        self.__rollback = rollback
        self.__complete_artifact = complete_artifact
        self.__disable_autonomous = disable_autonomous
        self.__post_update = post_update

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Delegate to the engine's caller-gated sub-agent spawn."""
        return await self.__run_subagent(caller, name, task_input)

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        """Delegate to the engine's caller-gated Author/Critic round."""
        return await self.__run_author_critic(
            caller, author_name, critic_name, input_artifact_ids, for_revision_artifact_ids
        )

    async def rollback(self, target_sha: str) -> None:
        """Delegate to the engine's ``__run_rollback``."""
        await self.__rollback(target_sha)

    async def complete_artifact(self, artifact_id: str) -> None:
        """Delegate to the engine's ``__complete_artifact``."""
        await self.__complete_artifact(artifact_id)

    async def disable_autonomous_mode(self) -> None:
        """Delegate to the engine's ``__disable_autonomous``."""
        await self.__disable_autonomous()

    async def post_update(self, message: str) -> None:
        """Delegate to the engine's ``__post_update``."""
        await self.__post_update(message)


class WorkflowEngine:
    """Single-worker runtime engine hosting the Guide session.

    Args:
        sink: Message sink for sending events to the connected client.
        gate: Gate orchestrator for approval and question prompts.
        key_provider: Provider for cloud API keys.
        get_settings: Callable returning the current merged settings dict.
        transient: Append-only JSONL session store.
        layout: Project filesystem layout.
        registry: Loaded subagent file registry.
        checkpoints: Mirror checkpoint manager.
    """

    __sink: MessageSink
    __gate: GateOrchestrator
    __key_provider: ApiKeyProvider
    __get_settings: Callable[[], dict[str, object]]
    __transient: TransientStore
    __workspace_layout: WorkspaceLayout
    __session_workspace: SessionWorkspace
    __gateway: LLMGateway
    __layout: ProjectLayout | None
    __registry: AgentRegistry
    __checkpoints: CheckpointManager | None
    __current_project: dict[str, str] | None
    __workspace: Workspace
    __queue: asyncio.Queue[dict[str, object]]
    __session: SessionState
    __index: ProjectIndex
    __services: _EngineServices
    __worker: asyncio.Task[None] | None
    __cumulative_usd: float
    __main_messages: list[Message]
    # Measured token size of the live main context (last entry-agent turn's
    # input + cache + output, or an estimate immediately after a compaction).
    __context_tokens: int
    # True while a compaction run is in flight (disables the manual trigger and
    # drives the "Compacting context…" indicator).
    __compacting: bool
    __orch_session_id: str
    __current_vendor: str | None
    # Registry key of the model the entry agent last ran on (the model that owns
    # the live main context). Used to detect a model switch and, when the new
    # model has a smaller context window, compact with this (old) model first.
    __active_model_key: str | None
    __replay_subsessions: list[dict[str, object]] | None
    __resume_subsession_pending: bool

    def __init__(
        self,
        sink: MessageSink,
        gate: GateOrchestrator,
        key_provider: ApiKeyProvider,
        get_settings: Callable[[], dict[str, object]],
        transient: TransientStore,
        workspace_layout: WorkspaceLayout,
        registry: AgentRegistry,
        gateway: LLMGateway,
        session_workspace: SessionWorkspace | None = None,
    ) -> None:
        """Initialise the runtime engine.

        The engine is workspace-scoped.  Project-level collaborators (the
        ``ProjectLayout``, ``CheckpointManager``, and artifact ``Workspace``) are
        built lazily in :meth:`bind_project` when the current project is selected
        for Guided mode; until then ``self.__layout`` is ``None`` and the
        placeholder ``Workspace`` (rooted at the physical root) is never used,
        because Problem Solver tools touch only the filesystem via the resolver.

        Args:
            sink (MessageSink): Sends outbound envelopes to the client.
            gate (GateOrchestrator): Handles approval / question gates.
            key_provider (ApiKeyProvider): Retrieves cloud API keys on demand.
            get_settings (Callable): Returns fresh merged settings on each call.
            transient (TransientStore): Append-only JSONL session store
                (workspace-tier ``.kodo-workspace/sessions/``).
            workspace_layout (WorkspaceLayout): Workspace-tier filesystem layout
                + logical-root folder map.
            registry (AgentRegistry): Loaded subagent file registry.
        """
        self.__sink = sink
        self.__gate = gate
        self.__key_provider = key_provider
        self.__get_settings = get_settings
        self.__transient = transient
        self.__workspace_layout = workspace_layout
        self.__session_workspace = session_workspace or SessionWorkspace()
        self.__gateway = gateway
        self.__registry = registry
        self.__layout: ProjectLayout | None = None
        self.__checkpoints: CheckpointManager | None = None
        self.__current_project: dict[str, str] | None = None
        self.__index = ProjectIndex()
        # Placeholder workspace at the physical root — replaced by the real,
        # project-rooted one in bind_project(); never used before then (no
        # artifact tool runs without a bound project).
        self.__workspace = Workspace(self.__session_workspace.physical_root, self.__index)
        self.__queue = asyncio.Queue()
        self.__session = SessionState()
        self.__worker = None
        self.__cumulative_usd = 0.0
        self.__main_messages = []
        self.__context_tokens = 0
        self.__compacting = False
        self.__orch_session_id = ""
        self.__current_vendor = None
        self.__active_model_key = None
        self.__replay_subsessions = None
        self.__resume_subsession_pending = False
        self.__toolchain: ToolchainPlugin | None = None
        self.__services = _EngineServices(
            run_subagent=self.__run_subagent,
            run_author_critic=self.__run_author_critic_iteration,
            rollback=self.__run_rollback,
            complete_artifact=self.__complete_artifact,
            disable_autonomous=self.__disable_autonomous,
            post_update=self.__post_update,
        )

    @property
    def session(self) -> SessionState:
        """Current session state snapshot."""
        return self.__session

    @property
    def gate(self) -> GateOrchestrator:
        """Gate orchestrator (needed by the approval handler in _app.py)."""
        return self.__gate

    @property
    def session_id(self) -> str:
        """Identifier of the active Guide session."""
        return self.__orch_session_id

    @property
    def session_name(self) -> str:
        """Human-readable name of the active session (from ``meta.json``)."""
        return self.__transient.session_name

    @property
    def current_project(self) -> dict[str, str] | None:
        """The session's locked current project ``{root, name}``, or ``None``.

        Bound once (lazily) for Guided mode and immutable for the session.
        ``None`` while only Problem Solver has run.
        """
        return self.__current_project

    def __require_layout(self) -> ProjectLayout:
        """Return the bound project layout, or raise if none is bound.

        Guards the Guided-only code paths (rollback, promotion, toolchain) that
        run only after :meth:`bind_project` has set ``self.__layout``.
        """
        if self.__layout is None:
            raise RuntimeError(
                "No current project is bound — Guided mode requires a project selection."
            )
        return self.__layout

    def __require_checkpoints(self) -> CheckpointManager:
        """Return the bound checkpoint manager, or raise if none is bound."""
        if self.__checkpoints is None:
            raise RuntimeError(
                "No current project is bound — Guided mode requires a project selection."
            )
        return self.__checkpoints

    async def start(self, session_id: str, resumed: bool) -> None:
        """Attach the given session and start the worker.

        The session id + resumed flag are supplied by the ``SessionManager``
        (client-driven: ``hello`` creates a new id or resumes an existing one).
        The project is *not* bound here — that happens lazily in
        :meth:`bind_project` when the user first runs Guided mode.  If the
        resumed session already recorded a current project, it is re-bound now so
        crash-resume of a mid-subagent Guided turn still works.

        Args:
            session_id (str): Session identifier to attach.
            resumed (bool): ``True`` if an existing session dir was found.
        """
        self.__orch_session_id = session_id
        self.__clear_llm_request_logs()
        self.__transient.attach_session(session_id, resumed)

        if resumed:
            self.__main_messages = self.__load_main_messages()
            # Seed the gauge from an estimate until the first resumed turn yields
            # a measured token count.
            self.__context_tokens = self.__estimate_tokens(self.__main_messages)
            # Restore per-session prefs so a resumed tab keeps its own mode
            # (the window no longer re-syncs a single global value on connect).
            self.__session.autonomous = self.__transient.autonomous
            self.__session.workflow_mode = self.__transient.workflow_mode
            self.__session.edit_control = self.__transient.edit_control
            self.__session.command_control = self.__transient.command_control
            persisted = self.__transient.current_project
            if persisted is not None:
                await self.__bind_project(persisted["root"], persisted["name"], emit=False)
                # A main turn interrupted while a sub-agent held the floor leaves
                # a dangling assistant ``tool_use`` (the spawning call) with no
                # following ``tool_result``. Resume needs the bound project's
                # workspace/index, so it is gated on a successful bind above.
                if self.__layout is not None and self.__has_dangling_tool_use():
                    self.__resume_subsession_pending = True
            if not self.__resume_subsession_pending:
                pending = self.__transient.pending_prompt
                if pending is not None:
                    asyncio.create_task(
                        self.__resume_pending_prompt(pending), name="kodo-resume-prompt"
                    )

        self.__worker = asyncio.create_task(self.__run_worker(), name="kodo-worker")
        _log.info(
            "Runtime worker started (guide_session=%s resumed=%s messages=%d "
            "project=%s resume_subsession=%s)",
            self.__orch_session_id,
            resumed,
            len(self.__main_messages),
            self.__current_project["name"] if self.__current_project else None,
            self.__resume_subsession_pending,
        )

    async def handle_workspace_folders(self, physical_root: str, folders: dict[str, str]) -> None:
        """Update the logical-root folder map (pushed over the WS protocol).

        Args:
            physical_root (str): The physical workspace root (informational —
                the server is already launched against it).
            folders (dict[str, str]): Logical name → physical path for every
                open VS Code workspace folder.
        """
        if physical_root:
            self.__session_workspace.set_physical_root(Path(physical_root))
        self.__session_workspace.set_folders({k: Path(v) for k, v in folders.items()})
        _log.info(
            "Workspace folder map updated (physical_root=%s): %s",
            physical_root,
            sorted(folders),
        )

    async def bind_project(self, root: str, name: str) -> None:
        """Bind the session's current project for Guided mode (idempotent).

        Immutable for the session: a request to bind a *different* project once
        one is set is rejected with an error event.

        Args:
            root (str): Absolute path to the project root (contains ``kodo.md``).
            name (str): Logical workspace-folder name for display.
        """
        if self.__current_project is not None:
            if self.__current_project["root"] != str(Path(root).resolve()):
                await self.__emit_error(
                    "The current project is fixed for this session and cannot be changed.",
                    recoverable=True,
                )
            return
        await self.__bind_project(root, name, emit=True)

    async def __bind_project(self, root: str, name: str, *, emit: bool) -> None:
        """Construct the project-tier collaborators and rebuild the index.

        Args:
            root (str): Project root path.
            name (str): Logical workspace-folder name.
            emit (bool): Emit ``EVT_PROJECT_BOUND`` and persist the choice
                (skipped when re-binding a resumed session, which already has it).
        """
        project_root = Path(root).resolve()
        layout = ProjectLayout(project_root)
        try:
            layout.validate()
        except ProjectLayoutError as exc:
            _log.error("Cannot bind project %s: %s", project_root, exc)
            await self.__emit_error(str(exc), recoverable=True)
            return

        self.__layout = layout
        self.__checkpoints = CheckpointManager(layout)
        await self.__checkpoints.ensure_initialized()

        self.__index = ProjectBootstrap(
            mirror_dir=layout.checkpoints_dir,
            workspace_dir=layout.workspace_dir,
            sessions_dir=self.__workspace_layout.sessions_dir,
        ).build_index()
        self.__workspace = Workspace(layout.root, self.__index)
        self.__toolchain = None
        self.__current_project = {"root": str(project_root), "name": name}

        if emit:
            self.__transient.update(current_project=self.__current_project)
            await self.__sink.send(
                Envelope.make_event(EVT_PROJECT_BOUND, dict(self.__current_project))
            )
        _log.info("Current project bound: %s (%s)", name, project_root)

    async def __resume_pending_prompt(self, pending: dict[str, object]) -> None:
        """Re-surface a ``prompt.question``/``prompt.approval`` lost to a server restart.

        The original LLM turn that issued the prompt was never persisted (it
        only lands in ``session.jsonl`` once the turn completes), so it
        cannot be resumed in place. Instead, re-fire the same prompt to the
        client and feed the user's answer back to the Guide as a new
        input describing what was asked and how it was answered.
        """
        self.__session.phase = "awaiting_user"
        await self.__emit_state()

        kind = pending.get("kind")
        try:
            if kind == "question":
                question = str(pending.get("question", ""))
                mode = str(pending.get("mode", "free_text"))
                raw_choices = pending.get("choices")
                choices: list[dict[str, str]] | None = None
                if isinstance(raw_choices, list):
                    choices = [
                        {"key": str(c.get("key", "")), "label": str(c.get("label", ""))}
                        for c in raw_choices
                        if isinstance(c, dict)
                    ]
                response = await self.__gate.fire_question(question, mode, choices)
                answer = response.choice_key or response.answer_text
                text = (
                    f'(Resuming after restart) You previously asked the user: "{question}". '
                    f"Their answer: {answer}"
                )
            elif kind == "approval":
                gate_type = str(pending.get("gate_type", ""))
                artifact_id = pending.get("artifact_id")
                summary = str(pending.get("summary", ""))
                approval = await self.__gate.fire_approval(
                    gate_type,
                    artifact_id=artifact_id if isinstance(artifact_id, str) else None,
                    summary=summary,
                )
                text = f'(Resuming after restart) You previously requested approval: "{summary}". '
                text += f"The user responded: {approval.action}"
                if approval.feedback:
                    text += f" — feedback: {approval.feedback}"
            else:
                return
        except Exception:
            _log.exception("Failed to resume pending prompt")
            return

        await self.__queue.put({"text": text, "request_id": ""})

    @property
    def __llm_logs_dir(self) -> Path:
        """Per-session LLM request/response log dir (sessions never share one).

        ``~/.kodo/logs/llm_requests/<session_id>/`` — keeps concurrent sessions'
        logs isolated and makes the on-start clear scoped to this session only.
        """
        return self.__workspace_layout.llm_requests_dir / (self.__orch_session_id or "unbound")

    def __clear_llm_request_logs(self) -> None:
        """Remove this session's previously logged LLM requests/responses."""
        logs_dir = self.__llm_logs_dir
        if not logs_dir.is_dir():
            return
        for entry in logs_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

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
        """Enqueue a user prompt for the Guide to process.

        Any attachment control line the extension prepended (see
        :mod:`kodo.runtime._attachments`) is parsed off here so the queued/
        persisted prompt is the user's *clean* text and the attachment source
        paths travel alongside it. The files themselves are read, copied into
        the session, and injected into the LLM context later, when the prompt
        actually reaches its entry agent.

        Args:
            text: The user's prompt text (possibly with a leading control line).
            request_id: Envelope ID of the originating request.
        """
        clean_text, attachment_paths = parse_attachment_marker(text)
        self.__transient.update(prompt=clean_text)
        await self.__queue.put(
            {"text": clean_text, "attachments": attachment_paths, "request_id": request_id}
        )

    async def handle_mode_set(self, autonomous: bool) -> None:
        """Toggle autonomous mode.

        Args:
            autonomous: New autonomous mode value.
        """
        self.__session.autonomous = autonomous
        self.__transient.update(autonomous=autonomous)
        await self.__emit_state()

    async def handle_workflow_set(self, mode: str) -> None:
        """Select the top-level workflow that drives user prompts.

        Args:
            mode: ``"guided"`` (Guide + full Kodo pipeline) or
                ``"problem_solving"`` (the standalone Problem Solver agent).
                Unknown values fall back to ``"guided"``.
        """
        self.__session.workflow_mode = mode if mode == "problem_solving" else "guided"
        self.__transient.update(workflow_mode=self.__session.workflow_mode)
        await self.__emit_state()

    async def handle_edit_control_set(self, value: str) -> None:
        """Set the Edit Control posture.

        Unlike the frozen toggles this is **never** frozen: the client owns the
        value (forcing ``"allow_all"`` while Autonomous is in effect, restoring
        the user's pick otherwise) and the engine simply mirrors whatever it last
        sent, so the stored value is always exactly what the UI shows. State
        tracking only — enforcement is deferred to the M4 security layer.

        Args:
            value: ``"review_all"`` | ``"allow_all"`` | ``"smart"``. Unknown
                values fall back to ``"smart"``.
        """
        self.__session.edit_control = (
            value if value in ("review_all", "allow_all", "smart") else "smart"
        )
        self.__transient.update(edit_control=self.__session.edit_control)
        await self.__emit_state()

    async def handle_command_control_set(self, value: str) -> None:
        """Set the Command Control posture.

        Mirrors the client exactly, same as :meth:`handle_edit_control_set`
        (the client forces ``"permissive"`` while Autonomous is in effect).
        State tracking only — enforcement deferred to M4.

        Args:
            value: ``"defensive"`` | ``"permissive"`` | ``"smart"``. Unknown
                values fall back to ``"smart"``.
        """
        self.__session.command_control = (
            value if value in ("defensive", "permissive", "smart") else "smart"
        )
        self.__transient.update(command_control=self.__session.command_control)
        await self.__emit_state()

    def __freeze_effective_modes(self) -> None:
        """Snapshot the two frozen toggles into their ``effective_*`` twins.

        Called once per prompt at dequeue (and on sub-session resume) so the
        guide and every sub-agent it spawns see one consistent value for the
        whole turn even if the user flips a toggle mid-run. Only ``autonomous``
        and ``workflow_mode`` are frozen — ``edit_control``/``command_control``
        are deliberately never frozen (the client owns them and may change them
        any time it is not locked by Autonomous mode).
        """
        self.__session.effective_autonomous = self.__session.autonomous
        self.__session.effective_workflow_mode = self.__session.workflow_mode

    async def handle_compact_now(self) -> None:
        """Enqueue a manual context-compaction request.

        Compaction mutates ``__main_messages``, so it is funnelled through the
        same single-consumer worker queue as prompts rather than run inline on
        the connection handler. The worker honours it only when the entry agent
        is idle and there is context to compact (see :meth:`__run_manual_compaction`);
        a request that arrives mid-run simply waits its turn and is re-checked.
        """
        await self.__queue.put({"kind": "compact"})

    async def handle_config_changed(self) -> None:
        """React to a window-global settings change (e.g. a model switch).

        The model selection lives in the singleton's ``~/.kodo/settings.json`` and
        is read fresh per turn, so a switch normally takes effect lazily. This
        hook (fired by the ``config.reload`` handler for every live session) lets
        the engine act *immediately*: if the new model's context window is smaller
        than the live context, it compacts using the *current* model before the
        switch takes effect. It is funnelled through the worker queue so it never
        races an in-flight turn or another compaction.
        """
        await self.__queue.put({"kind": "config_changed"})

    # ------------------------------------------------------------------
    # Plugin resolution — per-dispatch, reads fresh settings each time
    # ------------------------------------------------------------------

    def __resolve_model_key(self, capability: str) -> str:
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
        settings = self.__get_settings()
        mode = str(settings.get("mode", "cloud"))
        models_map = settings.get("models", {})
        if not isinstance(models_map, dict):
            models_map = {}
        if mode == "local":
            return str(models_map.get("local", "llamacpp-qwen36-27b"))
        return str(models_map.get(capability, models_map.get("medium", capability)))

    async def __resolve_plugin(
        self, capability: str, force_model_key: str | None = None
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
        model_key = force_model_key or self.__resolve_model_key(capability)

        registry = get_llm_registry()
        entry = registry.get(model_key)
        module = entry.module if entry is not None else "kodo.llms.anthropic"

        if module == "kodo.llms.llamacpp":
            self.__current_vendor = None
            plugin: LLMPlugin = LlamaPlugin(sink=self.__sink, kodo_dir=kodo_user_dir())
            routing = LLMRouting(residence="local")
            return LoggingLLMPlugin(plugin, self.__llm_logs_dir), model_key, routing

        model_id = entry.model_id if entry is not None else model_key
        vendor = module.rsplit(".", 1)[-1]
        self.__current_vendor = vendor

        key_result: ApiKey = await self.__key_provider.get_key(vendor)
        if key_result.error:
            raise RuntimeError(f"API key request rejected: {key_result.error}")

        plugin = ClaudePlugin(api_key=key_result.api_key)
        routing = LLMRouting(residence="cloud", vendor=vendor)
        return LoggingLLMPlugin(plugin, self.__llm_logs_dir), model_id, routing

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def __run_worker(self) -> None:
        # Resume an interrupted sub-agent before accepting any queued prompt, so
        # the resume and a new prompt never drive __main_messages concurrently.
        if self.__resume_subsession_pending:
            self.__resume_subsession_pending = False
            self.__freeze_effective_modes()
            try:
                await self.__resume_main_turn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception("Failed to resume interrupted subsession: %s", exc)
                self.__replay_subsessions = None
                self.__session.agent = None
                await self.__emit_error(str(exc), recoverable=True)
                await self.__emit_state()

        while True:
            task = await self.__queue.get()
            if task.get("kind") == "compact":
                try:
                    await self.__run_manual_compaction()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.exception("Manual compaction failed: %s", exc)
                    await self.__emit_error(f"Compaction failed: {exc}", recoverable=True)
                finally:
                    self.__queue.task_done()
                continue
            if task.get("kind") == "config_changed":
                try:
                    await self.__handle_config_changed()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.exception("Config-change handling failed: %s", exc)
                finally:
                    self.__queue.task_done()
                continue
            text = str(task.get("text", ""))
            raw_attachments = task.get("attachments", [])
            attachments = (
                [str(p) for p in raw_attachments] if isinstance(raw_attachments, list) else []
            )
            # Freeze every mode toggle for the whole prompt (guide + every
            # sub-agent it spawns). A toggle the user flips mid-prompt updates
            # the user-facing value but takes effect only when the next prompt
            # is dequeued here, so the in-flight prompt stays consistent end to
            # end and the client can tell "in effect" from "queued".
            self.__freeze_effective_modes()
            try:
                # Name the session from its first prompt, before that prompt
                # reaches the main agent. The titler session is invisible: it
                # streams nothing to the client and only its cost is folded in.
                await self.__maybe_generate_session_title(text)

                # The entry agent is chosen per prompt from the current
                # workflow mode: Problem Solver for "problem_solving", the
                # Guide (full Kodo pipeline) for "guided".
                if self.__session.workflow_mode == "problem_solving":
                    if self.__agent_available(_PROBLEM_SOLVER_AGENT_NAME):
                        await self.__run_problem_solver_with_input(text, attachments)
                    else:
                        await self.__handle_input_no_agent(_PROBLEM_SOLVER_AGENT_NAME, text)
                elif self.__layout is None:
                    # Guided mode requires a bound project. The extension forces
                    # the picker before sending the first Guided prompt, so this
                    # is a safety net for an out-of-band prompt.
                    self.__session.agent = None
                    await self.__emit_error(
                        "Select a project before running Guided mode.", recoverable=True
                    )
                    await self.__emit_state()
                elif self.__agent_available(_GUIDE_AGENT_NAME):
                    await self.__run_guide_with_input(text, attachments)
                else:
                    await self.__handle_input_no_agent(_GUIDE_AGENT_NAME, text)

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

    def __agent_available(self, name: str) -> bool:
        try:
            self.__registry.get(name)
            return True
        except AgentLoadError:
            return False

    async def __handle_input_no_agent(self, name: str, text: str) -> None:
        self.__session.phase = "running"
        await self.__emit_state()
        _log.warning(
            "Prompt received (len=%d) — entry agent %r not found; "
            "add subagent_%s.md to register one",
            len(text),
            name,
            name,
        )
        self.__session.phase = "intake"
        await self.__emit_state()

    # ------------------------------------------------------------------
    # Session titling (engine-driven, invisible to the user)
    # ------------------------------------------------------------------

    async def __maybe_generate_session_title(self, text: str) -> None:
        """Name the session from its first prompt, if it is still unnamed.

        Runs the ``session_titler`` sub-agent directly (never via the tool
        surface), persists the result to ``meta.json``, and pushes it to the
        client so the editor tab can be renamed. The titler session is silent:
        no streaming, state, or agent events are emitted — only its USD cost is
        folded into the running session total. Any failure is swallowed so the
        user's prompt is never blocked by titling.
        """
        if not text.strip():
            return
        if self.__transient.is_session_named:
            return
        if not self.__agent_available(_SESSION_TITLER_AGENT_NAME):
            return

        # Tell the client a (silent) naming call is in flight so it can show a
        # "Naming session …" indicator — otherwise the titling round-trip looks
        # like an unexplained stall before the main agent starts streaming.
        await self.__emit_session_naming(True)
        try:
            title = await self.__generate_session_title(text)
        except Exception:
            _log.exception("Session title generation failed; leaving session unnamed")
            return
        finally:
            await self.__emit_session_naming(False)

        if not title:
            return

        self.__transient.set_session_name(title)
        await self.__sink.send(
            Envelope.make_event(
                EVT_SESSION_NAME,
                {"session_id": self.__orch_session_id, "name": title},
            )
        )
        _log.info("Session %s named %r", self.__orch_session_id, title)

    async def __generate_session_title(self, text: str) -> str | None:
        """Run a silent LLM call to produce a session title from *text*.

        Does not forward any stream/thinking events to the client; only the
        title text is collected. The call's USD cost is added to the running
        cumulative total and pushed as a cost-only ``usage.update`` (no
        ``last_call_tokens``, so it adds no entry to the session feed).

        Weak titler models occasionally ignore the rules and emit a degenerate
        answer (a single bare token such as the implementation language). The
        sanitized result is validated against :meth:`__is_acceptable_title`; on
        rejection we re-prompt once with a corrective nudge appended to the
        conversation, then give up (returning ``None`` leaves the session
        unnamed so the next prompt can try again).
        """
        agent = self.__registry.get(_SESSION_TITLER_AGENT_NAME)
        plugin, model_id, routing = await self.__resolve_plugin(agent.capability)

        messages: list[Message] = [Message(role="user", content=text)]
        for _attempt in range(2):
            raw = await self.__run_titler_turn(routing, plugin, model_id, agent, messages)
            title = self.__sanitize_title(raw)
            if self.__is_acceptable_title(title):
                return title
            # Show the model its own rejected answer and ask for a real title.
            messages.append(Message(role="assistant", content=raw))
            messages.append(
                Message(
                    role="user",
                    content=(
                        "That is not a usable title. It must be 2 to 6 words in "
                        "Title Case naming the subject of the request — not the "
                        "programming language, not a single bare word. Output "
                        "only the corrected title."
                    ),
                )
            )

        # Both attempts failed validation; better to leave it unnamed than to
        # commit a degenerate title.
        _log.info("Session titler produced no acceptable title after retry")
        return None

    async def __run_silent_return_turn(
        self,
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
        async for event in self.__gateway.stream_query(
            routing=routing,
            plugin=plugin,
            sink=self.__sink,
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
            self.__cumulative_usd += turn_end.usage.usd_cost
            await self.__emit_cost_only()

        return result, "".join(text_parts)

    async def __run_titler_turn(
        self,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
    ) -> str:
        """One silent titler LLM turn; returns the title (via return_result) or text."""
        result, text = await self.__run_silent_return_turn(
            routing, plugin, model_id, agent, messages
        )
        if result is not None:
            title = result.get("title")
            if isinstance(title, str) and title.strip():
                return title
        return text

    @staticmethod
    def __is_acceptable_title(title: str | None) -> bool:
        """Reject degenerate titler output that slipped past sanitizing.

        Enforces the word-count band the prompt asks for (a single bare token
        such as ``python`` is the canonical failure). Title Case, length, and
        formatting are already handled by :meth:`__sanitize_title`.
        """
        if not title:
            return False
        words = title.split()
        return _MIN_TITLE_WORDS <= len(words) <= _MAX_TITLE_WORDS

    @staticmethod
    def __sanitize_title(raw: str) -> str | None:
        """Reduce raw model output to a single clean title line.

        Takes the first non-empty line, strips wrapping quotes and a leading
        ``Title:`` label, collapses whitespace, and clamps the length. Returns
        ``None`` if nothing usable remains.
        """
        line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        if not line:
            return None
        if ":" in line:
            head, _, tail = line.partition(":")
            if head.strip().lower() in ("title", "session", "session title"):
                line = tail.strip()
        line = line.strip().strip("\"'`").strip()
        line = " ".join(line.split())
        if not line:
            return None
        if len(line) > _MAX_TITLE_LEN:
            line = line[:_MAX_TITLE_LEN].rstrip()
        return line or None

    # ------------------------------------------------------------------
    # Context compaction (in-place; see doc/STATE_AND_LIFECYCLE.md §4.5)
    # ------------------------------------------------------------------

    def __entry_agent_name(self) -> str:
        """The top-level entry agent for the current workflow mode."""
        if self.__session.workflow_mode == "problem_solving":
            return _PROBLEM_SOLVER_AGENT_NAME
        return _GUIDE_AGENT_NAME

    def __entry_capability(self) -> str:
        """Capability tier of the current entry agent (defaults to medium)."""
        try:
            return self.__registry.get(self.__entry_agent_name()).capability
        except Exception:  # noqa: BLE001 — unregistered agent → safe default
            return "medium"

    def __context_limit(self) -> int:
        """Token budget for the main context = current model's context window.

        Resolved from the entry-agent model selected in settings (see
        :meth:`__resolve_model_key`) via the per-model ``context_window`` in the
        LLM registry. This is *not* session-specific: switching the model mid-
        session changes the limit, and the gauge/auto-compaction threshold follow
        it on the next stats emission (or immediately, via
        :meth:`handle_config_changed`).
        """
        return get_context_window(self.__resolve_model_key(self.__entry_capability()))

    def __can_compact(self) -> bool:
        """True when a manual compaction would be honoured right now.

        Mirrors the worker-side guard so the client can enable/disable its
        "Compact now" button from the pushed stats: the entry agent must be idle
        (the last turn ended and no new one started), a compaction must not be in
        flight, there must be measured context, and the ``compactor`` agent must
        be registered.
        """
        return (
            self.__session.phase == "awaiting_user"
            and not self.__compacting
            and bool(self.__main_messages)
            and self.__context_tokens > 0
            and self.__agent_available(_COMPACTOR_AGENT_NAME)
        )

    async def __maybe_auto_compact(self) -> None:
        """Auto-compact when the just-measured context crosses the threshold.

        Called at the end of every main entry-agent turn (after the LLM has
        responded). One pass is enough — compaction collapses the context far
        below the threshold — so this never loops.
        """
        if self.__compacting:
            return
        limit = self.__context_limit()
        if self.__context_tokens >= _COMPACTION_THRESHOLD * limit:
            _log.info(
                "Context at %d/%d tokens (≥%d%%) — auto-compacting",
                self.__context_tokens,
                limit,
                int(_COMPACTION_THRESHOLD * 100),
            )
            await self.__run_compaction("auto")

    async def __run_manual_compaction(self) -> None:
        """Honour a queued ``compact.now`` request, if currently compactable."""
        if not self.__can_compact():
            _log.info("compact.now ignored — not in a compactable state")
            return
        await self.__run_compaction("manual")

    async def __handle_config_changed(self) -> None:
        """Worker-side handler for a settings change (see :meth:`handle_config_changed`).

        Detects whether the entry-agent model changed. If it shrank below the live
        context size, compact with the *outgoing* model first (so the switch only
        takes effect on a context that fits the new window); then record the new
        model and re-emit the context gauge (the limit may have moved either way).
        """
        new_key = self.__resolve_model_key(self.__entry_capability())
        old_key = self.__active_model_key
        if old_key is not None and new_key != old_key:
            new_limit = get_context_window(new_key)
            if self.__context_tokens > new_limit and self.__can_compact():
                _log.info(
                    "Model switch %s → %s shrinks context window to %d < %d live tokens "
                    "— compacting with the outgoing model first",
                    old_key,
                    new_key,
                    new_limit,
                    self.__context_tokens,
                )
                await self.__run_compaction("model_switch", force_model_key=old_key)
        self.__active_model_key = new_key
        await self.__emit_context_stats()

    async def __run_compaction(self, reason: str, force_model_key: str | None = None) -> None:
        """Summarise the live main context with the compactor and reset it.

        The full ``session.jsonl`` is preserved as audit history: this appends a
        ``compaction`` marker carrying the summary, then resets the live LLM
        context to a single synthetic block holding that summary. On resume,
        :meth:`__load_main_messages` rebuilds the context from the latest marker
        onward (summary + any later messages), so the pre-compaction transcript
        is never resent to the model. ``reason`` is ``"auto"``, ``"manual"``, or
        ``"model_switch"``.

        Args:
            reason: Why the compaction ran (recorded on the marker).
            force_model_key: When set, the summarisation call runs on this exact
                model rather than the one currently selected in settings — used
                for a model switch so the *outgoing* model compacts before the
                switch takes effect.
        """
        if not self.__main_messages or not self.__agent_available(_COMPACTOR_AGENT_NAME):
            return

        self.__compacting = True
        await self.__emit_context_compacting(True)
        await self.__emit_context_stats()  # reflect can_compact=False while running
        tokens_before = self.__context_tokens
        summary: str | None = None
        try:
            summary = await self.__generate_compaction_summary(force_model_key=force_model_key)
        except Exception:
            _log.exception("Compaction summary generation failed; context unchanged")
        finally:
            self.__compacting = False
            await self.__emit_context_compacting(False)

        if not summary:
            await self.__emit_context_stats()
            return

        context_msg = self.__compaction_context_message(summary)
        tokens_after = self.__estimate_tokens([context_msg])
        self.__transient.append_marker(
            {
                "type": "compaction",
                "summary": summary,
                "reason": reason,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "ts": datetime.now(tz=UTC).isoformat(),
            }
        )
        self.__main_messages = [context_msg]
        self.__context_tokens = tokens_after

        await self.__sink.send(
            Envelope.make_event(
                EVT_CONTEXT_COMPACTED,
                {
                    "summary_excerpt": summary[:_COMPACTION_EXCERPT_LEN],
                    # Full summary = the exact context the conversation continues
                    # from; the client reveals it in the collapsible divider.
                    "summary": summary,
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                },
            )
        )
        await self.__emit_context_stats()
        _log.info("Context compacted (%s): ~%d → ~%d tokens", reason, tokens_before, tokens_after)

    async def __generate_compaction_summary(self, force_model_key: str | None = None) -> str | None:
        """Run one silent LLM call producing a compact briefing of the context.

        The current main message list is rendered to a plain-text transcript and
        handed to the ``compactor`` sub-agent as a single user message; the model
        gets no tools. Like the titler, this streams nothing to the feed — only
        the summary text is collected and the call's USD cost folded into the
        running total.

        Args:
            force_model_key: When set, run on this exact model instead of the one
                resolved from current settings (see :meth:`__run_compaction`).
        """
        agent = self.__registry.get(_COMPACTOR_AGENT_NAME)
        plugin, model_id, routing = await self.__resolve_plugin(
            agent.capability, force_model_key=force_model_key
        )
        transcript = self.__render_transcript(self.__main_messages)
        messages: list[Message] = [
            Message(role="user", content=f"Conversation transcript to compact:\n\n{transcript}")
        ]
        result, text = await self.__run_silent_return_turn(
            routing, plugin, model_id, agent, messages
        )
        if result is not None:
            summary = result.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        return text.strip() or None

    @staticmethod
    def __compaction_context_message(summary: str) -> Message:
        """Build the synthetic user message that replaces a compacted context.

        Used both when compaction happens live and when a resumed session is
        rebuilt from its latest ``compaction`` marker, so the in-memory context
        is identical in both paths.
        """
        return Message(
            role="user",
            content=(
                "The conversation so far has been compacted to stay within the "
                "context limit. The following is a summary of everything that "
                "happened before this point; treat it as your working memory and "
                "continue seamlessly from it:\n\n" + summary
            ),
        )

    @staticmethod
    def __render_transcript(messages: list[Message]) -> str:
        """Flatten a message list to a plain-text transcript for summarisation.

        Tool-use/`tool_result`/thinking blocks are rendered as labelled lines so
        the compactor sees the whole exchange as data without needing the tool
        schemas that a structured replay would require.
        """
        out: list[str] = []
        for msg in messages:
            content = msg.content
            header = f"## {msg.role.upper()}"
            if isinstance(content, str):
                out.append(f"{header}\n{content}")
                continue
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype == "thinking":
                    parts.append(f"[thinking] {block.get('thinking', '')}")
                elif btype == "tool_use":
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                    parts.append(f"[tool_use {block.get('name', '')}] {args}")
                elif btype == "tool_result":
                    raw = block.get("content")
                    body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                    parts.append(f"[tool_result] {body}")
            out.append(f"{header}\n" + "\n".join(parts))
        return "\n\n".join(out)

    @staticmethod
    def __estimate_tokens(messages: list[Message]) -> int:
        """Rough token estimate (~4 chars/token) for messages with no live usage.

        Used only to seed the gauge immediately after a compaction, before the
        next real turn supplies a measured count.
        """
        chars = 0
        for msg in messages:
            content = msg.content
            chars += (
                len(content)
                if isinstance(content, str)
                else len(json.dumps(content, ensure_ascii=False))
            )
        return max(1, chars // 4)

    # ------------------------------------------------------------------
    # Guide LLM loop
    # ------------------------------------------------------------------

    async def __run_guide_with_input(self, text: str, attachments: list[str] | None = None) -> None:
        await self.__run_entry_agent(_GUIDE_AGENT_NAME, text, attachments)

    async def __run_entry_agent(
        self, agent_name: str, text: str, attachments: list[str] | None = None
    ) -> None:
        """Drive a top-level entry agent (Guide or Problem Solver).

        Both entry agents share one agent-agnostic main message history
        (``__main_messages``) persisted to ``session.jsonl``; the only per-mode
        difference is the system prompt and tool set. The seed user prompt is
        persisted immediately; the agent's own turns persist incrementally
        through :meth:`__run_agent_turn` (the spawning-tool prefix is flushed
        before any sub-agent dispatch so an interrupted sub-agent can resume).

        Prompt attachments are resolved here: each source file is read, copied
        into the session, and *injected* into the in-memory user message (so the
        LLM sees the content), while ``session.jsonl`` persists only the clean
        prompt plus links to the stored copies — see :meth:`__store_attachments`.
        """
        agent = self.__registry.get(agent_name, self.__session.effective_autonomous)
        plugin, model_id, routing = await self.__resolve_plugin(agent.capability)
        # Remember the model that owns this main context, so a later model switch
        # can detect a shrink and compact with this model first.
        self.__active_model_key = self.__resolve_model_key(agent.capability)

        stored, errors = await self.__store_attachments(attachments or [])
        for message in errors:
            await self.__emit_error(message, recoverable=True)

        if text or stored:
            llm_text = inject_attachments(text, [(s["name"], s["content"]) for s in stored])
            self.__main_messages = self.__main_messages + [Message(role="user", content=llm_text)]
            self.__transient.append_message(
                "user",
                text,
                entry_agent=agent_name,
                attachments=[{"name": s["name"], "stored": s["stored"]} for s in stored],
            )
            # Always echo the authoritative stored set when the user staged
            # anything — even an empty set (every file failed validation) — so
            # the client retargets the optimistically-rendered chips to the
            # stored copies, or clears them.
            if attachments:
                await self.__sink.send(
                    Envelope.make_event(
                        EVT_USER_ATTACHMENTS,
                        {
                            "attachments": [
                                {
                                    "name": s["name"],
                                    "path": self.__transient.attachment_abs_path(s["stored"]),
                                }
                                for s in stored
                            ]
                        },
                    )
                )

        self.__session.phase = "running"
        self.__session.agent = agent_name
        await self.__emit_state()
        await self.__emit_agent_started(agent_name)

        dispatcher = self.__make_dispatcher(agent_name, self.__orch_session_id)
        stream_id = uuid.uuid4().hex
        self.__main_messages, _ = await self.__run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=self.__main_messages,
            tools=tools_for_agent(agent.tools),
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=agent_name,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=self.__persist_main_messages(agent_name),
            flush_before=_SUBAGENT_SPAWNING_TOOLS,
            track_context=True,
        )
        await self.__sink.send(Envelope.make_stream_end(stream_id))
        await self.__emit_agent_finished(agent_name)

        if self.__session.phase != "done":
            self.__session.phase = "awaiting_user"
        self.__session.agent = None
        await self.__emit_state()
        await self.__maybe_auto_compact()

    async def __store_attachments(self, paths: list[str]) -> tuple[list[dict[str, str]], list[str]]:
        """Validate, copy into the session, and link the prompt's attachments.

        Each source path is read + validated (text-only, per-file and combined
        size caps, at most :data:`MAX_ATTACHMENTS`) and, on success, copied into
        the session's ``attachments/`` directory. The original may have changed
        or vanished since the user staged it, so this server-side read is the
        authoritative gate; a rejected file is skipped and its reason returned
        as a user-facing error (the rest of the prompt still proceeds).

        Returns:
            tuple: ``(stored, errors)`` where each ``stored`` item is
            ``{"name", "stored", "content"}`` (``stored`` is the session-relative
            link, ``content`` is kept only for in-memory injection) and
            ``errors`` is a list of human-readable rejection messages.
        """
        stored: list[dict[str, str]] = []
        errors: list[str] = []
        running_total = 0
        for path in paths:
            if len(stored) >= MAX_ATTACHMENTS:
                errors.append(
                    f"At most {MAX_ATTACHMENTS} files can be attached; the rest were skipped."
                )
                break
            try:
                loaded = load_attachment(path, running_total=running_total)
            except AttachmentError as exc:
                errors.append(str(exc))
                continue
            rel = self.__transient.store_attachment(loaded.name, loaded.content)
            if rel is None:
                errors.append(f'Attached file "{loaded.name}" could not be saved and was skipped.')
                continue
            running_total += loaded.size
            stored.append({"name": loaded.name, "stored": rel, "content": loaded.content})
        return stored, errors

    def __persist_main_messages(self, entry_agent: str) -> Callable[[list[Message]], None]:
        """Return a persist hook that appends main messages to ``session.jsonl``."""

        def _persist(batch: list[Message]) -> None:
            for msg in batch:
                self.__transient.append_message(msg.role, msg.content, entry_agent=entry_agent)

        return _persist

    # ------------------------------------------------------------------
    # Problem Solver LLM loop (standalone, outside the Kodo pipeline)
    # ------------------------------------------------------------------

    async def __run_problem_solver_with_input(
        self, text: str, attachments: list[str] | None = None
    ) -> None:
        """Drive the standalone Problem Solver agent for one user prompt.

        Shares the agent-agnostic main history with the Guide (see
        :meth:`__run_entry_agent`): switching to Problem Solving only swaps the
        system prompt and tools, so the conversation continues across the mode
        change and — unlike before — Problem Solver turns now persist to
        ``session.jsonl``.
        """
        await self.__run_entry_agent(_PROBLEM_SOLVER_AGENT_NAME, text, attachments)

    # ------------------------------------------------------------------
    # Generic agent turn (single LLM call + tool loop)
    # ------------------------------------------------------------------

    async def __run_agent_turn(
        self,
        llm: LLMPlugin,
        routing: LLMRouting,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_dispatch: Callable[[str, dict[str, object]], Awaitable[str]],
        stream_id: str,
        agent_name: str = _GUIDE_AGENT_NAME,
        stop_after_tools: Callable[[], bool] | None = None,
        persist: Callable[[list[Message]], None] | None = None,
        flush_before: frozenset[str] = frozenset(),
        persist_each_iteration: bool = False,
        track_context: bool = False,
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
            persist: When provided, called with each batch of newly appended
                messages so they can be durably logged (main ``session.jsonl``
                or a subsession file). Messages already present on entry are
                assumed already persisted and never re-emitted.
            flush_before: Tool names whose dispatch must be preceded by flushing
                the not-yet-persisted message prefix (including the spawning
                assistant message). Used by the main turn so an interrupted
                sub-agent leaves a recoverable dangling ``tool_use`` on disk.
            persist_each_iteration: When ``True`` (subsession turns), flush after
                every tool-result batch so a sub-agent's history is durable at
                each turn boundary and can be resumed mid-run.
            track_context: When ``True`` (the shared main entry-agent turn), the
                measured prompt+output token total of each LLM call updates the
                live context gauge (:attr:`__context_tokens`) and is pushed to the
                client. Sub-agent/titler turns leave it ``False`` — only the main
                context counts toward the compaction threshold.

        Returns:
            tuple[list[Message], list[Path]]: Updated messages and (unused) files.
        """
        files_written: list[Path] = []
        tool_desc = {t.name: t.user_description for t in tools}
        tool_logger = ToolCallLogger(self.__llm_logs_dir)
        persisted_upto = len(messages)

        def _flush() -> None:
            nonlocal persisted_upto
            if persist is not None and len(messages) > persisted_upto:
                persist(messages[persisted_upto:])
                persisted_upto = len(messages)

        while True:
            call_start_dt = datetime.now(tz=UTC)
            call_start = call_start_dt.isoformat()
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            thinking_signature: str | None = None
            tool_calls: list[ToolCallEvent] = []
            turn_end: TurnEnd | None = None

            await self.__sink.send(
                Envelope.make_event(EVT_LLM_TURN_START, {"agent": agent_name, "model": model})
            )

            try:
                async for event in self.__gateway.stream_query(
                    routing=routing,
                    plugin=llm,
                    sink=self.__sink,
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
                    elif isinstance(event, ThinkingDelta):
                        thinking_parts.append(event.text)
                    elif isinstance(event, ThinkingSignature):
                        thinking_signature = event.signature
                    elif isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                    elif isinstance(event, TurnEnd):
                        turn_end = event
            except Exception:
                await self.__sink.send(Envelope.make_stream_end(stream_id))
                raise

            if turn_end is not None:
                self.__cumulative_usd += turn_end.usage.usd_cost
                call_end_dt = datetime.now(tz=UTC)
                duration_seconds = (call_end_dt - call_start_dt).total_seconds()
                await self.__emit_usage(turn_end, model, duration_seconds)
                await self.__transient.write_agent_record(
                    agent_name,
                    {
                        "call_start": call_start,
                        "call_end": call_end_dt.isoformat(),
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
                if track_context:
                    usage = turn_end.usage
                    # The whole prompt that was sent (uncached input + both cache
                    # tiers) plus the output the model just appended ≈ what the
                    # next call will carry as context.
                    self.__context_tokens = (
                        usage.input_tokens
                        + usage.cache_read_tokens
                        + usage.cache_write_tokens
                        + usage.output_tokens
                    )
                    await self.__emit_context_stats()

            thinking_text = "".join(thinking_parts)

            if not tool_calls:
                if thinking_text:
                    messages = messages + [
                        Message(
                            role="assistant",
                            content=[
                                self.__thinking_block(thinking_text, thinking_signature),
                                {"type": "text", "text": "".join(text_parts) or "(no text)"},
                            ],
                        )
                    ]
                else:
                    messages = messages + [
                        Message(role="assistant", content="".join(text_parts) or "(no text)")
                    ]
                _flush()
                break

            assistant_content: list[dict[str, object]] = []
            if thinking_text:
                assistant_content.append(self.__thinking_block(thinking_text, thinking_signature))
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

            # Persist the spawning assistant message BEFORE dispatching a
            # sub-agent, so a crash mid-subagent leaves the dangling tool_use on
            # disk for the resume path to pick up.
            if any(tc.tool_name in flush_before for tc in tool_calls):
                _flush()

            calls = [(tc.tool_use_id, tc.tool_name, tc.tool_input) for tc in tool_calls]
            tool_results = await self.__dispatch_tool_calls(
                calls, tool_dispatch, tool_desc, tool_logger
            )
            messages = messages + [Message(role="user", content=tool_results)]

            if persist_each_iteration:
                _flush()

            if stop_after_tools is not None and stop_after_tools():
                _flush()
                break

        return messages, files_written

    @staticmethod
    def __thinking_block(thinking: str, signature: str | None) -> dict[str, object]:
        """Build a persisted ``thinking`` content block for an assistant message.

        ``signature`` is Anthropic's per-block signature, required for Claude to
        accept the block back in a later request; llama.cpp never supplies one,
        so the field is simply omitted (see ``_drop_unsigned_thinking`` in
        ``kodo.llms.anthropic._cache``, which strips signature-less thinking
        blocks before they reach a Claude call).
        """
        block: dict[str, object] = {"type": "thinking", "thinking": thinking}
        if signature is not None:
            block["signature"] = signature
        return block

    async def __dispatch_tool_calls(
        self,
        calls: list[tuple[str, str, dict[str, object]]],
        tool_dispatch: Callable[[str, dict[str, object]], Awaitable[str]],
        tool_desc: dict[str, str],
        tool_logger: ToolCallLogger,
    ) -> list[dict[str, object]]:
        """Dispatch a batch of ``(tool_use_id, name, input)`` calls in order.

        Shared by the live turn loop and the crash-resume path (which replays
        the tool calls recorded in a persisted assistant message).

        Returns:
            list[dict[str, object]]: ``tool_result`` content blocks, in order.
        """
        tool_results: list[dict[str, object]] = []
        for tool_use_id, tool_name, tool_input in calls:
            payload: dict[str, object] = {
                "tool_name": tool_name,
                "description": tool_desc.get(tool_name, ""),
                "tool_call_id": tool_use_id,
            }
            # run_command carries a mandatory timeout; surface it so the client
            # can render a "waiting for tool output" progress bar that fills
            # over the timeout window while the command runs.
            if tool_name == "run_command":
                payload["timeout_seconds"] = tool_input.get("timeout")
            await self.__sink.send(Envelope.make_event(EVT_AGENT_TOOL_CALL, payload))
            tc_n = tool_logger.log_invocation(tool_name, tool_input)
            result_text = await tool_dispatch(tool_name, tool_input)
            tool_logger.log_result(tool_name, tc_n, result_text)
            content = await self.__finalize_tool_result(
                tool_use_id, tool_name, tool_input, result_text
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            )
        return tool_results

    async def __finalize_tool_result(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, object],
        result_text: str,
    ) -> str:
        """Normalize a tool result to its schema; persist and surface its detail.

        Returns the JSON string handed back to the LLM as the ``tool_result``
        content. The engine owns the injected ``schema_compliance`` flag (added
        by :func:`~kodo.toolspecs.normalize_output`). The full input + output is
        persisted as a Markdown doc keyed by ``tool_use_id``, and the
        customer-visible projection is pushed to the client via
        :data:`EVT_AGENT_TOOL_CALL_DETAIL`; non-compliant output additionally
        emits :data:`EVT_TOOL_INCOMPLIANT` so the VSIX can warn the user.

        A tool with no matching spec (none today) passes through unchanged.
        """
        spec = _SPECS_BY_NAME.get(tool_name)
        if spec is None:
            return result_text
        try:
            raw: object = json.loads(result_text)
        except json.JSONDecodeError:
            raw = {"result": result_text}

        # A tool may smuggle a before/after diff out-of-band via an
        # undeclared "diff" key (see EditFileTool). Pop it BEFORE
        # normalize_output: it's never part of any output_schema, and leaving
        # it in would make every such call look non-compliant (extra
        # undeclared field) and leak file content into the LLM-visible result.
        diff_raw = raw.pop("diff", None) if isinstance(raw, dict) else None

        output, compliant = normalize_output(spec.output_schema, raw)
        content = json.dumps(output)

        markdown = render_tool_call_markdown(
            name=spec.name,
            external_name=spec.external_name,
            user_description=spec.user_description,
            security_label=spec.security_impact.label,
            compliant=compliant,
            tool_input=tool_input,
            output=output,
        )
        doc_path = self.__transient.write_tool_call(tool_use_id, markdown)

        diff_detail: dict[str, object] | None = None
        if isinstance(diff_raw, dict):
            diff_detail = self.__transient.write_diff_files(
                tool_use_id,
                label=str(diff_raw.get("label", "")),
                filename=str(diff_raw.get("filename", "")),
                old_content=str(diff_raw.get("old_content", "")),
                new_content=str(diff_raw.get("new_content", "")),
            )

        await self.__sink.send(
            Envelope.make_event(
                EVT_AGENT_TOOL_CALL_DETAIL,
                {
                    "tool_call_id": tool_use_id,
                    "file": str(doc_path) if doc_path is not None else None,
                    "rows": build_detail_rows(spec, tool_input, output),
                    "schema_compliance": compliant,
                    "success": tool_result_succeeded(output),
                    "diff": diff_detail,
                },
            )
        )
        if not compliant:
            await self.__sink.send(
                Envelope.make_event(
                    EVT_TOOL_INCOMPLIANT,
                    {
                        "tool_name": spec.name,
                        "external_name": spec.external_name,
                        "user_description": spec.user_description,
                    },
                )
            )
        return content

    # ------------------------------------------------------------------
    # ToolDispatcher factory
    # ------------------------------------------------------------------

    def __make_dispatcher(self, agent_name: str, session_id: str) -> ToolDispatcher:
        """Build a per-run tool dispatcher for *agent_name*.

        Reads ``self.__index`` at call time so post-bootstrap/rollback runs see
        the current index without any persistent surface to rebuild. The
        autonomous mode is not snapshotted here: tools read it live from
        ``self.__session.effective_autonomous``, which the worker freezes per
        prompt, so the dispatcher needs no rebuild on a mode toggle.
        """
        spec = self.__registry.spec_for(agent_name)
        return ToolDispatcher(
            workspace=self.__workspace,
            index=self.__index,
            resolver=self.__make_resolver(),
            gate=self.__gate,
            session=self.__session,
            services=self.__services,
            agent_name=agent_name,
            session_id=session_id,
            root_paths=self.__root_paths(),
            util_paths=self.__util_paths(),
            output_schema=spec.output_schema if spec is not None else None,
        )

    def __root_paths(self) -> tuple[RootPath, ...]:
        """The filesystem roots the run may operate within, mode-aware.

        Guided mode confines the agent to one project, so it reports just the
        bound project root. Problem Solver mode addresses the whole workspace, so
        it reports every open VS Code workspace folder (the map the extension
        keeps synced via ``workspace.folders``). When no folders have been pushed
        — e.g. a future console-only single-project run — it falls back to the
        physical root, keeping ``get_root_paths`` always non-empty.
        """
        if self.__session.workflow_mode == "guided" and self.__current_project is not None:
            cp = self.__current_project
            return (RootPath(name=cp["name"], path=cp["root"]),)
        folders = self.__session_workspace.folders
        if folders:
            return tuple(RootPath(name=name, path=str(p)) for name, p in folders.items())
        root = self.__session_workspace.physical_root
        return (RootPath(name=root.name or str(root), path=str(root)),)

    @staticmethod
    def __util_paths() -> dict[str, Path]:
        """Absolute paths to the bundled search utils (``fd`` / ``ripgrep``).

        Read from the ``~/.kodo/bin/`` manifests written by
        :mod:`kodo.binutils`. A util absent here (not yet installed) is simply
        omitted; the search tool then returns a clear "not available" error
        rather than crashing.
        """
        paths: dict[str, Path] = {}
        kodo_dir = kodo_user_dir()
        for name in ("fd", "ripgrep"):
            install = find_util(kodo_dir, name)
            if install is not None:
                paths[name] = install.path
        return paths

    def __make_resolver(self) -> PathResolver:
        """Pick the path resolver for the active workflow mode.

        Guided confines file/shell tools to the locked current project's root;
        Problem Solver resolves *logical* paths (workspace-folder-keyed) so it
        can address every project in the workspace.  In the degenerate case of a
        Guided run with no project bound (the extension should prevent this), it
        falls back to the logical resolver rather than crashing.
        """
        if self.__session.workflow_mode == "guided" and self.__layout is not None:
            return ProjectPathResolver(self.__layout.root)
        return LogicalPathResolver(
            self.__session_workspace.folders, self.__session_workspace.physical_root
        )

    # ------------------------------------------------------------------
    # Subagent dispatch
    # ------------------------------------------------------------------

    def __assert_can_spawn(self, caller: str, *names: str) -> None:
        """Gate a spawn: ``caller`` must be allowed to invoke every name in *names*.

        Permission is **not** wired to any one agent — there is no "only the
        Guide spawns" assumption. Each agent declares the sub-agents it may
        spawn in its frontmatter ``subagents:`` allow-list (see
        :meth:`AgentRegistry.allowed_subagents`); any agent that also holds a
        spawning tool can drive them. ``_DIRECT_ONLY_AGENTS`` (engine-driven
        agents such as the session titler) are never spawnable by anyone.

        Raises:
            PermissionError: ``caller`` may not spawn one of *names* — surfaced to
                the calling LLM as the tool's ``{"error": ...}`` result.
        """
        allowed = self.__registry.allowed_subagents(caller)
        for name in names:
            if name in _DIRECT_ONLY_AGENTS:
                raise PermissionError(
                    f"{name!r} is engine-driven only and cannot be spawned as a sub-agent."
                )
            if name not in allowed:
                permitted = ", ".join(sorted(allowed)) or "(none)"
                raise PermissionError(
                    f"Agent {caller!r} is not permitted to spawn sub-agent {name!r}. "
                    f"Permitted sub-agents: {permitted}."
                )

    async def __run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Gate a caller's sub-agent spawn, then run it.

        Args:
            caller: Agent making the call (the running agent — not assumed to be
                the Guide). Its frontmatter allow-list gates the spawn.
            name: Sub-agent name from the registry.
            task_input: Structured task, conforming to the sub-agent's
                ``input_schema``.

        Returns:
            dict: The sub-agent's structured result (its ``output_schema``).

        Raises:
            PermissionError: ``caller`` is not permitted to spawn ``name``.
        """
        self.__assert_can_spawn(caller, name)
        return await self.__spawn_subagent(name, task_input)

    @staticmethod
    def __render_task_input(task_input: dict[str, object]) -> str:
        """Render a structured ``task_input`` to the user turn the sub-agent reads.

        The instructions become the heading; every other field is listed under
        ``## Inputs`` (lists comma-joined). This is what the LLM sees; the UI
        renders the same task as a distinct *task brief* entry (see the
        ``subagent_task`` entry kind), not as a user prompt bubble.
        """
        if not task_input:
            return "(no task)"
        lines: list[str] = []
        instructions = task_input.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            lines.append("# Task\n\n" + instructions.strip())
        others = {k: v for k, v in task_input.items() if k != "instructions"}
        if others:
            input_lines = ["## Inputs"]
            for key, value in others.items():
                if isinstance(value, list):
                    rendered = ", ".join(str(x) for x in value) if value else "(none)"
                else:
                    rendered = str(value)
                input_lines.append(f"- {key}: {rendered}")
            lines.append("\n".join(input_lines))
        return "\n\n".join(lines) or "(no task)"

    async def __spawn_subagent(self, name: str, task_input: dict[str, object]) -> dict[str, object]:
        """Invoke a leaf sub-agent and return its structured result.

        The ungated spawn primitive: callers that have already passed the
        permission gate (:meth:`__run_subagent`, or
        :meth:`__run_author_critic_iteration` which gates both names up front)
        drive a subsession through here.

        Args:
            name: Sub-agent name from the registry.
            task_input: Structured task conforming to the sub-agent's input schema.

        Returns:
            dict: The structured result the sub-agent returned via ``return_result``.
        """
        if name in _DIRECT_ONLY_AGENTS:
            _log.warning("spawn_subagent: %r is engine-driven only and cannot be invoked", name)
            return {}

        # During a crash-resume replay, each run_subagent call consumes the next
        # subsession marker recorded before the crash instead of starting fresh.
        # An exhausted/empty ledger means no marker was recorded for this call
        # (crash landed before the subsession opened) — fall through to a fresh run.
        if self.__replay_subsessions:
            return await self.__replay_next_subsession(name)
        self.__replay_subsessions = None

        subsession_id = uuid.uuid4().hex
        seed_content = self.__render_task_input(task_input)
        await self.__open_subsession(name, subsession_id, seed_content)

        seed = Message(role="user", content=seed_content)
        # Persisted/displayed as a distinct task brief, not a user prompt bubble.
        self.__transient.append_subsession_message(
            subsession_id, seed.role, seed.content, kind="subagent_task"
        )

        output = await self.__drive_subsession(name, subsession_id, [seed])
        await self.__close_subsession(name, subsession_id, output)
        return output

    async def __drive_subsession(
        self, name: str, subsession_id: str, messages: list[Message]
    ) -> dict[str, object]:
        """Run a sub-agent's isolated turn loop and return its structured result.

        Used for both a fresh subsession and a resumed one (``messages`` already
        rehydrated from the subsession log). Sub-agent messages persist into the
        subsession file at every turn boundary so the run is resumable mid-flight.
        The structured result is whatever the agent passed to ``return_result``
        (validated against its output schema); if it never called it, a fallback
        ``{artifact_ids, schema_compliance: False}`` is synthesized from the
        artifacts it published.
        """
        agent = self.__registry.get(name, self.__session.effective_autonomous)
        plugin, model_id, routing = await self.__resolve_plugin(agent.capability)
        dispatcher = self.__make_dispatcher(name, subsession_id)
        leaf_tools = tools_for_agent(agent.tools)

        self.__session.phase = "running"
        self.__session.agent = name
        await self.__emit_state()

        stream_id = uuid.uuid4().hex
        await self.__emit_agent_started(name)

        def _persist(batch: list[Message]) -> None:
            for msg in batch:
                self.__transient.append_subsession_message(subsession_id, msg.role, msg.content)

        await self.__run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=messages,
            tools=leaf_tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=name,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=_persist,
            persist_each_iteration=True,
        )

        await self.__sink.send(Envelope.make_stream_end(stream_id))
        await self.__emit_agent_finished(name)
        # Union freshly-published IDs with any published before a crash (those
        # are recoverable because publish_artifact stamps the subsession_id).
        pre_crash = [
            e.artifact_id for e in self.__index.all_entries() if e.session_id == subsession_id
        ]
        published = list(dict.fromkeys([*pre_crash, *dispatcher.published_ids]))
        output = dispatcher.returned_output
        if output is None:
            _log.warning(
                "subsession %s (%s) ended without return_result; synthesizing fallback",
                subsession_id,
                name,
            )
            output = {"artifact_ids": published, SCHEMA_COMPLIANCE_KEY: False}
        _log.info(
            "subsession completed: name=%s id=%s published=%s keys=%s",
            name,
            subsession_id,
            published,
            sorted(output.keys()),
        )
        return output

    async def __open_subsession(
        self, name: str, subsession_id: str, task_content: str = ""
    ) -> None:
        """Record a subsession takeover: marker, active pointer, and UI divider.

        ``task_content`` is the rendered task brief; it rides the live
        ``subsession.started`` event so the client can show the same task-brief
        card it reconstructs from the seed message on reload.
        """
        display_name = self.__display_name(name)
        parent_display = self.__display_name(self.__session.agent or _GUIDE_AGENT_NAME)
        self.__transient.append_marker(
            {
                "type": "subsession_start",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        self.__transient.update(
            active_subsession={
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        await self.__sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_STARTED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "task": task_content,
                },
            )
        )

    async def __close_subsession(
        self, name: str, subsession_id: str, output: dict[str, object]
    ) -> None:
        """Record a subsession handing control back: marker, clear pointer, divider.

        ``output`` is the sub-agent's structured result; it is stored on the
        ``subsession_end`` marker so a crash-resume replay can return it verbatim.
        """
        display_name = self.__display_name(name)
        parent_display = self.__display_name(self.__session.agent or _GUIDE_AGENT_NAME)
        self.__transient.append_marker(
            {
                "type": "subsession_end",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
                "result": dict(output),
            }
        )
        self.__transient.update(active_subsession=None)
        await self.__sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_ENDED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "parent_display_name": parent_display,
                },
            )
        )

    async def __replay_next_subsession(self, name: str) -> dict[str, object]:
        """Consume the next pre-crash subsession marker during resume replay.

        Completed subsessions return their stored structured result immediately
        (the artifacts are already on disk and rebuilt into the index). The single
        active (un-closed) subsession is rehydrated from its log and driven to
        completion live; once consumed, replay mode ends. A legacy marker whose
        ``result`` is a bare artifact-id list is wrapped into the structured shape.
        """
        assert self.__replay_subsessions
        rec = self.__replay_subsessions.pop(0)
        subsession_id = str(rec["subsession_id"])
        if not self.__replay_subsessions:
            self.__replay_subsessions = None
        if rec.get("completed"):
            _log.info(
                "Replay: subsession %s already complete; returning stored result", subsession_id
            )
            result = rec.get("result", {})
            if isinstance(result, dict):
                return result
            if isinstance(result, list):  # legacy marker: bare artifact-id list
                return {"artifact_ids": [str(x) for x in result], SCHEMA_COMPLIANCE_KEY: False}
            return {}

        _log.info("Replay: resuming active subsession %s (%s)", subsession_id, name)
        rehydrated = [
            Message(role=str(m["role"]), content=m["content"])  # type: ignore[arg-type]
            for m in self.__transient.read_subsession_messages(subsession_id)
        ]
        output = await self.__drive_subsession(name, subsession_id, rehydrated)
        await self.__close_subsession(name, subsession_id, output)
        return output

    def __display_name(self, agent_name: str) -> str:
        """User-friendly name for an agent (frontmatter ``display_name`` or derived)."""
        try:
            return self.__registry.get(agent_name).display_name or agent_name
        except AgentLoadError:
            return agent_name

    # ------------------------------------------------------------------
    # Crash resume of an interrupted sub-agent subsession
    # ------------------------------------------------------------------

    def __has_dangling_tool_use(self) -> bool:
        """True when the last persisted main message awaits sub-agent results.

        A spawning-tool turn flushes the assistant ``tool_use`` to disk before
        dispatch; an interrupted sub-agent therefore leaves that assistant
        message as the final persisted main message with no following
        ``tool_result``. That is the marker of a resumable subsession.
        """
        if not self.__main_messages:
            return False
        last = self.__main_messages[-1]
        if last.role != "assistant" or not isinstance(last.content, list):
            return False
        return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in last.content)

    def __last_entry_agent(self) -> str:
        """Entry agent that produced the last persisted main message.

        Read from the ``entry_agent`` tag on the most recent message line in
        ``session.jsonl`` — *any* entry agent may have been holding the floor
        when the run was interrupted, so resume must not assume the Guide.
        Falls back to the Guide only for legacy/untagged sessions.
        """
        for line in reversed(self.__transient.read_session_lines()):
            if "role" in line:
                ea = line.get("entry_agent")
                return ea if isinstance(ea, str) and ea else _GUIDE_AGENT_NAME
        return _GUIDE_AGENT_NAME

    async def __resume_main_turn(self) -> None:
        """Resume a main turn that was interrupted while a sub-agent held the floor.

        Rebuilds the subsession replay ledger from the markers recorded after
        the dangling assistant message, re-dispatches the pending spawning tool
        call(s) — completed sub-sessions return their stored result, the active
        one is rehydrated and driven to completion — then appends the tool
        results and continues the interrupted entry agent's turn live.

        The entry agent is recovered from the persisted ``entry_agent`` tag, not
        assumed to be the Guide: any agent permitted to spawn sub-agents
        can be the one holding the floor at crash time.
        """
        last = self.__main_messages[-1]
        if not isinstance(last.content, list):
            return
        tool_uses = [b for b in last.content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not tool_uses:
            return

        entry_agent = self.__last_entry_agent()
        self.__replay_subsessions = self.__build_replay_ledger()
        _log.info(
            "Resuming interrupted main turn for %r: %d pending tool call(s), "
            "%d subsession(s) to replay",
            entry_agent,
            len(tool_uses),
            len(self.__replay_subsessions),
        )

        agent = self.__registry.get(entry_agent, self.__session.effective_autonomous)
        plugin, model_id, routing = await self.__resolve_plugin(agent.capability)
        self.__active_model_key = self.__resolve_model_key(agent.capability)
        dispatcher = self.__make_dispatcher(entry_agent, self.__orch_session_id)
        tools = tools_for_agent(agent.tools)
        tool_desc = {t.name: t.user_description for t in tools}
        tool_logger = ToolCallLogger(self.__llm_logs_dir)

        self.__session.phase = "running"
        self.__session.agent = entry_agent
        await self.__emit_state()
        await self.__emit_agent_started(entry_agent)

        calls: list[tuple[str, str, dict[str, object]]] = []
        for b in tool_uses:
            raw_input = b.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            calls.append((str(b["id"]), str(b["name"]), tool_input))
        tool_results = await self.__dispatch_tool_calls(
            calls, dispatcher.dispatch, tool_desc, tool_logger
        )
        self.__replay_subsessions = None
        results_msg = Message(role="user", content=tool_results)
        self.__main_messages = self.__main_messages + [results_msg]
        self.__transient.append_message(
            results_msg.role, results_msg.content, entry_agent=entry_agent
        )

        stream_id = uuid.uuid4().hex
        self.__main_messages, _ = await self.__run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=self.__main_messages,
            tools=tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=entry_agent,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=self.__persist_main_messages(entry_agent),
            flush_before=_SUBAGENT_SPAWNING_TOOLS,
            track_context=True,
        )
        await self.__sink.send(Envelope.make_stream_end(stream_id))
        await self.__emit_agent_finished(entry_agent)
        if self.__session.phase != "done":
            self.__session.phase = "awaiting_user"
        self.__session.agent = None
        await self.__emit_state()
        await self.__maybe_auto_compact()

    def __build_replay_ledger(self) -> list[dict[str, object]]:
        """Build the ordered subsession replay ledger from ``session.jsonl`` markers.

        Considers only the markers after the last persisted assistant message
        (the in-flight spawning turn). Each ``subsession_start`` becomes a ledger
        entry; one paired with a ``subsession_end`` is ``completed`` (its stored
        result is reused), an unpaired start is the single active subsession.
        """
        lines = self.__transient.read_session_lines()
        last_assistant = -1
        for i, ln in enumerate(lines):
            if ln.get("role") == "assistant":
                last_assistant = i
        markers = [
            ln
            for ln in lines[last_assistant + 1 :]
            if ln.get("type") in ("subsession_start", "subsession_end")
        ]
        ends = {str(m["subsession_id"]): m for m in markers if m.get("type") == "subsession_end"}
        ledger: list[dict[str, object]] = []
        for m in markers:
            if m.get("type") != "subsession_start":
                continue
            sid = str(m["subsession_id"])
            end = ends.get(sid)
            end_result = end.get("result", []) if end else []
            ledger.append(
                {
                    "subsession_id": sid,
                    "agent": m.get("agent"),
                    "completed": end is not None,
                    "result": list(end_result) if isinstance(end_result, list) else [],
                }
            )
        return ledger

    # ------------------------------------------------------------------
    # Author/Critic iteration
    # ------------------------------------------------------------------

    async def __extract_verdict(
        self, critic_output: dict[str, object]
    ) -> tuple[str, list[dict[str, object]]]:
        """Read a critic's verdict + concerns, preferring its ``return_result``.

        The structured ``return_result`` output is authoritative. As a safety net
        for a critic that finished without returning a verdict, fall back to
        reading its published feedback artifact (the pre-schema behaviour).
        """
        verdict_raw = critic_output.get("verdict")
        if isinstance(verdict_raw, str) and verdict_raw:
            concerns_raw = critic_output.get("concerns", [])
            concerns = (
                [c for c in concerns_raw if isinstance(c, dict)]
                if isinstance(concerns_raw, list)
                else []
            )
            return verdict_raw, concerns

        candidate_ids: list[str] = []
        feedback_id = critic_output.get("feedback_artifact_id")
        if isinstance(feedback_id, str) and feedback_id:
            candidate_ids.append(feedback_id)
        art_ids = critic_output.get("artifact_ids")
        if isinstance(art_ids, list):
            candidate_ids.extend(str(a) for a in reversed(art_ids))
        for aid in candidate_ids:
            feedback_arts = await self.__workspace.read(artifact_id=aid)
            if not feedback_arts:
                continue
            fa = feedback_arts[0]
            if fa.verdict is not None:
                return (
                    fa.verdict.value,
                    [{"kind": c.kind, "description": c.description} for c in fa.concerns],
                )
        return "accepted", []

    async def __run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        """Execute one Author/Critic round and return verdict + concerns.

        Args:
            caller: Agent making the call. Its frontmatter allow-list must permit
                spawning both ``author_name`` and ``critic_name``; both are gated
                up front so the inner spawns can use the ungated primitive.
            author_name: Author sub-agent name.
            critic_name: Critic sub-agent name.
            input_artifact_ids: Input artifact IDs for the Author.
            for_revision_artifact_ids: Prior Author outputs to revise (empty on the
                first round).

        Returns:
            dict: ``{artifact_id, verdict, concerns}`` — verdict/concerns come from
            the Critic's ``return_result`` (falling back to its feedback artifact
            if it returned none).

        Raises:
            PermissionError: ``caller`` may not spawn the author or the critic.
        """
        self.__assert_can_spawn(caller, author_name, critic_name)
        author_instructions = "Produce your artifact."
        if for_revision_artifact_ids:
            author_instructions += " Revise the prior version(s) per the critic's concerns."
        author_task: dict[str, object] = {
            "instructions": author_instructions,
            "input_artifact_ids": input_artifact_ids,
            "for_revision_artifact_ids": for_revision_artifact_ids,
        }

        author_output = await self.__spawn_subagent(author_name, author_task)
        author_ids_raw = author_output.get("artifact_ids", [])
        author_ids = [str(a) for a in author_ids_raw] if isinstance(author_ids_raw, list) else []

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

        critic_task: dict[str, object] = {
            "instructions": (
                f"Review artifact {primary_id} and publish a feedback artifact "
                f"with reviewed_artifact_id={primary_id}."
            ),
            "input_artifact_ids": [primary_id],
        }
        critic_output = await self.__spawn_subagent(critic_name, critic_task)
        verdict, concerns = await self.__extract_verdict(critic_output)

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
        """Execute rollback, rebuild the index, and start a fresh Guide session.

        Args:
            target_sha: Mirror commit SHA to roll back to.
        """
        _log.info("Rollback initiated: target_sha=%s", target_sha[:12])
        rollback = Rollback(
            self.__require_layout().root,
            self.__require_checkpoints().repo,
            self.__workspace_layout.sessions_dir,
        )
        index = await rollback.execute(target_sha)

        self.__index = index
        self.__workspace.bind_index(self.__index)
        self.__toolchain = None  # tech-stack may differ post-rollback; re-resolve lazily
        # Session identity is owned by the driving window and is unchanged; the
        # rollback only invalidates the in-memory conversation, so reset it.
        self.__main_messages = []
        self.__replay_subsessions = None
        _log.info("Post-rollback: index rebuilt for session %s", self.__orch_session_id)

    # ------------------------------------------------------------------
    # Artifact completion (promotion)
    # ------------------------------------------------------------------

    async def __complete_artifact(self, artifact_id: str) -> None:
        """Promote a gate-passed artifact and mark it completed.

        Materializes the artifact into ``src/``/``gen/``, commits it to the
        mirror with a sidecar, flips its index entry to ``completed`` at the
        promoted location, and removes the workspace staging file. Non-
        materializable artifacts (e.g. feedback) only flip state.

        Args:
            artifact_id: ID of the artifact reported complete.
        """
        arts = await self.__workspace.read(artifact_id=artifact_id)
        if not arts:
            _log.warning("complete_artifact: %s not found; flipping state only", artifact_id[:8])
            await self.__workspace.mark_completed(artifact_id)
            return
        artifact = arts[0]

        toolchain = await self.__resolve_toolchain()
        registry = await self.__component_registry()
        target = materialization_path(artifact, self.__require_layout().root, toolchain, registry)
        if target is None:
            await self.__workspace.mark_completed(artifact_id)
            return

        promoter = Promoter(
            self.__require_layout().root, self.__require_checkpoints().repo, toolchain, registry
        )
        message = f"[{artifact.type.value}] {artifact.responsibility_code} completed"
        try:
            await promoter.promote(artifact, message)
        except PromoterError:
            _log.exception("complete_artifact: promote failed for %s", artifact_id[:8])
            await self.__workspace.mark_completed(artifact_id)
            return

        await self.__workspace.mark_completed(artifact_id, location=target)
        _log.info(
            "complete_artifact: promoted %s (%s) -> %s",
            artifact_id[:8],
            artifact.type.value,
            target,
        )

    async def __resolve_toolchain(self) -> ToolchainPlugin:
        """Resolve the active toolchain from the Tech Stack, caching the result.

        Falls back to Python until a Tech Stack artifact exists (only code/test
        promotion needs a real toolchain, and those stages run well after the
        Tech Stack is accepted).
        """
        if self.__toolchain is not None:
            return self.__toolchain
        content = await self.__latest_artifact_content(ArtifactType.TECH_STACK)
        if content is not None:
            self.__toolchain = select_toolchain(content, self.__require_layout().root)
            return self.__toolchain
        return select_toolchain("", self.__require_layout().root)

    async def __component_registry(self) -> ComponentRegistry:
        """Build a component registry from the architecture document, if any."""
        content = await self.__latest_artifact_content(ArtifactType.ARCHITECTURE)
        return ComponentRegistry(content) if content is not None else ComponentRegistry.empty()

    async def __latest_artifact_content(self, artifact_type: ArtifactType) -> str | None:
        """Return the content of the most recent artifact of *artifact_type*."""
        entries = [e for e in self.__index.all_entries() if e.type == artifact_type]
        if not entries:
            return None
        latest = max(entries, key=lambda e: e.created_at)
        arts = await self.__workspace.read(artifact_id=latest.artifact_id)
        return arts[0].content if arts else None

    def history_entries(self) -> list[dict[str, object]]:
        """Rebuild the full client-facing feed for a resumed session.

        Walks the main ``session.jsonl`` in order. Message lines become
        ``user_message`` / ``assistant_response`` / ``tool_call`` entries; a
        ``subsession_start`` marker emits a takeover divider and splices the
        sub-agent's full inner transcript (read from its subsession log), and a
        ``subsession_end`` marker emits a hand-back divider. This gives the
        WebView a faithful replay of who did what, including sub-agent work.

        Returns:
            list[dict[str, object]]: Ordered entries in the shape expected by the
            VSIX webview's ``session.history`` handler.
        """
        tool_desc = {t.name: t.user_description for t in ALL_TOOLS}
        toolcalls_dir = self.__transient.toolcalls_dir
        lines = self.__transient.read_session_lines()

        # Pass 1: index every tool_use_id → its (normalized) output, so the
        # tool_call entries can be rebuilt with their detail rows and file link.
        # Subsession transcripts carry their own tool calls, so include them.
        all_messages: list[dict[str, object]] = [ln for ln in lines if "role" in ln]
        for line in lines:
            if line.get("type") == "subsession_start":
                sid = str(line.get("subsession_id", ""))
                all_messages.extend(self.__transient.read_subsession_messages(sid))
        results_by_id = self.__tool_results_from_messages(all_messages)

        session_dir = self.__transient.session_dir
        entries: list[dict[str, object]] = []
        for line in lines:
            if "role" in line:
                entries.extend(
                    self.__message_to_entries(
                        line, tool_desc, results_by_id, toolcalls_dir, session_dir
                    )
                )
                continue
            kind = line.get("type")
            if kind == "subsession_start":
                entries.append(self.__divider_entry("subsession_start", line))
                sid = str(line.get("subsession_id", ""))
                for sub in self.__transient.read_subsession_messages(sid):
                    entries.extend(
                        self.__message_to_entries(
                            sub, tool_desc, results_by_id, toolcalls_dir, session_dir
                        )
                    )
            elif kind == "subsession_end":
                entries.append(self.__divider_entry("subsession_end", line))
            elif kind == "compaction":
                tb = line.get("tokens_before", 0)
                ta = line.get("tokens_after", 0)
                entries.append(
                    {
                        "type": "context_compacted",
                        "summaryExcerpt": str(line.get("summary", ""))[:_COMPACTION_EXCERPT_LEN],
                        # Full summary so the reloaded divider expands to the same
                        # post-compaction context shown live.
                        "summary": str(line.get("summary", "")),
                        "tokensBefore": tb if isinstance(tb, int) else 0,
                        "tokensAfter": ta if isinstance(ta, int) else 0,
                    }
                )
        return entries

    @staticmethod
    def __tool_results_from_messages(
        messages: list[dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        """Map ``tool_use_id`` → parsed tool output across persisted messages.

        Tool outputs live in ``tool_result`` blocks of user messages; the
        content is the normalized JSON string the engine stored at dispatch.
        """
        results: dict[str, dict[str, object]] = {}
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id", ""))
                raw_content = block.get("content")
                if not tool_use_id or not isinstance(raw_content, str):
                    continue
                try:
                    parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    results[tool_use_id] = parsed
        return results

    @staticmethod
    def __divider_entry(kind: str, marker: dict[str, object]) -> dict[str, object]:
        return {
            "type": kind,
            "agent": str(marker.get("agent", "")),
            "displayName": str(marker.get("display_name", "")),
            "parentDisplayName": str(marker.get("parent_display_name", "")),
        }

    @staticmethod
    def __message_to_entries(
        msg: dict[str, object],
        tool_desc: dict[str, str],
        results_by_id: dict[str, dict[str, object]],
        toolcalls_dir: Path,
        session_dir: Path,
    ) -> list[dict[str, object]]:
        """Convert one persisted ``{role, content}`` line to client feed entries."""
        role = msg.get("role")
        content = msg.get("content")
        out: list[dict[str, object]] = []
        # A subsession's seed task is a user-role message tagged ``subagent_task``;
        # render it as a distinct task brief, never as the user's prompt bubble.
        if msg.get("kind") == "subagent_task":
            out.append(
                {"type": "subagent_task", "content": content if isinstance(content, str) else ""}
            )
            return out
        if isinstance(content, str):
            if role == "user":
                atts = _history_attachment_links(msg.get("attachments"), session_dir)
                if content or atts:
                    out.append({"type": "user_message", "content": content, "attachments": atts})
            elif role == "assistant" and content:
                out.append({"type": "assistant_response", "content": content})
            return out
        if not isinstance(content, list):
            return out
        if role == "assistant":
            thinking_text = "".join(
                str(b.get("thinking", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
            if thinking_text:
                out.append({"type": "thinking_block", "content": thinking_text})
            text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                out.append({"type": "assistant_response", "content": text})
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name", ""))
                    tool_use_id = str(block.get("id", ""))
                    tool_input = block.get("input")
                    if not isinstance(tool_input, dict):
                        tool_input = {}
                    output = results_by_id.get(tool_use_id)
                    spec = _SPECS_BY_NAME.get(name)
                    rows = build_detail_rows(spec, tool_input, output) if spec is not None else []
                    doc = toolcalls_dir / f"{tool_use_id}.md"
                    diff = read_diff_files(toolcalls_dir, tool_use_id)
                    entry: dict[str, object] = {
                        "type": "tool_call",
                        "toolName": name,
                        "description": tool_desc.get(name, ""),
                        "toolCallId": tool_use_id,
                        "rows": rows,
                        "detailFile": str(doc) if doc.exists() else None,
                        "schemaCompliance": (
                            output.get("schema_compliance") if output is not None else None
                        ),
                        "success": tool_result_succeeded(output),
                        "diff": (
                            {
                                "label": diff["label"],
                                "prevPath": diff["prev_path"],
                                "newPath": diff["new_path"],
                            }
                            if diff is not None
                            else None
                        ),
                    }
                    out.append(entry)
        elif role == "user":
            text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                out.append({"type": "user_message", "content": text, "attachments": []})
        return out

    def __load_main_messages(self) -> list[Message]:
        # Honour the latest compaction marker: the live LLM context is the
        # compacted summary block plus every message appended after that marker.
        # Lines before it remain in session.jsonl as audit history (and are still
        # replayed into the client feed by history_entries), but are never resent
        # to the model. With no marker this is the full message history.
        lines = self.__transient.read_session_lines()
        last_compaction = -1
        for i, line in enumerate(lines):
            if line.get("type") == "compaction":
                last_compaction = i

        messages: list[Message] = []
        if last_compaction >= 0:
            summary = str(lines[last_compaction].get("summary", ""))
            if summary:
                messages.append(self.__compaction_context_message(summary))

        for item in lines[last_compaction + 1 :]:
            if "role" not in item:
                continue
            try:
                role = str(item["role"])
                content = item["content"]
                if isinstance(content, str):
                    content = self.__expand_persisted_attachments(content, item.get("attachments"))
                if isinstance(content, (str, list)):
                    messages.append(Message(role=role, content=content))
            except (KeyError, TypeError):
                _log.warning("Skipping malformed message in session.jsonl")
        return messages

    def __expand_persisted_attachments(self, clean_text: str, attachments: object) -> str:
        """Re-inject a persisted user message's attachments from their copies.

        ``session.jsonl`` stores only the clean prompt plus attachment links; on
        resume the LLM context must match what was sent originally, so each
        stored copy is read back and re-injected with the same layout used at
        submit time (:func:`inject_attachments`). A copy that has gone missing is
        replaced by a short placeholder rather than failing the whole resume.
        """
        if not isinstance(attachments, list) or not attachments:
            return clean_text
        items: list[tuple[str, str]] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            name = str(att.get("name", "attachment"))
            stored = str(att.get("stored", ""))
            content = self.__transient.read_attachment(stored) if stored else None
            items.append((name, content if content is not None else "(attachment unavailable)"))
        return inject_attachments(clean_text, items)

    # ------------------------------------------------------------------
    # Event emitters
    # ------------------------------------------------------------------

    async def __handle_stream_event(self, event: StreamEvent, stream_id: str) -> None:
        if isinstance(event, ThinkingDelta):
            await self.__sink.send(Envelope.make_thinking_chunk(stream_id, event.text))
        elif isinstance(event, TokenDelta):
            await self.__sink.send(Envelope.make_stream_chunk(stream_id, event.text))
        elif isinstance(event, ToolCallArgDelta):
            await self.__sink.send(
                Envelope.make_toolgen_chunk(stream_id, event.tool_name, event.text)
            )

    async def __emit_state(self) -> None:
        await self.__sink.send(Envelope.make_event(EVT_STATE, self.__session.to_dict()))
        # The header context gauge and its "Compact now" enablement both depend
        # on phase, so refresh them whenever state is pushed.
        await self.__emit_context_stats()

    async def __emit_context_stats(self) -> None:
        """Push the live context gauge (current/limit/percent + compactability)."""
        limit = self.__context_limit()
        current = self.__context_tokens
        percent = round(100.0 * current / limit, 1) if limit > 0 else 0.0
        await self.__sink.send(
            Envelope.make_event(
                EVT_CONTEXT_STATS,
                {
                    "current_tokens": current,
                    "limit_tokens": limit,
                    "percent": percent,
                    "can_compact": self.__can_compact(),
                },
            )
        )

    async def __emit_context_compacting(self, active: bool) -> None:
        """Bracket a compaction run so the client shows a "Compacting…" banner."""
        await self.__sink.send(Envelope.make_event(EVT_CONTEXT_COMPACTING, {"active": active}))

    async def __emit_usage(self, turn_end: TurnEnd, model: str, duration_seconds: float) -> None:
        await self.__sink.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self.__cumulative_usd, 6),
                    "duration_seconds": round(duration_seconds, 3),
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

    async def __emit_session_naming(self, active: bool) -> None:
        """Tell the client whether the silent session-titler call is running.

        Drives a transient "Naming session …" indicator in the WebView so the
        titling round-trip (which streams nothing) does not look like a stall.
        """
        await self.__sink.send(Envelope.make_event(EVT_SESSION_NAMING, {"active": active}))

    async def __emit_cost_only(self) -> None:
        """Push a cost-only ``usage.update`` (no per-call token entry).

        With ``last_call_tokens`` set to ``None`` the client updates the running
        session-cost figure without appending a status entry to the feed — used
        to fold an invisible call's cost (e.g. session titling) into the total.
        """
        await self.__sink.send(
            Envelope.make_event(
                EVT_USAGE_UPDATE,
                {
                    "cumulative_usd": round(self.__cumulative_usd, 6),
                    "duration_seconds": 0.0,
                    "last_call_tokens": None,
                    "model": "",
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

    async def __disable_autonomous(self) -> None:
        """Disable autonomous mode and notify the client.

        Unlike a user toggle, this is an Guide decision that must take
        effect immediately, so it clears the frozen ``effective_autonomous`` as
        well — any sub-agent spawned later in this same prompt runs interactive.
        """
        self.__session.autonomous = False
        self.__session.effective_autonomous = False
        self.__transient.update(autonomous=False)
        await self.__emit_state()
        await self.__sink.send(Envelope.make_event(EVT_AUTONOMOUS_CHANGED, {"autonomous": False}))

    async def __post_update(self, message: str) -> None:
        """Forward a progress update message to the client."""
        await self.__sink.send(Envelope.make_event(EVT_POST_UPDATE, {"message": message}))

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
