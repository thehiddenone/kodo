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
    "QuestionLike",
    "RootPath",
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


class QuestionLike(Protocol):
    """Structural shape of a user's answer to a question.

    Read-only (``@property``) so it works as a covariant method return type:
    runtime's concrete ``QuestionResponse`` satisfies it.
    """

    @property
    def answer_text(self) -> str: ...

    @property
    def choice_key(self) -> str: ...


class ApprovalLike(Protocol):
    """Structural shape of a user's response to an approval gate.

    Read-only (``@property``) for the same covariance reason as
    :class:`QuestionLike`.
    """

    @property
    def action(self) -> str: ...

    @property
    def feedback(self) -> str: ...


class GateLike(Protocol):
    """Structural shape of the approval/question gate.

    Satisfied by :class:`kodo.runtime.GateOrchestrator` (by shape, no
    inheritance).
    """

    async def fire_question(
        self,
        question: str,
        mode: str,
        choices: list[dict[str, str]] | None = None,
    ) -> QuestionLike:
        """Surface a question to the user and block until they respond."""
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


class SessionLike(Protocol):
    """Structural shape of the session state a tool may read or write.

    Satisfied by :class:`kodo.runtime.SessionState`.

    ``effective_autonomous`` is the mode the *current prompt* runs under. It is
    frozen by the engine at the start of each prompt and never changes mid-run,
    so every tool in a prompt sees one consistent value (unlike the user-facing
    ``SessionState.autonomous``, which may already reflect a toggle queued for
    the *next* prompt).
    """

    phase: str
    effective_autonomous: bool


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
        stop_requested: Set ``True`` by ``escalate_blocker`` to end the run.
        returned_output: The normalized result the sub-agent passed to
            ``return_result`` (with the engine-owned ``schema_compliance`` field),
            or ``None`` until it calls it. Read back by the engine after the run.
    """

    resolver: PathResolver
    gate: GateLike
    session: SessionLike
    services: EngineServices
    agent_name: str
    session_id: str
    mode: str = "problem_solving"
    project_root: Path | None = None
    root_paths: tuple[RootPath, ...] = ()
    util_paths: dict[str, Path] = field(default_factory=dict)
    output_schema: dict[str, object] | None = None
    stop_requested: bool = False
    returned_output: dict[str, object] | None = None
