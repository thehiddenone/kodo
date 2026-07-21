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
            (Guide + full Kodo pipeline), ``"problem_solving"`` (the standalone
            Problem Solver agent), or the validator-only ``"judge"`` (the
            standalone Judge agent that scores a finished run for
            ``kodo.validator``; never sent by kodo-vsix).
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
            always exactly what the UI shows. **Enforced** for ``create_file``/
            ``edit_file`` only — read live per call by
            :class:`~kodo.tools.ToolDispatcher`'s edit-review gate
            (``prompt.edit_review``, WS_PROTOCOL.md §6.5b), independent of and
            always evaluated after ``command_control``'s security gate; not
            part of the security layer itself.
        command_control: How much Kodo restricts potentially risky commands —
            ``"defensive"`` (ask on Moderate+ calls) | ``"permissive"`` (allow
            below Critical) | ``"smart"`` (judge per call; the default).
            Mirrors the client exactly, same as ``edit_control`` (auto-forced
            to ``"permissive"`` while Autonomous is in effect). **Enforced**:
            this is the security layer's posture, read live per tool call by
            the dispatcher (doc/SECURITY.md).
        thinking_level: The session's reasoning-tier slug for the currently
            active *local* model's thinking family (``kodo.llms.
            local_thinking_family``/``local_thinking_tiers``) — ``""`` while
            on a cloud model or a local model with no thinking family (e.g.
            Qwen3-Coder-Next-80B, or a custom entry). Unlike
            ``edit_control``/``command_control`` this is not a fixed enum: the
            valid value set is model-dependent, so the engine validates every
            change against the active model's family rather than mirroring
            the client unconditionally (doc/SESSIONS.md). A brand-new session
            seeds it from the active model's family default (the Qwen family
            defaults to ``"unlimited"``, GPT-OSS to ``"medium"``), and a
            mid-session model switch to a different thinking family
            re-derives it the same way (``WorkflowEngine.
            _sync_thinking_level_to_model``).
        security_rules: This session's Phase 2 "always allow" grants
            (doc/SECURITY_RULES_PLAN.md §2) — ``(executable, subcommand)``
            shapes the security layer's rule engine may silently allow
            instead of asking. Never frozen (read live per ``run_command``
            call, like ``command_control``); mirrors
            ``TransientStore.security_rules`` for crash-resume, the same
            relationship ``command_control`` has to its transient twin.
        security_path_rules: The workspace-escape sibling of
            ``security_rules`` (doc/SECURITY_RULES_PLAN.md §2.7) —
            ``(executable, resolved_absolute_path)`` shapes granted for a
            non-destructive command (read-only/``cd``) whose only issue was
            targeting a path outside the workspace. Same never-frozen,
            mirrors-``TransientStore.security_path_rules`` relationship as
            ``security_rules``; kept as a separate field rather than folded
            in since the two rule kinds are matched with different semantics.
        awaiting_first_chunk: ``True`` from the moment an ``llm.turn_start``
            is sent until the first ``ThinkingDelta``/``TokenDelta``/
            ``ToolCallArgDelta`` of that call arrives (or the stream ends
            with none at all). ``phase == "running"`` alone can't tell a
            reconnecting client whether to show the "awaiting response"
            spinner — it stays `"running"` for the whole multi-round tool-use
            loop, including while mid-stream or mid-tool-call. This narrower
            flag is what lets a fresh `hello`/`state` snapshot reconstruct
            that spinner correctly instead of only ever setting it from the
            live (and reconnect-losable) `llm.turn_start` event.
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
    thinking_level: str = ""
    security_rules: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    security_path_rules: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    awaiting_first_chunk: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for wire-protocol events.

        The two frozen toggles (``autonomous``/``workflow_mode``) emit both the
        user-facing *selected* value and the per-prompt frozen *effective* value
        so the client can render each as "in effect" or "queued for the next
        prompt". ``edit_control``/``command_control``/``thinking_level`` are
        never frozen — only the single current value is emitted for each.

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
            "thinking_level": self.thinking_level,
            "awaiting_first_chunk": self.awaiting_first_chunk,
        }
