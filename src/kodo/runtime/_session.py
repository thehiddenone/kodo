"""Per-session runtime metadata."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

__all__ = ["SessionState"]

# Valid phase values per WS_PROTOCOL.md §5.1
Phase = str  # "intake" | "running" | "awaiting_user" | "stopped" | "done" | "error"


@dataclass
class SessionState:
    """Mutable state for one Kodo session.

    The runtime engine owns this object and updates it as work progresses.
    It is intentionally mutable (not frozen) because the engine writes it
    frequently.

    Attributes:
        session_id: Unique session identifier.
        phase: Current wire-protocol phase (WS_PROTOCOL.md §5.1).
        agent: Name of the currently active sub-agent, if any.
        component: Responsibility code currently under work, if any.
        autonomous: User-facing autonomous mode. Set the instant the user
            toggles it and reported to the client; it reflects the mode the
            *next* prompt will run under, which may differ from the prompt
            already in flight.
        effective_autonomous: The mode the *current* prompt actually runs
            under. The engine freezes it from ``autonomous`` when it dequeues a
            prompt, so every agent and tool in that prompt sees one consistent
            value even if the user toggles mid-run. Tools read this, never
            ``autonomous``.
        workflow_mode: Which top-level workflow drives prompts — ``"guided"``
            (Guide + full Kodo pipeline) or ``"problem_solving"``
            (the standalone Problem Solver agent).
        effective_workflow_mode: The workflow the *current* prompt runs under,
            frozen alongside ``effective_autonomous`` at dequeue. Lets the client
            tell "in effect" from "queued for the next prompt" while a turn runs.
        edit_control: How Kodo handles file edits —
            ``"review_all"`` (pause for sign-off on every edit) |
            ``"allow_all"`` (apply without pausing) | ``"smart"`` (decide per
            edit; the default). Unlike the two frozen toggles above this is
            **never** frozen: the client owns it, drives the value (auto-forcing
            ``"allow_all"`` while Autonomous mode is in effect), and the engine
            simply mirrors whatever the client last sent so its stored value is
            always exactly what the UI shows. (State tracking only — no edit
            gate is enforced; not part of the security layer.)
        command_control: How much Kodo restricts potentially risky commands —
            ``"defensive"`` (ask on Moderate+ calls) | ``"permissive"`` (allow
            below Critical) | ``"smart"`` (judge per call; the default).
            Mirrors the client exactly, same as ``edit_control`` (auto-forced
            to ``"permissive"`` while Autonomous is in effect). **Enforced**:
            this is the security layer's posture, read live per tool call by
            the dispatcher (doc/SECURITY.md).
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: Phase = "intake"
    agent: str | None = None
    component: str | None = None
    autonomous: bool = False
    effective_autonomous: bool = False
    workflow_mode: str = "guided"
    effective_workflow_mode: str = "guided"
    edit_control: str = "smart"
    command_control: str = "smart"

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for wire-protocol events.

        The two frozen toggles (``autonomous``/``workflow_mode``) emit both the
        user-facing *selected* value and the per-prompt frozen *effective* value
        so the client can render each as "in effect" or "queued for the next
        prompt". ``edit_control``/``command_control`` are never frozen — only the
        single mirrored value is emitted.

        Returns:
            dict[str, object]: JSON-serialisable state snapshot.
        """
        return {
            "phase": self.phase,
            "current_agent": {"name": self.agent, "component": self.component}
            if self.agent
            else None,
            "autonomous": self.autonomous,
            "effective_autonomous": self.effective_autonomous,
            "workflow_mode": self.workflow_mode,
            "effective_workflow_mode": self.effective_workflow_mode,
            "edit_control": self.edit_control,
            "command_control": self.command_control,
        }
