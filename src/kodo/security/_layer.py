"""The security layer: judge every tool call — allow it, or ask the user.

A self-sufficient judgement engine over the :mod:`kodo.toolspecs` catalog and
the :mod:`kodo.shellparser` structural parse. It receives one tool call's name
and input plus the session's security facts, and returns a
:class:`SecurityDecision` — ``allow`` (dispatch proceeds) or ``ask`` (the
caller must surface a permission prompt and obey the user's answer). The
layer itself never performs I/O beyond the injected judge callable; wiring
the decision to the actual gate/UI is the dispatcher's job.

Three postures, driven by the session's ``command_control`` setting:

- ``permissive`` — threshold only: everything below CRITICAL passes.
- ``defensive``  — threshold only: everything at or above MODERATE asks.
- ``smart``      — the default. Calls below HIGH pass (their effects are
  workspace-confined by construction). HIGH calls are judged individually:
  ``run_command`` is first statically analyzed — any target provably outside
  the workspace asks immediately, a provably read-only in-workspace command
  passes immediately — and everything not settled statically goes to the LLM
  intent judge (:mod:`._judge`), which matches the declared ``intent``
  against the parameters and answers allow-or-ask. CRITICAL always asks.

While Autonomous mode is in effect the layer operates as ``permissive`` —
the server-side twin of the client forcing the Command toggle to Permissive
(there is no user to ask).

``disable_autonomous_mode`` is always allowed regardless of posture: its only
effect is returning control to the user — gating it would be self-defeating.
"""

from __future__ import annotations

import logging
import os.path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kodo.toolspecs import ALL_TOOLS, INTENT_KEY, SecurityImpact, ToolSpec

from ._analysis import analyze_command
from ._judge import build_judge_messages, parse_judge_verdict

__all__ = [
    "MODE_DEFENSIVE",
    "MODE_PERMISSIVE",
    "MODE_SMART",
    "JudgeCallable",
    "SecurityDecision",
    "SecurityLayer",
]

_log = logging.getLogger(__name__)

MODE_PERMISSIVE = "permissive"
MODE_DEFENSIVE = "defensive"
MODE_SMART = "smart"

# An async (system_prompt, user_message) -> raw model text callable. Injected
# by the runtime; this package never imports an LLM client.
JudgeCallable = Callable[[str, str], Awaitable[str]]

_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in ALL_TOOLS}

# Always allowed: its only effect is handing control back to the user.
_ALWAYS_ALLOWED = frozenset({"disable_autonomous_mode"})


@dataclass(frozen=True)
class SecurityDecision:
    """The layer's verdict on one tool call.

    Attributes:
        action: ``"allow"`` (dispatch proceeds) or ``"ask"`` (surface a
            permission prompt; dispatch only on the user's approval).
        reason: One human-readable sentence explaining the verdict — shown in
            the permission prompt when ``action == "ask"``.
        source: What produced the verdict: ``"policy"`` (always-allow /
            unknown tool), ``"threshold"`` (permissive/defensive/smart
            levels), ``"workspace"`` (static outside-workspace finding),
            ``"static"`` (provably read-only fast path), or ``"judge"``
            (LLM intent match, including its failure modes).
    """

    action: str
    reason: str
    source: str


def _allow(reason: str, source: str) -> SecurityDecision:
    return SecurityDecision(action="allow", reason=reason, source=source)


def _ask(reason: str, source: str) -> SecurityDecision:
    return SecurityDecision(action="ask", reason=reason, source=source)


