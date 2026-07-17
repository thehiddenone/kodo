"""Injected context and structural protocols for tool handlers.

Every tool handler receives a single :class:`ToolContext` carrying the
collaborators it may need plus the per-run mutable state (``stop_requested``,
``returned_output``).  The collaborators that live *above* this package in the
import graph — the approval/question gate, the session state, and every
engine-side operation a tool can trigger — are expressed here as **structural
Protocols** so that ``kodo.tools`` never imports ``runtime`` (or any T3+
package).  The runtime's concrete ``GateOrchestrator`` / ``SessionState`` and a
single :class:`EngineServices` adapter satisfy these protocols by shape and are
injected by the engine.

See [[feedback-tools-layer]]: ``kodo.tools`` may import only T0/T1/T2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ._paths import PathResolver

__all__ = [
    "ApprovalLike",
    "EngineServices",
    "GateLike",
    "PermissionLike",
    "PermissionPartLike",
    "RootPath",
    "SecurityDecisionLike",
    "SecurityLike",
    "SessionLike",
    "ToolContext",
]


@dataclass(frozen=True)
class RootPath:
    """One filesystem root the running agent may operate within.

    In Guided mode there is exactly one — the bound project root.  In Problem
    Solver mode there is one per open VS Code workspace folder.  ``get_root_paths``
    returns these; ``find_files`` / ``find_text_in_files`` take an ``root``
    matching one of the ``path`` values.

    Attributes:
        name: Human/logical label (the project name in Guided, the workspace
            folder's display name in Problem Solver).
        path: Absolute path to the root directory.
    """

    name: str
    path: str


class ApprovalLike(Protocol):
    """Structural shape of a user's response to an approval gate.

    Read-only (``@property``) so it works as a covariant method return type:
    runtime's concrete ``ApprovalResponse`` satisfies it.
    """

    @property
    def action(self) -> str: ...

    @property
    def feedback(self) -> str: ...


class PermissionLike(Protocol):
    """Structural shape of a user's response to a permission prompt.

    Read-only (``@property``) so it works as a covariant method return type:
    runtime's concrete ``PermissionResponse`` satisfies it.
    """

    @property
    def action(self) -> str:
        """``'allow'`` or ``'deny'``."""
        ...

    @property
    def feedback(self) -> str:
        """Optional free-text the user attached to the decision."""
        ...

    @property
    def remember(self) -> tuple[str | None, ...]:
        """One entry per part the server offered (``GateLike.fire_permission``'s
        ``parts``, same order/length): ``'session'`` / ``'global'`` where the
        user chose to permanently allow that part's offered rule shape, else
        ``None`` (doc/SECURITY_RULES_PLAN.md §2.6). ``ToolDispatcher`` only
        acts on an entry when the corresponding part actually carried a
        ``rule_offer`` — a stray value here for a non-offered part is
        ignored, never trusted blindly from the wire."""
        ...


class PermissionPartLike(Protocol):
    """Structural shape of one elementary command within a compound
    ``run_command`` ask that still needs the user's attention.

    Satisfied by :class:`kodo.security.AskPart` (by shape, no inheritance —
    mirrors how :class:`SecurityDecisionLike` decouples ``kodo.tools`` from
    ``kodo.security``).
    """

    @property
    def reason(self) -> str:
        """One user-facing sentence explaining why this part asks."""
        ...

    @property
    def rule_offer(self) -> tuple[str, str] | None:
        """The ``(executable, subcommand)`` shape this part may be
        permanently allowed as, or ``None`` when not offer-eligible
        (doc/SECURITY_RULES_PLAN.md §2.2/§2.6)."""
        ...


class SecurityDecisionLike(Protocol):
    """Structural shape of the security layer's verdict on one tool call.

    Satisfied by :class:`kodo.security.SecurityDecision` (by shape).
    """

    @property
    def action(self) -> str:
        """``'allow'`` (dispatch proceeds) or ``'ask'`` (prompt the user)."""
        ...

    @property
    def reason(self) -> str:
        """One sentence explaining the verdict (shown in the prompt)."""
        ...

    @property
    def parts(self) -> tuple[PermissionPartLike, ...]:
        """For a ``run_command`` ask, every elementary command that still
        needs the user's attention, in command order, deduplicated by shape —
        empty for every other tool and for any ``"allow"``
        (doc/SECURITY_RULES_PLAN.md §2.6). The source of truth the dispatcher
        forwards to ``GateLike.fire_permission``."""
        ...


class SecurityLike(Protocol):
    """Structural shape of the security layer.

    Satisfied by :class:`kodo.security.SecurityLayer` (by shape, no
    inheritance — ``kodo.tools`` never imports ``kodo.security``, mirroring
    how ``GateLike`` decouples it from ``runtime``).
    """

    async def evaluate(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        command_control: str,
        autonomous: bool,
        default_cwd: str,
        roots: tuple[str, ...],
        session_rules: frozenset[tuple[str, str]] = frozenset(),
    ) -> SecurityDecisionLike:
        """Judge one tool call: allow it, or ask the user for permission.

        ``session_rules`` is this session's Phase 2 "always allow" grants
        (``SessionLike.security_rules``); the layer merges in the
        process-wide global store itself.
        """
        ...


class GateLike(Protocol):
    """Structural shape of the approval/question gate.

    Satisfied by :class:`kodo.runtime.GateOrchestrator` (by shape, no
    inheritance).
    """

    async def fire_questions(
        self,
        questions: list[dict[str, object]],
        tool_call_id: str = "",
    ) -> list[dict[str, object]]:
        """Surface a batch of questions to the user; block until they confirm.

        ``questions`` is the normalized ``ask_user`` batch
        (``{"question", "kind", "options"}`` per entry); ``tool_call_id`` is
        the calling ``tool_use`` block's id, forwarded on the wire so the
        client can correlate the interactive panel with the persisted feed
        entry. Returns one ``{"selected": [str, ...], "free_text": str | None}``
        per question, in order.
        """
        ...

    async def fire_approval(
        self,
        gate_type: str,
        *,
        artifact_id: str | None = None,
        summary: str = "",
    ) -> ApprovalLike:
        """Surface an approval gate to the user and block until they respond.

        ``artifact_id`` is a historical name on the wire protocol — its value
        is now a Guided-mode document path, not a workspace artifact ID.
        """
        ...

    async def fire_permission(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        external_name: str,
        risk: str,
        intent: str,
        reason: str,
        params: list[dict[str, str]],
        recovered: bool = False,
        parts: tuple[PermissionPartLike, ...] = (),
    ) -> PermissionLike:
        """Surface a security permission prompt and block until the user decides.

        Emitted when the security layer's verdict on a tool call is ``ask``
        (``prompt.permission``, WS_PROTOCOL.md §6.5). ``params`` is the
        customer-visible parameter preview (``{"name", "value"}`` rows);
        ``risk`` is the tool's :class:`~kodo.toolspecs.SecurityImpact` label.
        ``recovered`` is ``True`` when the prompt is for a salvaged
        malformed tool call (the client renders a distinct banner).
        ``parts`` is every elementary command within the call that still
        needs the user's attention (one for an ordinary single-command ask,
        several for a compound pipeline/`&&`/`;` chain — doc/SECURITY_RULES_PLAN.md
        §2.6); the client shows one "always allow — this session / all
        sessions" checkbox pair per part whose ``rule_offer`` is set. Returns
        the user's ``allow``/``deny`` decision plus optional feedback and a
        ``remember`` tuple parallel to ``parts``.
        """
        ...


class SessionLike(Protocol):
    """Structural shape of the session state a tool may read or write.

    Satisfied by :class:`kodo.runtime.SessionState`.

    ``effective_autonomous`` is the mode the *current prompt* runs under. It is
    frozen by the engine at the start of each prompt and never changes mid-run,
    so every tool in a prompt sees one consistent value (unlike the user-facing
    ``SessionState.autonomous``, which may already reflect a toggle queued for
    the *next* prompt).

    ``command_control`` is the never-frozen Command Control posture
    (``"permissive"`` / ``"defensive"`` / ``"smart"``) — read live per tool
    call by the security gate in :class:`~kodo.tools.ToolDispatcher`, so a
    mid-turn toggle takes effect on the very next call.

    ``security_rules`` is this session's Phase 2 "always allow" grants —
    ``(executable, subcommand)`` shapes, read live per ``run_command`` call
    the same way ``command_control`` is (a rule granted mid-session applies
    to the very next matching call). The layer merges the process-wide
    global store in on top of this set itself.
    """

    phase: str
    effective_autonomous: bool
    command_control: str
    security_rules: frozenset[tuple[str, str]]


class EngineServices(Protocol):
    """Structural shape of every engine-side operation a tool can trigger.

    The tools live below ``runtime`` in the import graph, so the operations
    they delegate upward — spawning sub-agents, rolling back, promoting a
    completed artifact, and pushing client notifications — are inverted through
    this single protocol.  The engine provides one concrete adapter
    (``_EngineServices``) that satisfies it by shape, keeping agent loading and
    the LLM loop in ``runtime`` while the dispatch logic stays in ``kodo.tools``.
    """

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Run a leaf sub-agent and return its structured result.

        ``task_input`` is the structured task validated against the sub-agent's
        ``input_schema``; the return is the structured output the sub-agent
        produced via ``return_result`` (its ``output_schema``).

        ``caller`` is the name of the agent making the call (the running agent —
        not necessarily the guide). The engine gates the spawn against
        that caller's declared sub-agent allow-list and raises ``PermissionError``
        when ``name`` is not permitted.
        """
        ...

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        """Run the ``toolchain_depsmgr`` sub-agent and return its structured result.

        The ungated sibling of :meth:`run_subagent`, dedicated to the
        ``toolchain_deps`` tool: holding that tool is the authorization, so the
        dependency-management sub-agent is spawned directly (a fixed engine-side
        name) without consulting any caller's ``subagents:`` allow-list and never
        appears in a ``run_subagent`` roster. ``task_input`` conforms to the
        sub-agent's ``input_schema``; the return is its ``output_schema`` result
        (carrying the ``status`` the tool translates, including
        ``dependencies_md_missing``).
        """
        ...

    async def run_web_search_agent(
        self, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        """Run the ``web_search`` agent and return ``{"themes": [...], "note": "..."}``.

        Backs the ``web_search`` tool (doc/WEB_SEARCH.md). Like
        :meth:`run_dependency_manager` it is ungated — holding ``web_search``
        is the authorization, so the fixed engine-side agent never sits in any
        caller's ``subagents:`` allow-list. Unlike a ``run_subagent`` spawn it
        opens **no subsession** (``web_search`` is typically called from a
        sub-agent, and subsessions do not nest): the engine drives a silent,
        multi-round tool-calling turn (the agent plans its own discovery/read/
        synthesis loop via ``query_search_engine``/``read_webpage``) bounded by
        ``task_input["timeout"]``, and only the structured result comes back.
        ``task_input`` conforms to the sub-agent's ``input_schema`` (``query``,
        ``max_themes``, ``timeout``).

        ``tool_call_id`` (the ``web_search`` call's own ``tool_use`` block id)
        correlates the agent's live narration (``web_search.note``) and its
        persisted notes sidecar file with that call's card — see
        doc/WEB_SEARCH.md §6.
        """
        ...

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        path: str,
        input_paths: dict[str, str],
        instructions: str,
        for_revision: bool,
    ) -> dict[str, object]:
        """Run one Author/Critic round over a real file; return ``{path, status, concerns}``.

        ``path`` is the file the Author writes/revises and the Critic reviews;
        ``for_revision`` is ``True`` when ``path`` already exists and this round
        revises it. ``caller`` is the agent making the call; the engine gates
        both ``author_name`` and ``critic_name`` against that caller's
        allow-list and raises ``PermissionError`` when either is not permitted.
        """
        ...

    async def rollback(self, target_sha: str) -> None:
        """Roll the project's checkpoint mirror back to ``target_sha``."""
        ...

    async def disable_autonomous_mode(self) -> None:
        """Turn off autonomous mode and notify the client."""
        ...

    async def create_project(self, name: str, path: str | None = None) -> dict[str, object]:
        """Scaffold a new project directory and add it to the workspace.

        When ``path`` is given it supersedes ``name``: the project is laid out
        in that exact directory. Otherwise the engine derives a filesystem-safe
        directory name from ``name`` and creates that directory under the
        session workspace root (auto-suffixing on collision). Either way it
        lays out ``specs/``, ``src/``, ``test/`` and the ``.kodo/`` marker +
        checkpoint mirror, then asks the VS Code extension to add the
        directory to the open workspace.

        Returns ``{"path": <absolute project dir>, "name": <workspace label>}``.

        Raises:
            ProjectLayoutError: ``path``'s ``kodo.md`` already exists.
        """
        ...

    async def init_project(self, path: str) -> dict[str, object]:
        """Augment an existing directory with Kodo's project layout and git mirror.

        Unlike :meth:`create_project`, ``path`` must already exist. The
        engine judges the directory empty when it holds no entries besides
        dotfiles/dot-directories (``.git/``, ``.gitignore``, ...); only then
        are ``specs/``, ``src/``, ``test/`` created. Either way ``.kodo/``
        (with ``kodo.md``) and the checkpoint git mirror — with its mandatory
        baseline commit — are always created, and the directory is added to
        the open VS Code workspace unless it is already one of the session's
        registered folders.

        Returns ``{"path": ..., "name": <workspace label>, "scaffolded":
        <bool>}``.

        Raises:
            ProjectLayoutError: ``path`` does not exist, or its ``.kodo/``
                already exists.
        """
        ...

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        """Tell the client this call has cleared the security gate and its
        tool handler is about to run.

        Fired by :class:`ToolDispatcher` right after the gate resolves
        (allowed outright, or the user granted permission) — the moment a
        run_command timeout genuinely starts. Lets the client defer its
        "waiting for tool output" timeout animation past whatever judging
        round or permission wait preceded this point (doc/SECURITY.md §6).
        """
        ...

    async def add_security_rule(self, scope: str, executable: str, subcommand: str) -> None:
        """Persist a Phase 2 "always allow" rule at the given scope.

        Fired by :class:`ToolDispatcher` when a permission response carries
        a ``remember`` choice for a call whose decision offered a
        ``rule_offer`` (doc/SECURITY_RULES_PLAN.md §2.3/§2.4). ``scope`` is
        ``"session"`` (this session only, survives crash-resume) or
        ``"global"`` (every session, this machine); any other value is a
        no-op. ``executable``/``subcommand`` are exactly the offered shape —
        never re-derived from the live tool call, so a stale or manipulated
        client response can at most grant the *specific* shape the server
        already decided was safe to generalize.
        """
        ...


