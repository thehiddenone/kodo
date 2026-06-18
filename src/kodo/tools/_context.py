"""Injected context and structural protocols for tool handlers.

Every tool handler receives a single :class:`ToolContext` carrying the
collaborators it may need plus the per-run mutable state (``published_ids``,
``stop_requested``).  The collaborators that live *above* this package in the
import graph — the approval/question gate, the session state, and the
sub-agent launcher — are expressed here as **structural Protocols** so that
``kodo.tools`` never imports ``runtime`` (or any T3+ package).  The runtime's
concrete ``GateOrchestrator`` / ``SessionState`` and a small engine adapter
satisfy these protocols by shape and are injected by the engine.

See [[feedback-tools-layer]]: ``kodo.tools`` may import only T0/T1/T2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from kodo.workspace import ProjectIndex, Workspace

__all__ = [
    "ApprovalLike",
    "GateLike",
    "QuestionLike",
    "SessionLike",
    "SubagentRunner",
    "ToolContext",
]


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
    """Structural shape of the mutable session state a tool may write.

    Satisfied by :class:`kodo.runtime.SessionState`.
    """

    phase: str


class SubagentRunner(Protocol):
    """Structural shape of the sub-agent launcher injected from the engine.

    The tools that spawn sub-agents (``run_subagent``,
    ``run_author_critic_iteration``) depend only on this protocol; the engine
    provides a concrete adapter.  This keeps agent loading and the LLM loop in
    ``runtime`` while the dispatch logic stays in ``kodo.tools``.
    """

    async def run_subagent(
        self, name: str, task_message: str, input_artifact_ids: list[str]
    ) -> list[str]:
        """Run a leaf sub-agent and return the artifact IDs it published."""
        ...

    async def run_author_critic_iteration(
        self,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        previous_artifact_id: str | None,
    ) -> dict[str, object]:
        """Run one Author/Critic round and return ``{artifact_id, verdict, concerns}``."""
        ...


@dataclass
class ToolContext:
    """Everything a tool handler may touch, plus per-run mutable state.

    One instance is created per agent run (orchestrator or leaf) by the engine
    and shared across every tool call in that run.  Handlers mutate
    ``published_ids`` and ``stop_requested``; the owning
    :class:`~kodo.tools.ToolDispatcher` exposes them back to the engine.

    Attributes:
        workspace: Shared artifact store.
        index: Live in-memory artifact index.
        gate: Approval/question gate (protocol).
        session: Mutable session state (protocol).
        runner: Sub-agent launcher (protocol).
        rollback_fn: Callback that executes the rollback procedure.
        complete_fn: Callback that promotes and marks an artifact completed.
        agent_name: Name of the running agent (used as artifact author).
        session_id: Session ID attached to published artifacts.
        autonomous: Whether autonomous mode is active.
        published_ids: Artifact IDs published during this run (mutated by
            ``publish_artifact``).
        stop_requested: Set ``True`` by ``escalate_blocker`` to end the run.
    """

    workspace: Workspace
    index: ProjectIndex
    gate: GateLike
    session: SessionLike
    runner: SubagentRunner
    rollback_fn: Callable[[str], Awaitable[None]]
    complete_fn: Callable[[str], Awaitable[None]]
    agent_name: str
    session_id: str
    autonomous: bool
    published_ids: list[str] = field(default_factory=list)
    stop_requested: bool = False
