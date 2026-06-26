"""Injected context and structural protocols for tool handlers.

Every tool handler receives a single :class:`ToolContext` carrying the
collaborators it may need plus the per-run mutable state (``published_ids``,
``stop_requested``).  The collaborators that live *above* this package in the
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

from kodo.workspace import ProjectIndex, Workspace

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
        """Surface an approval gate to the user and block until they respond."""
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
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        """Run one Author/Critic round and return ``{artifact_id, verdict, concerns}``.

        ``for_revision_artifact_ids`` are the prior Author outputs to revise (empty
        on the first round). ``caller`` is the agent making the call; the engine
        gates both ``author_name`` and ``critic_name`` against that caller's
        allow-list and raises ``PermissionError`` when either is not permitted.
        """
        ...

    async def rollback(self, target_sha: str) -> None:
        """Roll the mirror back to ``target_sha`` and rebuild session state."""
        ...

    async def complete_artifact(self, artifact_id: str) -> None:
        """Promote a gate-passed artifact and flip its index entry to completed."""
        ...

    async def disable_autonomous_mode(self) -> None:
        """Turn off autonomous mode and notify the client."""
        ...

    async def post_update(self, message: str) -> None:
        """Send a non-blocking progress update to the client."""
        ...


@dataclass
class ToolContext:
    """Everything a tool handler may touch, plus per-run mutable state.

    One instance is created per agent run (guide or leaf) by the engine
    and shared across every tool call in that run.  Handlers mutate
    ``published_ids`` and ``stop_requested``; the owning
    :class:`~kodo.tools.ToolDispatcher` exposes them back to the engine.

    The autonomous mode a handler should honour is read from
    ``session.effective_autonomous`` (frozen for the whole prompt), not stored
    on the context, so no per-run snapshot can drift from the session.

    Attributes:
        workspace: Shared artifact store.
        index: Live in-memory artifact index.
        resolver: Path resolver for the native file/shell tools — a
            project-confined resolver in Guided mode, a logical (workspace-folder
            keyed) resolver in Problem Solver mode.
        gate: Approval/question gate (protocol).
        session: Session state (protocol); ``effective_autonomous`` is the
            frozen mode for this prompt.
        services: Engine-side operations (protocol): sub-agent launch,
            rollback, artifact completion, mode disable, client updates.
        agent_name: Name of the running agent (used as artifact author).
        session_id: Session ID attached to published artifacts.
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
        published_ids: Artifact IDs published during this run (mutated by
            ``publish_artifact``).
        stop_requested: Set ``True`` by ``escalate_blocker`` to end the run.
        returned_output: The normalized result the sub-agent passed to
            ``return_result`` (with the engine-owned ``schema_compliance`` field),
            or ``None`` until it calls it. Read back by the engine after the run.
    """

    workspace: Workspace
    index: ProjectIndex
    resolver: PathResolver
    gate: GateLike
    session: SessionLike
    services: EngineServices
    agent_name: str
    session_id: str
    root_paths: tuple[RootPath, ...] = ()
    util_paths: dict[str, Path] = field(default_factory=dict)
    output_schema: dict[str, object] | None = None
    published_ids: list[str] = field(default_factory=list)
    stop_requested: bool = False
    returned_output: dict[str, object] | None = None
