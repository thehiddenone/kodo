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

:class:`WorkflowEngine` itself is assembled from two kinds of parts (see
this package's ``__init__`` for the map):

- **mixins** (:mod:`._llm`, :mod:`._worker`, :mod:`._turns`,
  :mod:`._subagents`, :mod:`._resume`) share this instance's state through
  the :class:`~._proto.EngineHost` protocol;
- **collaborators** (:mod:`._events`, :mod:`._compaction`, :mod:`._titling`,
  :mod:`._checkpointing`, :mod:`._history`) own their state slice and reach
  back only through their narrow host protocols.

This module keeps what is left: construction/wiring, the session lifecycle
(start/stop/bind), the client-facing ``handle_*`` entry points, and the small
environment helpers (path resolvers, root set) everything else shares.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

from kodo.binutils import find_util
from kodo.common import ApiKeyProvider, Envelope, MessageSink
from kodo.guided_state import append_accepted, append_review_result
from kodo.llms import LLMGateway, Message
from kodo.project import (
    ProjectLayout,
    ProjectLayoutError,
    SessionWorkspace,
    WorkspaceLayout,
    kodo_user_dir,
)
from kodo.security import SecurityLayer
from kodo.state import TransientStore
from kodo.subagents import AgentLoadError, AgentRegistry
from kodo.tools import LogicalPathResolver, PathResolver, ProjectPathResolver, RootPath
from kodo.transport import (
    EVT_AUTONOMOUS_CHANGED,
    EVT_PROJECT_BOUND,
    EVT_WORKSPACE_ADD_FOLDER,
)

from .._attachments import parse_attachment_marker
from .._checkpoints import CheckpointState
from .._gates import GateOrchestrator
from .._session import SessionState
from ._checkpointing import CheckpointCoordinator
from ._compaction import ContextCompactor, estimate_tokens
from ._events import EngineEmitters
from ._history import HistoryProjector
from ._llm import LLMPlumbingMixin
from ._resume import ResumeMixin
from ._services import _EngineServices
from ._shared import _slugify_project_name, _unique_child_dir
from ._subagents import SubagentMixin
from ._titling import SessionTitler
from ._turns import TurnLoopMixin
from ._worker import WorkerMixin

_log = logging.getLogger(__name__)


class WorkflowEngine(LLMPlumbingMixin, WorkerMixin, TurnLoopMixin, SubagentMixin, ResumeMixin):
    """Single-worker runtime engine hosting the Guide session.

    Args:
        sink: Message sink for sending events to the connected client.
        gate: Gate orchestrator for approval and question prompts.
        key_provider: Provider for cloud API keys.
        get_settings: Callable returning the current merged settings dict.
        transient: Append-only JSONL session store.
        layout: Project filesystem layout.
        registry: Loaded subagent file registry.
    """

    _sink: MessageSink
    _gate: GateOrchestrator
    _key_provider: ApiKeyProvider
    _get_settings: Callable[[], dict[str, object]]
    _transient: TransientStore
    _workspace_layout: WorkspaceLayout
    _session_workspace: SessionWorkspace
    _gateway: LLMGateway
    _layout: ProjectLayout | None
    _registry: AgentRegistry
    _security: SecurityLayer
    _services: _EngineServices
    _emitters: EngineEmitters
    _compactor: ContextCompactor
    _titler: SessionTitler
    _checkpoints: CheckpointCoordinator
    _history: HistoryProjector
    _current_project: dict[str, str] | None
    _queue: asyncio.Queue[dict[str, object]]
    _session: SessionState
    _worker: asyncio.Task[None] | None
    _main_messages: list[Message]
    _orch_session_id: str
    _current_vendor: str | None
    _last_thinking_base_llm: str | None
    _replay_subsessions: list[dict[str, object]] | None
    _resume_subsession_pending: bool

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

        The engine is workspace-scoped. The project-level collaborator (the
        ``ProjectLayout``) is built lazily in :meth:`bind_project` when the
        current project is selected for Guided mode; until then
        ``self._layout`` is ``None`` and Guided-only tools (``guided_dev_status``,
        ``document_feedback``, ``rollback``) are unreachable because no Guided
        prompt can run without a bound project.

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
        self._sink = sink
        self._gate = gate
        self._key_provider = key_provider
        self._get_settings = get_settings
        self._transient = transient
        self._workspace_layout = workspace_layout
        self._session_workspace = session_workspace or SessionWorkspace()
        self._gateway = gateway
        self._registry = registry
        self._layout = None
        self._current_project = None
        self._queue = asyncio.Queue()
        self._session = SessionState()
        self._worker = None
        self._main_messages = []
        self._orch_session_id = ""
        self._current_vendor = None
        # Sentinel distinct from every real base_llm (including "") so the
        # first _sync_thinking_level_to_model()/start() call always seeds
        # thinking_level rather than treating "no change" as a no-op.
        self._last_thinking_base_llm = None
        self._replay_subsessions = None
        self._resume_subsession_pending = False
        # The security layer judging every tool call (doc/SECURITY.md) —
        # deterministic heuristic rules, no LLM involved.
        self._security = SecurityLayer()
        # Collaborators. The emitters' context gauge and the compactor's cost
        # folding cross-reference each other, so both sides are late-bound:
        # the emitters get a lambda that reads the compactor built right after.
        self._emitters = EngineEmitters(
            sink,
            self._session,
            context_stats=lambda: self._compactor.context_stats_payload(),
            transient=transient,
        )
        self._compactor = ContextCompactor(
            self,
            registry=registry,
            transient=transient,
            sink=sink,
            session=self._session,
            emitters=self._emitters,
        )
        self._titler = SessionTitler(
            self,
            transient=transient,
            sink=sink,
            emitters=self._emitters,
        )
        self._checkpoints = CheckpointCoordinator(self, sink=sink)
        self._history = HistoryProjector(transient, self._checkpoints)
        self._services = _EngineServices(
            run_subagent=self._run_subagent,
            run_dependency_manager=self._run_dependency_manager,
            run_web_search_agent=self._run_web_search_agent,
            run_author_critic=self._run_author_critic_iteration,
            rollback=self._run_rollback,
            disable_autonomous=self._disable_autonomous,
            create_project=self._create_project,
            notify_tool_call_in_progress=self._emitters.notify_tool_call_in_progress,
        )

    @property
    def session(self) -> SessionState:
        """Current session state snapshot."""
        return self._session

    @property
    def gate(self) -> GateOrchestrator:
        """Gate orchestrator (needed by the approval handler in _app.py)."""
        return self._gate

    @property
    def session_id(self) -> str:
        """Identifier of the active Guide session."""
        return self._orch_session_id

    @property
    def session_name(self) -> str:
        """Human-readable name of the active session (from ``meta.json``)."""
        return self._transient.session_name

    @property
    def current_project(self) -> dict[str, str] | None:
        """The session's locked current project ``{root, name}``, or ``None``.

        Bound once (lazily) for Guided mode and immutable for the session.
        ``None`` while only Problem Solver has run.
        """
        return self._current_project

    def _require_layout(self) -> ProjectLayout:
        """Return the bound project layout, or raise if none is bound.

        Guards the Guided-only code paths (rollback, document finalization)
        that run only after :meth:`bind_project` has set ``self._layout``.
        """
        if self._layout is None:
            raise RuntimeError(
                "No current project is bound — Guided mode requires a project selection."
            )
        return self._layout

    def _agent_available(self, name: str) -> bool:
        try:
            self._registry.get(name)
            return True
        except AgentLoadError:
            return False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def start(
        self, session_id: str, resumed: bool, thinking_level: str | None = None
    ) -> None:
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
            thinking_level (str | None): For a brand-new session only, an
                explicit seed for ``_session.thinking_level`` instead of the
                active model's family default — a valid tier slug for the
                active model's thinking family, pre-validated by the caller
                (``hello``'s optional field, WS_PROTOCOL.md §4.1; built for
                the validator's RVP judge session, whose ``hello`` fires
                before there is anywhere else to attach the tier its
                preceding ``llm.select`` pinned). Ignored when *resumed*.
        """
        self._orch_session_id = session_id
        self._clear_llm_request_logs()
        self._transient.attach_session(session_id, resumed)

        if resumed:
            self._main_messages = self._history.load_main_messages()
            # Seed the gauge from an estimate until the first resumed turn yields
            # a measured token count.
            self._compactor.context_tokens = estimate_tokens(self._main_messages)
            # Restore per-session prefs so a resumed tab keeps its own mode
            # (the window no longer re-syncs a single global value on connect).
            self._session.autonomous = self._transient.autonomous
            self._session.workflow_mode = self._transient.workflow_mode
            self._session.edit_control = self._transient.edit_control
            self._session.command_control = self._transient.command_control
            # Re-validate against the *current* model rather than trusting the
            # persisted tier blindly — the shared local/cloud selection may
            # have changed while this session was closed (doc/SESSIONS.md).
            base_llm = self._current_base_llm()
            self._last_thinking_base_llm = base_llm
            self._session.thinking_level = self._thinking_level_for_model(
                base_llm, prefer=self._transient.thinking_level
            )
            self._transient.update(thinking_level=self._session.thinking_level)
            persisted = self._transient.current_project
            if persisted is not None:
                # Re-bind first so sub-agent-spawn replay and checkpointing have
                # the project's layout; a dangling non-spawn tool call resumes
                # fine without it.
                await self._bind_project(persisted["root"], persisted["name"], emit=False)
            # A turn interrupted mid tool-dispatch leaves a dangling assistant
            # ``tool_use`` with no following ``tool_result``. It must always be
            # resolved — otherwise the next LLM call sees a malformed sequence —
            # so resume is gated only on the dangling marker, not on a bound
            # project (which only sub-agent-spawn replay actually needs).
            if self._has_dangling_tool_use():
                self._resume_subsession_pending = True
            if not self._resume_subsession_pending:
                pending = self._transient.pending_prompt
                if pending is not None:
                    asyncio.create_task(
                        self._resume_pending_prompt(pending), name="kodo-resume-prompt"
                    )
        else:
            # Brand-new session: seed thinking_level from the caller's
            # explicit *thinking_level* if given and valid, else the active
            # model's family default — same reconciliation as the resumed
            # path, just against ``thinking_level`` instead of a persisted
            # value (doc/SESSIONS.md).
            base_llm = self._current_base_llm()
            self._last_thinking_base_llm = base_llm
            self._session.thinking_level = self._thinking_level_for_model(
                base_llm, prefer=thinking_level
            )
            self._transient.update(thinking_level=self._session.thinking_level)

        self._worker = asyncio.create_task(self._run_worker(), name="kodo-worker")
        _log.info(
            "Runtime worker started (guide_session=%s resumed=%s messages=%d "
            "project=%s resume_subsession=%s)",
            self._orch_session_id,
            resumed,
            len(self._main_messages),
            self._current_project["name"] if self._current_project else None,
            self._resume_subsession_pending,
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
            self._session_workspace.set_physical_root(Path(physical_root))
        self._session_workspace.set_folders({k: Path(v) for k, v in folders.items()})
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
        if self._current_project is not None:
            if self._current_project["root"] != str(Path(root).resolve()):
                await self._emitters.emit_error(
                    "The current project is fixed for this session and cannot be changed.",
                    recoverable=True,
                )
            return
        await self._bind_project(root, name, emit=True)

    async def _bind_project(self, root: str, name: str, *, emit: bool) -> None:
        """Validate and bind the project layout.

        There is no index to rebuild and no separate checkpoint manager to
        initialise: the checkpoint coordinator's mirrors (shared with Problem
        Solver) lazily scaffold on the project root the first time a
        mutating tool touches it, and a document's state lives entirely in
        its own ``.jsonl`` evolution log — read on demand, never rebuilt.

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
            await self._emitters.emit_error(str(exc), recoverable=True)
            return

        self._layout = layout
        self._current_project = {"root": str(project_root), "name": name}

        if emit:
            self._transient.update(current_project=self._current_project)
            await self._sink.send(
                Envelope.make_event(EVT_PROJECT_BOUND, dict(self._current_project))
            )
        _log.info("Current project bound: %s (%s)", name, project_root)

    async def _resume_pending_prompt(self, pending: dict[str, object]) -> None:
        """Re-surface a ``prompt.approval`` lost to a server restart.

        Approvals are engine-fired (document review), not tool calls, so an
        interrupted one has no dangling ``tool_use`` to resume through —
        re-fire the same prompt and feed the user's decision back to the Guide
        as a new input describing what was asked and how it was answered.
        ``ask_user`` questions no longer persist a pending prompt at all:
        their ``tool_use`` is flushed before dispatch, so the dangling-tool-use
        resume path (:meth:`_resume_main_turn`) re-drives the whole batch
        from scratch instead (a legacy persisted ``kind == "question"`` record
        is simply ignored here).
        """
        self._session.phase = "awaiting_user"
        await self._emitters.emit_state()

        kind = pending.get("kind")
        try:
            if kind == "approval":
                gate_type = str(pending.get("gate_type", ""))
                artifact_id = pending.get("artifact_id")
                summary = str(pending.get("summary", ""))
                approval = await self._gate.fire_approval(
                    gate_type,
                    artifact_id=artifact_id if isinstance(artifact_id, str) else None,
                    summary=summary,
                )
                text = f'(Resuming after restart) You previously requested approval: "{summary}". '
                text += f"The user responded: {approval.action}"
                if approval.feedback:
                    text += f" — feedback: {approval.feedback}"
            else:
                # Legacy/unknown pending kind (e.g. an old-format question
                # record): drop it so it does not linger across restarts.
                self._transient.update(pending_prompt=None)
                return
        except Exception:
            _log.exception("Failed to resume pending prompt")
            return

        await self._queue.put({"text": text, "request_id": ""})

    async def stop(self) -> None:
        """Cancel the in-flight turn and return the session to awaiting_user.

        Cancelling the worker task abandons whatever it had in flight, so
        before anything else this folds that into ``session.jsonl`` exactly
        like a normal turn boundary would (see
        :meth:`~._resume.ResumeMixin._persist_interrupted_turn`) — nothing
        streamed to the client is silently dropped from the record. The worker
        is then restarted: the one it replaces was the sole consumer of
        ``_queue``, so without this the engine would report "not running"
        (accepting input) while actually never processing another queued
        prompt.
        """
        was_running = self._session.phase == "running"
        entry_agent = self._session.agent
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if was_running and entry_agent is not None:
            self._persist_interrupted_turn(entry_agent)
        self._session.phase = "stopped"
        self._session.agent = None
        await self._emitters.emit_state()
        self._worker = asyncio.create_task(self._run_worker(), name="kodo-worker")
        _log.info("Runtime worker stopped by user (entry_agent=%s); session ready", entry_agent)

    # ------------------------------------------------------------------
    # Client-facing handlers
    # ------------------------------------------------------------------

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
        self._transient.update(prompt=clean_text)
        await self._queue.put(
            {"text": clean_text, "attachments": attachment_paths, "request_id": request_id}
        )

    async def handle_mode_set(self, autonomous: bool) -> None:
        """Toggle autonomous mode.

        Args:
            autonomous: New autonomous mode value.
        """
        self._session.autonomous = autonomous
        self._transient.update(autonomous=autonomous)
        await self._emitters.emit_state()

    async def handle_workflow_set(self, mode: str) -> None:
        """Select the top-level workflow that drives user prompts.

        Args:
            mode: ``"guided"`` (Guide + full Kodo pipeline), ``"problem_solving"``
                (the standalone Problem Solver agent), or the validator-only
                ``"judge"`` (the standalone Judge agent — scores a finished run
                for ``kodo.validator``; never sent by kodo-vsix, whose workflow
                picker only offers the first two). Unknown values fall back to
                ``"guided"``.
        """
        self._session.workflow_mode = mode if mode in ("problem_solving", "judge") else "guided"
        self._transient.update(workflow_mode=self._session.workflow_mode)
        await self._emitters.emit_state()

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
        self._session.edit_control = (
            value if value in ("review_all", "allow_all", "smart") else "smart"
        )
        self._transient.update(edit_control=self._session.edit_control)
        await self._emitters.emit_state()

    async def handle_command_control_set(self, value: str) -> None:
        """Set the Command Control posture.

        Mirrors the client exactly, same as :meth:`handle_edit_control_set`
        (the client forces ``"permissive"`` while Autonomous is in effect).
        State tracking only — enforcement deferred to M4.

        Args:
            value: ``"defensive"`` | ``"permissive"`` | ``"smart"``. Unknown
                values fall back to ``"smart"``.
        """
        self._session.command_control = (
            value if value in ("defensive", "permissive", "smart") else "smart"
        )
        self._transient.update(command_control=self._session.command_control)
        await self._emitters.emit_state()

    def _freeze_effective_modes(self) -> None:
        """Snapshot the two frozen toggles into their ``effective_*`` twins.

        Called once per prompt at dequeue (and on sub-session resume) so the
        guide and every sub-agent it spawns see one consistent value for the
        whole turn even if the user flips a toggle mid-run. Only ``autonomous``
        and ``workflow_mode`` are frozen — ``edit_control``/``command_control``
        are deliberately never frozen (the client owns them and may change them
        any time it is not locked by Autonomous mode).
        """
        self._session.effective_autonomous = self._session.autonomous
        self._session.effective_workflow_mode = self._session.workflow_mode

    async def handle_compact_now(self) -> None:
        """Enqueue a manual context-compaction request.

        Compaction mutates ``_main_messages``, so it is funnelled through the
        same single-consumer worker queue as prompts rather than run inline on
        the connection handler. The worker honours it only when the entry agent
        is idle and there is context to compact (see
        :meth:`~._compaction.ContextCompactor.run_manual_compaction`); a
        request that arrives mid-run simply waits its turn and is re-checked.
        """
        await self._queue.put({"kind": "compact"})

    async def handle_config_changed(self) -> None:
        """React to a window-global settings change (e.g. a model switch).

        The model selection lives in the singleton's ``~/.kodo/etc/settings.json`` and
        is read fresh per turn, so a switch normally takes effect lazily. This
        hook (fired by the ``config.reload`` handler for every live session) lets
        the engine act *immediately*: if the new model's context window is smaller
        than the live context, it compacts using the *current* model before the
        switch takes effect. It is funnelled through the worker queue so it never
        races an in-flight turn or another compaction.
        """
        await self._queue.put({"kind": "config_changed"})

    # ------------------------------------------------------------------
    # Checkpoint handlers (forwarded to the coordinator)
    # ------------------------------------------------------------------

    async def handle_checkpoint_undo(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Undo checkpoint *sha* in *root*'s mirror; return the updated state.

        See :meth:`~._checkpointing.CheckpointCoordinator.undo`.
        """
        return await self._checkpoints.undo(root, sha, resolution)

    async def handle_checkpoint_redo(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Redo checkpoint *sha* in *root*'s mirror; return the updated state.

        See :meth:`~._checkpointing.CheckpointCoordinator.redo`.
        """
        return await self._checkpoints.redo(root, sha, resolution)

    async def handle_checkpoint_rollback(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Move *root*'s current branch to checkpoint *sha*.

        See :meth:`~._checkpointing.CheckpointCoordinator.rollback`.
        """
        return await self._checkpoints.rollback(root, sha, resolution)

    async def handle_checkpoint_roll_forward(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Move *root*'s current branch forward to checkpoint *sha*.

        See :meth:`~._checkpointing.CheckpointCoordinator.roll_forward`.
        """
        return await self._checkpoints.roll_forward(root, sha, resolution)

    async def handle_checkpoint_list(self, root: str) -> CheckpointState:
        """The persisted :class:`CheckpointState` for *root* (UI hydration)."""
        return await self._checkpoints.state_for(root)

    # ------------------------------------------------------------------
    # Environment helpers (shared by dispatch, checkpointing, projects)
    # ------------------------------------------------------------------

    def _root_paths(self) -> tuple[RootPath, ...]:
        """The filesystem roots the run may operate within, mode-aware.

        Guided mode confines the agent to one project, so it reports just the
        bound project root. Problem Solver mode addresses the whole workspace, so
        it reports every open VS Code workspace folder (the map the extension
        keeps synced via ``workspace.folders``). When no folders have been pushed
        — e.g. a future console-only single-project run — it falls back to the
        physical root, keeping ``get_root_paths`` always non-empty.
        """
        if self._session.workflow_mode == "guided" and self._current_project is not None:
            cp = self._current_project
            return (RootPath(name=cp["name"], path=cp["root"]),)
        folders = self._session_workspace.folders
        if folders:
            return tuple(RootPath(name=name, path=str(p)) for name, p in folders.items())
        root = self._session_workspace.physical_root
        return (RootPath(name=root.name or str(root), path=str(root)),)

    @staticmethod
    def _util_paths() -> dict[str, Path]:
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

    def _make_resolver(self) -> PathResolver:
        """Pick the path resolver for the active workflow mode.

        Guided confines file/shell tools to the locked current project's root;
        Problem Solver resolves *logical* paths (workspace-folder-keyed) so it
        can address every project in the workspace.  In the degenerate case of a
        Guided run with no project bound (the extension should prevent this), it
        falls back to the logical resolver rather than crashing.
        """
        if self._session.workflow_mode == "guided" and self._layout is not None:
            return ProjectPathResolver(self._layout.root)
        return LogicalPathResolver(
            self._session_workspace.folders, self._session_workspace.physical_root
        )

    # ------------------------------------------------------------------
    # Rollback callback
    # ------------------------------------------------------------------

    async def _run_rollback(self, target_sha: str) -> None:
        """Roll the bound project's checkpoint mirror back and reset the session.

        Delegates to the same :meth:`RootMirrorManager.rollback` primitive
        Problem Solver already uses — there is no separate index to rebuild;
        every document's state is read on demand from whichever revision the
        mirror's working tree now reflects.

        Args:
            target_sha: Mirror commit SHA to roll back to.
        """
        project_root = self._require_layout().root
        _log.info("Rollback initiated: target_sha=%s", target_sha[:12])
        self._checkpoints.sync_roots()
        await self._checkpoints.mirrors.rollback(str(project_root), target_sha)
        # Session identity is owned by the driving window and is unchanged; the
        # rollback only invalidates the in-memory conversation, so reset it.
        self._main_messages = []
        self._replay_subsessions = None
        _log.info("Post-rollback: project %s restored to %s", project_root, target_sha[:12])

    # ------------------------------------------------------------------
    # Document finalization (accept/review flow)
    # ------------------------------------------------------------------

    async def _finalize_document(self, path: str) -> None:
        """Drive the post-accept flow for a document a critic just approved.

        Called only after ``document_feedback(accept=True)``. Autonomous mode
        auto-accepts immediately (mirroring every other gate when the user is
        away). Interactive mode fires the same approval gate
        ``request_user_review_artifact`` used to — now engine-driven — and
        records the user's decision: agreement writes ``review_result``
        (approve) then ``accepted``; feedback writes ``review_result``
        (reject) only, which the next ``run_author_critic_iteration`` round
        picks up as ``needs_revision``.
        """
        project_root = self._require_layout().root
        try:
            resolved = ProjectPathResolver(project_root).resolve(path)
        except PermissionError:
            _log.warning("finalize_document: cannot resolve path %r", path)
            return

        if self._session.effective_autonomous:
            await asyncio.to_thread(append_accepted, resolved, project_root)
            return

        approval = await self._gate.fire_approval(
            "document_review", artifact_id=path, summary=f"Review {path}"
        )
        if approval.action == "agree":
            await asyncio.to_thread(
                append_review_result, resolved, project_root, decision="approve", comment=""
            )
            await asyncio.to_thread(append_accepted, resolved, project_root)
        else:
            await asyncio.to_thread(
                append_review_result,
                resolved,
                project_root,
                decision="reject",
                comment=approval.feedback,
            )

    # ------------------------------------------------------------------
    # History rebuild (forwarded to the projector)
    # ------------------------------------------------------------------

    async def history_entries(self) -> list[dict[str, object]]:
        """Rebuild the full client-facing feed for a resumed session.

        See :meth:`~._history.HistoryProjector.history_entries`.
        """
        return await self._history.history_entries()

    # ------------------------------------------------------------------
    # Autonomous-mode kill switch + project creation
    # ------------------------------------------------------------------

    async def _disable_autonomous(self) -> None:
        """Disable autonomous mode and notify the client.

        Unlike a user toggle, this is an Guide decision that must take
        effect immediately, so it clears the frozen ``effective_autonomous`` as
        well — any sub-agent spawned later in this same prompt runs interactive.
        """
        self._session.autonomous = False
        self._session.effective_autonomous = False
        self._transient.update(autonomous=False)
        await self._emitters.emit_state()
        await self._sink.send(Envelope.make_event(EVT_AUTONOMOUS_CHANGED, {"autonomous": False}))

    async def handle_project_create(
        self, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        """Direct (non-tool) entry point backing the ``project.create`` message.

        Used by the VS Code "Create Project" command, which already has a
        concrete folder from its own picker dialog and so always supplies
        *path*; shares :meth:`_create_project` with the LLM-facing
        ``create_new_project`` tool. May raise :class:`ProjectLayoutError` if
        *path*'s ``kodo.md`` already exists and *force* is not set — the
        caller should ask the user to confirm overwrite and retry with
        ``force=True``.
        """
        return await self._create_project(name, path, force)

    async def _create_project(
        self, name: str, path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        """Scaffold a new project directory and add it to the workspace.

        Backs the ``create_new_project`` tool and the ``project.create``
        message (the VS Code "Create Project" command, which always supplies
        *path* from its own folder picker). When *path* is given it supersedes
        *name*: the project is laid out in that exact directory instead of a
        slug derived from *name*. Otherwise a slug-named directory is created
        under the session workspace root (auto-suffixed on collision). Either
        way, ``specs/``, ``src/``, ``test/`` and ``.kodo/``/``kodo.md`` are laid
        out via :meth:`ProjectLayout.init`, the checkpoint mirror is initialised
        (done by :meth:`RootMirrorManager.prepare`), the new folder is recorded
        in the session's logical-root map so ``get_root_paths`` sees it
        immediately, and ``EVT_WORKSPACE_ADD_FOLDER`` is pushed so the VS Code
        extension adds the directory to the open workspace (its resulting
        workspace-folders change re-pushes ``workspace.folders``, reconciling
        the map).

        Args:
            name: Human-readable project name. Used as the workspace-folder
                label and, when *path* is omitted, as the basis for the
                on-disk directory name. May be empty when *path* is given.
            path: Absolute directory to lay the project out in, superseding
                the slug-derived directory. The directory need not exist yet.
            force: When *path* is given and it already has a ``kodo.md``,
                overwrite it instead of raising. Ignored when *path* is
                omitted (a freshly reserved directory never has one).

        Returns:
            ``{"path": <absolute project dir>, "name": <workspace label>}``.

        Raises:
            ValueError: Neither *name* nor *path* was given.
            ProjectLayoutError: *path*'s ``kodo.md`` already exists and
                *force* is not set.
        """
        name = name.strip()
        if path:
            project_dir = Path(path)
            await asyncio.to_thread(ProjectLayout(project_dir).init, force=force)
        else:
            if not name:
                raise ValueError("create_project requires a non-empty 'name' or 'path'.")
            parent = self._session_workspace.physical_root
            slug = _slugify_project_name(name)
            project_dir = await asyncio.to_thread(self._reserve_project_dir, parent, slug)
            await asyncio.to_thread(ProjectLayout(project_dir).init)

        # Make the new root addressable before scaffolding so _root_paths() (and
        # thus the mirror's known-roots set) includes it.
        folders = self._session_workspace.folders
        label = name if name and name not in folders else project_dir.name
        folders[label] = project_dir
        self._session_workspace.set_folders(folders)

        self._checkpoints.sync_roots()
        await self._checkpoints.mirrors.prepare(project_dir)

        await self._sink.send(
            Envelope.make_event(EVT_WORKSPACE_ADD_FOLDER, {"path": str(project_dir), "name": label})
        )
        _log.info("create_new_project: scaffolded %s (label=%r)", project_dir, label)
        return {"path": str(project_dir), "name": label}

    @staticmethod
    def _reserve_project_dir(parent: Path, slug: str) -> Path:
        """Pick a free child directory of *parent* and create it (blocking)."""
        project_dir = _unique_child_dir(parent, slug)
        project_dir.mkdir(parents=True)
        return project_dir