class SecurityLayer:
    """Judges tool calls per the session's Command Control posture.

    One instance serves a whole engine/session; it is stateless between calls
    apart from the injected judge callable.

    Args:
        judge: Async ``(system, user) -> raw text`` LLM callable for SMART
            mode's intent matching, or ``None`` — in which case every call
            that would need the judge asks the user instead (fail closed).
    """

    def __init__(self, judge: JudgeCallable | None = None) -> None:
        self.__judge = judge

    async def evaluate(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        command_control: str,
        autonomous: bool,
        default_cwd: str,
        roots: tuple[str, ...],
    ) -> SecurityDecision:
        """Judge one tool call.

        Args:
            tool_name: The tool being called.
            tool_input: The call's parsed input parameters.
            command_control: The session's posture (``permissive`` /
                ``defensive`` / ``smart``; unknown values fall back to smart).
            autonomous: The prompt's frozen ``effective_autonomous`` — when
                ``True`` the layer operates as permissive (no user to ask).
            default_cwd: The run's default working directory (absolute,
                workspace-confined) — the base for ``run_command`` analysis.
            roots: Absolute workspace root paths.

        Returns:
            SecurityDecision: allow or ask, with reason.
        """
        spec = _SPECS_BY_NAME.get(tool_name)
        if spec is None:
            # Unknown to the catalog — the dispatcher rejects it anyway.
            return _allow("Unknown tool; dispatcher will reject it.", "policy")
        if tool_name in _ALWAYS_ALLOWED:
            return _allow("Returns control to the user; never gated.", "policy")

        impact = spec.security_impact
        mode = (
            command_control
            if command_control in (MODE_PERMISSIVE, MODE_DEFENSIVE, MODE_SMART)
            else MODE_SMART
        )
        if autonomous:
            mode = MODE_PERMISSIVE

        decision = await self.__evaluate_mode(mode, spec, impact, tool_input, default_cwd, roots)
        _log.info(
            "security: %s %s (%s, mode=%s, source=%s): %s",
            decision.action.upper(),
            tool_name,
            impact.label,
            mode,
            decision.source,
            decision.reason,
        )
        return decision

    async def __evaluate_mode(
        self,
        mode: str,
        spec: ToolSpec,
        impact: SecurityImpact,
        tool_input: dict[str, object],
        default_cwd: str,
        roots: tuple[str, ...],
    ) -> SecurityDecision:
        if mode == MODE_PERMISSIVE:
            if impact >= SecurityImpact.CRITICAL:
                return _ask(f"{spec.external_name} is {impact.label}-impact.", "threshold")
            return _allow(f"Permissive: {impact.label} impact passes.", "threshold")

        if mode == MODE_DEFENSIVE:
            if impact >= SecurityImpact.MODERATE:
                return _ask(
                    f"Defensive: {spec.external_name} is {impact.label}-impact "
                    f"(Moderate or above always asks).",
                    "threshold",
                )
            return _allow(f"Defensive: {impact.label} impact passes.", "threshold")

        # SMART.
        if impact >= SecurityImpact.CRITICAL:
            return _ask(f"{spec.external_name} is {impact.label}-impact.", "threshold")
        if impact < SecurityImpact.HIGH:
            return _allow(
                f"Smart: {impact.label} impact, workspace-confined by construction.",
                "threshold",
            )
        if spec.name == "run_command":
            return await self.__evaluate_run_command(spec, tool_input, default_cwd, roots)
        return await self.__run_judge(spec, tool_input, roots, notes=())

    async def __evaluate_run_command(
        self,
        spec: ToolSpec,
        tool_input: dict[str, object],
        default_cwd: str,
        roots: tuple[str, ...],
    ) -> SecurityDecision:
        """Static workspace analysis first; the intent judge for the rest."""
        command = str(tool_input.get("command", ""))
        cwd = self.__effective_cwd(tool_input, default_cwd)
        analysis = analyze_command(command, cwd=cwd, roots=roots)

        if analysis.outside_paths:
            listed = ", ".join(analysis.outside_paths[:5])
            return _ask(f"The command targets paths outside the workspace: {listed}.", "workspace")
        if analysis.read_only and not analysis.unresolved:
            return _allow("Read-only command with all targets inside the workspace.", "static")

        notes: tuple[str, ...] = ()
        if analysis.unresolved:
            listed = ", ".join(analysis.unresolved[:5])
            notes = (
                f"Contains substitutions whose targets could not be statically resolved: {listed}",
            )
        return await self.__run_judge(spec, tool_input, roots, notes=notes)

    async def __run_judge(
        self,
        spec: ToolSpec,
        tool_input: dict[str, object],
        roots: tuple[str, ...],
        *,
        notes: tuple[str, ...],
    ) -> SecurityDecision:
        """One LLM intent-match round; every failure mode asks (fail closed)."""
        if self.__judge is None:
            return _ask("No security judge is available for this High-impact call.", "judge")

        intent = str(tool_input.get(INTENT_KEY, ""))
        params = {k: v for k, v in tool_input.items() if k != INTENT_KEY}
        system, user = build_judge_messages(
            tool_name=spec.name,
            external_name=spec.external_name,
            intent=intent,
            params=params,
            roots=roots,
            notes=notes,
        )
        try:
            raw = await self.__judge(system, user)
        except Exception:
            _log.exception("security judge call failed; asking the user")
            return _ask("The security judge could not be reached.", "judge")

        verdict = parse_judge_verdict(raw)
        if verdict.allow:
            return _allow(verdict.reason, "judge")
        return _ask(verdict.reason, "judge")

    @staticmethod
    def __effective_cwd(tool_input: dict[str, object], default_cwd: str) -> str:
        """Lexically resolve ``working_dir`` against the default cwd.

        Best-effort mirror of the run_command handler's resolution — the
        resolver rejects escapes at dispatch time, so a lexical join is enough
        for analysis purposes.
        """
        raw = tool_input.get("working_dir")
        if not isinstance(raw, str) or not raw.strip():
            return default_cwd
        if os.path.isabs(raw):
            return os.path.normpath(raw)
        return os.path.normpath(os.path.join(default_cwd, raw))