@dataclass
class ToolContext:
    """Everything a tool handler may touch, plus per-run mutable state.

    One instance is created per agent run (guide or leaf) by the engine
    and shared across every tool call in that run.  Handlers mutate
    ``stop_requested`` and ``returned_output``; the owning
    :class:`~kodo.tools.ToolDispatcher` exposes them back to the engine.

    The autonomous mode a handler should honour is read from
    ``session.effective_autonomous`` (frozen for the whole prompt), not stored
    on the context, so no per-run snapshot can drift from the session.

    Attributes:
        mode: The run's workflow mode, ``"guided"`` or ``"problem_solving"``.
            Frozen for the whole prompt, mirroring
            ``session.effective_workflow_mode``. Used to gate Guided-only
            tools (``guided_dev_status``) and to tag ``new_revision`` jsonl
            entries with which workflow produced them.
        project_root: The bound project's root, or ``None`` if no project is
            bound. Populated in both modes whenever a project is bound (Guided
            always has one; Problem Solver may carry one over from a prior
            Guided binding) — `kodo.guided_state` calls are skipped uniformly
            when this is ``None``, with no per-tool mode branch needed.
        resolver: Path resolver for the native file/shell tools — a
            project-confined resolver in Guided mode, a logical (workspace-folder
            keyed) resolver in Problem Solver mode.
        gate: Approval/question gate (protocol).
        security: The security layer (protocol), consulted by the dispatcher
            before every dispatch; ``None`` disables gating (tests/legacy
            callers).
        session: Session state (protocol); ``effective_autonomous`` is the
            frozen mode for this prompt.
        services: Engine-side operations (protocol): sub-agent launch,
            author/critic iteration, rollback, mode disable, project creation.
        agent_name: Name of the running agent (used as the jsonl ``author``/
            ``reviewer`` field).
        session_id: Session ID for this run.
        root_paths: Filesystem roots the agent may operate within, computed
            mode-aware by the engine (the bound project in Guided; every open
            workspace folder in Problem Solver).  Surfaced by ``get_root_paths``.
        util_paths: Absolute paths to the bundled third-party CLI utils keyed by
            manifest name (``"fd"``, ``"ripgrep"``), or absent when a util is not
            yet installed.  Injected by the engine from ``kodo.binutils`` so the
            search tools never import that package directly (tier rule).
        output_schema: The running sub-agent's declared ``output_schema`` (from
            its :class:`~kodo.subagents.SubAgentSpec`), injected by the engine so
            ``return_result`` can validate/normalize the agent's result against
            it. ``None`` for the entry agents (guide/problem_solver), which have
            no spec and never call ``return_result``.
        current_tool_use_id: The ``tool_use`` block id of the call currently
            being handled, set by :class:`~kodo.tools.ToolDispatcher` before
            each dispatch. Lets a handler correlate out-of-band client frames
            with its own persisted tool call (``ask_user`` forwards it on the
            ``prompt.question`` request). Empty for legacy callers that
            dispatch without an id.
        stop_requested: Set ``True`` by ``escalate_blocker`` to end the run.
        returned_output: The normalized result the sub-agent passed to
            ``return_result`` (with the engine-owned ``schema_compliance`` field),
            or ``None`` until it calls it. Read back by the engine after the run.
        deadline: Unix timestamp this run must wrap up by, or ``None`` if the
            run is not time-boxed. Populated only for the ``web_search``
            agent's dispatcher (from its caller-supplied, 600s-capped
            ``timeout``); read by ``remaining_time`` and ``wait`` (which
            clamps its own sleep so it never sleeps past the deadline).
    """

    resolver: PathResolver
    gate: GateLike
    session: SessionLike
    services: EngineServices
    agent_name: str
    session_id: str
    security: SecurityLike | None = None
    mode: str = "problem_solving"
    project_root: Path | None = None
    root_paths: tuple[RootPath, ...] = ()
    util_paths: dict[str, Path] = field(default_factory=dict)
    output_schema: dict[str, object] | None = None
    current_tool_use_id: str = ""
    stop_requested: bool = False
    returned_output: dict[str, object] | None = None
    deadline: float | None = None
