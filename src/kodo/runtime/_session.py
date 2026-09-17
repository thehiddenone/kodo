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
        top_agent: Which top-level agent drives prompts, as the user selected
            it. Holds an agent *name* (``"guide"``, ``"problem_solver"``,
            ``"judge"``), or a legacy workflow-mode alias (``"guided"``,
            ``"problem_solving"``) on a session persisted before the rename —
            resolved through ``AgentRegistry.resolve_top_agent`` on every read,
            so both spellings work and an unrecognized one falls back to the
            registry's declared default rather than failing.

            Distinct from :attr:`agent`, which is whichever agent holds the
            floor *right now* and is frequently a sub-agent mid-pipeline; this
            is the top-level one the session is configured to run.
        effective_top_agent: The top-level agent the *current* prompt runs
            under. Frozen at prompt dequeue like ``effective_autonomous``, so a
            switch mid-prompt applies from the next one.
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
            (``prompt.edit_review``, WS_PROTOCOL.md §6.9), independent of and
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
        sampling: Request-level ``llama-server`` sampling overrides, keyed by
            local registry entry ("quant") name — ``{entry_name: {param:
            value}}``, holding only the parameters the user actually set for
            each (doc/SAMPLING.md §9). Per entry rather than one flat set so
            switching models and back restores each quant's own tuning; empty
            for a session that has never opened the sampling modal, which is
            the normal case and means no sampling fields are sent at all.
            Never frozen and never reset by a model switch — unlike
            ``thinking_level``, whose valid values are model-dependent, an
            override here is already scoped to the exact entry it applies to.
            Mirrors ``TransientStore.sampling`` for crash-resume, the same
            relationship ``security_rules`` has to its transient twin.
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
        workspace_connected: Whether this session's bound directories (if any)
            are hosted by the live workspace currently pushed to this window —
            mirrors :meth:`~kodo.runtime._engine._core.WorkflowEngine.
            _is_workspace_connected`, mode-agnostic (unlike the
            Problem-Solver-only ``get_root_paths`` disconnected fallback it
            feeds alongside this field). Always ``True`` for a session that
            has never locked a directory. Recomputed on session start and on
            every ``workspace.folders`` push (``WorkflowEngine.
            handle_workspace_folders``); drives kodo-vsix's reconnect-workspace
            button (doc/SESSIONS.md, doc/WS_PROTOCOL.md §5.1).
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: Phase = "intake"
    agent: str | None = None
    component: str | None = None
    autonomous: bool = False
    effective_autonomous: bool = False
    # Empty until something says otherwise. There is no sensible literal to
    # put here — which agent is the default is the registry's answer, and this
    # dataclass has no registry — so ``WorkflowEngine.start`` fills it for a
    # brand-new session and the resume path fills it from the store.
    top_agent: str = ""
    effective_top_agent: str = ""
    edit_control: str = "smart"
    command_control: str = "smart"
    thinking_level: str = ""
    sampling: dict[str, dict[str, object]] = field(default_factory=dict)
    security_rules: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    security_path_rules: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    awaiting_first_chunk: bool = False
    workspace_connected: bool = True

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for wire-protocol events.

        The two frozen toggles (``autonomous``/``top_agent``) emit both the
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
            "top_agent": self.top_agent,
            "effective_top_agent": self.effective_top_agent,
            "edit_control": self.edit_control,
            "command_control": self.command_control,
            "thinking_level": self.thinking_level,
            "sampling": self.sampling,
            "awaiting_first_chunk": self.awaiting_first_chunk,
            "workspace_connected": self.workspace_connected,
        }
