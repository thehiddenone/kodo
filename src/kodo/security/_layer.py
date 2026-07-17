"""The security layer: judge every tool call — allow it, or ask the user.

A self-sufficient judgement engine over the :mod:`kodo.toolspecs` catalog and
the :mod:`kodo.shellparser` structural parse. It receives one tool call's name
and input plus the session's security facts, and returns a
:class:`SecurityDecision` — ``allow`` (dispatch proceeds) or ``ask`` (the
caller must surface a permission prompt and obey the user's answer). The
layer is fully deterministic and performs no I/O: judgement is heuristic
rules over structure (doc/SECURITY_RULES_PLAN.md), never an LLM.

Three postures, driven by the session's ``command_control`` setting:

- ``permissive`` — threshold only: everything below CRITICAL passes.
- ``defensive``  — threshold only: everything at or above MODERATE asks.
- ``smart``      — the default. Calls below HIGH pass (their effects are
  workspace-confined by construction). HIGH calls are judged individually by
  a per-tool static policy: ``run_command`` goes through the heuristic rule
  engine (:mod:`._rules` over the :mod:`._defaults` table), ``filesystem`` /
  ``rollback`` / ``toolchain_deps`` have direct structural policies, and a
  HIGH tool without a policy asks (fail closed). CRITICAL always asks.

While Autonomous mode is in effect the layer operates as ``permissive`` —
the server-side twin of the client forcing the Command toggle to Permissive
(there is no user to ask).

``disable_autonomous_mode`` is always allowed regardless of posture: its only
effect is returning control to the user — gating it would be self-defeating.

Likewise, any of the six native file tools called with ``temporary: true`` is
always allowed regardless of posture: the call is confined to the session's
private scratch directory (outside every project root, never mirrored into
the checkpoint/rollback system) rather than the workspace, so there is
nothing for the user to review.
"""

from __future__ import annotations

import logging
import os.path
import re
from dataclasses import dataclass

from kodo.toolspecs import ALL_TOOLS, SecurityImpact, ToolSpec

from ._rules import AskPart, RuleDecision, evaluate_command
from ._store import global_rules

__all__ = [
    "MODE_DEFENSIVE",
    "MODE_PERMISSIVE",
    "MODE_SMART",
    "SecurityDecision",
    "SecurityLayer",
]

_log = logging.getLogger(__name__)

MODE_PERMISSIVE = "permissive"
MODE_DEFENSIVE = "defensive"
MODE_SMART = "smart"

_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in ALL_TOOLS}

# Always allowed: its only effect is handing control back to the user.
_ALWAYS_ALLOWED = frozenset({"disable_autonomous_mode"})

# File tools whose `temporary: true` input confines the call to the session's
# private scratch directory (~/.kodo/sessions/<id>/tmp, kodo.project.session_temp_dir)
# instead of the project — never mirrored into the checkpoint/rollback system
# (kodo.runtime._engine._checkpointing skips it outright) and, to match, always
# allowed here regardless of Command Control posture. kodo.tools.Tool.resolve_path
# enforces the actual containment; this layer only reads the flag.
_TEMP_ALLOWED_TOOLS = frozenset(
    {
        "create_file",
        "create_directory",
        "edit_file",
        "filesystem",
        "find_files",
        "find_text_in_files",
    }
)

# A `toolchain_deps` dependency name/version that smells like anything other
# than a plain registry package: URLs, VCS refs, local paths, or option
# injection. Those are the supply-chain-risk shapes worth a user's eyes.
_SUSPICIOUS_DEP_RE = re.compile(r"://|^git\+|^file:|[/\\]|^[.\-]|\s")


@dataclass(frozen=True)
class SecurityDecision:
    """The layer's verdict on one tool call.

    Attributes:
        action: ``"allow"`` (dispatch proceeds) or ``"ask"`` (surface a
            permission prompt; dispatch only on the user's approval).
        reason: One human-readable sentence explaining the verdict — shown in
            the permission prompt when ``action == "ask"``.
        source: What produced the verdict: ``"policy"`` (always-allow /
            unknown tool / per-tool static policy), ``"threshold"``
            (permissive/defensive/smart levels), ``"workspace"`` (static
            outside-workspace finding), ``"static"`` (provably read-only
            fast path), or ``"rules"`` (the heuristic rule engine).
        rule_offer: For a ``run_command`` ask only, the ``(executable,
            subcommand)`` shape the permission prompt should offer to
            permanently allow (session or global) for the *first* asking
            part, or ``None`` when it isn't offer-eligible
            (doc/SECURITY_RULES_PLAN.md §2.2). Mirrors ``parts[0].rule_offer``.
        parts: For a ``run_command`` ask, every elementary command in the
            pipeline that still needs the user's attention, in command
            order, deduplicated by shape — empty for every other tool and
            for any ``"allow"`` (doc/SECURITY_RULES_PLAN.md §2.6). The
            source of truth for the permission prompt's checkboxes.
    """

    action: str
    reason: str
    source: str
    rule_offer: tuple[str, str] | None = None
    parts: tuple[AskPart, ...] = ()


def _allow(reason: str, source: str) -> SecurityDecision:
    return SecurityDecision(action="allow", reason=reason, source=source)


def _ask(
    reason: str,
    source: str,
    rule_offer: tuple[str, str] | None = None,
    parts: tuple[AskPart, ...] = (),
) -> SecurityDecision:
    return SecurityDecision(
        action="ask", reason=reason, source=source, rule_offer=rule_offer, parts=parts
    )


class SecurityLayer:
    """Judges tool calls per the session's Command Control posture.

    One instance serves a whole engine/session; it is stateless and fully
    synchronous in effect (the async surface is kept for the dispatcher's
    calling convention and Phase 2 rule stores).
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
            session_rules: This session's Phase 2 "always allow" rules
                (``(executable, subcommand)`` shapes) — merged with the
                process-wide global store for ``run_command``'s rule engine.
                Ignored by every other tool.

        Returns:
            SecurityDecision: allow or ask, with reason.
        """
        spec = _SPECS_BY_NAME.get(tool_name)
        if spec is None:
            # Unknown to the catalog — the dispatcher rejects it anyway.
            return _allow("Unknown tool; dispatcher will reject it.", "policy")
        if tool_name in _ALWAYS_ALLOWED:
            return _allow("Returns control to the user; never gated.", "policy")
        if tool_name in _TEMP_ALLOWED_TOOLS and bool(tool_input.get("temporary")):
            return _allow(
                "Session-scoped temporary location; never mirrored, always allowed.",
                "policy",
            )

        impact = spec.security_impact
        mode = (
            command_control
            if command_control in (MODE_PERMISSIVE, MODE_DEFENSIVE, MODE_SMART)
            else MODE_SMART
        )
        if autonomous:
            mode = MODE_PERMISSIVE

        decision = self.__evaluate_mode(
            mode, spec, impact, tool_input, default_cwd, roots, session_rules
        )
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

    def __evaluate_mode(
        self,
        mode: str,
        spec: ToolSpec,
        impact: SecurityImpact,
        tool_input: dict[str, object],
        default_cwd: str,
        roots: tuple[str, ...],
        session_rules: frozenset[tuple[str, str]],
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
            return self.__evaluate_run_command(tool_input, default_cwd, roots, session_rules)
        if spec.name == "filesystem":
            return self.__evaluate_filesystem(tool_input)
        if spec.name == "rollback":
            return _allow(
                "Workspace-confined checkpoint rollback; the Guide confirms it "
                "with the user via ask_user first.",
                "policy",
            )
        if spec.name == "toolchain_deps":
            return self.__evaluate_toolchain_deps(tool_input)
        # A HIGH tool without a static policy: fail closed to the user.
        return _ask(
            f"{spec.external_name} is High-impact and has no static security policy.",
            "policy",
        )

    def __evaluate_run_command(
        self,
        tool_input: dict[str, object],
        default_cwd: str,
        roots: tuple[str, ...],
        session_rules: frozenset[tuple[str, str]],
    ) -> SecurityDecision:
        """The heuristic rule engine's verdict (doc/SECURITY_RULES_PLAN.md §1),
        including any Phase 2 "always allow" rule the user already granted
        (session-scoped, merged with the process-wide global store)."""
        command = str(tool_input.get("command", ""))
        cwd = self.__effective_cwd(tool_input, default_cwd)
        known_rules = session_rules | global_rules()
        verdict: RuleDecision = evaluate_command(
            command, cwd=cwd, roots=roots, known_rules=known_rules
        )
        if verdict.action == "allow":
            return _allow(verdict.reason, verdict.source)
        return _ask(
            verdict.reason, verdict.source, rule_offer=verdict.rule_offer, parts=verdict.parts
        )

    @staticmethod
    def __evaluate_filesystem(tool_input: dict[str, object]) -> SecurityDecision:
        """Every path is resolver-confined to the workspace at dispatch and
        workspace changes are checkpointed — only the recursive directory
        delete is costly enough to warrant a user's eyes."""
        operation = str(tool_input.get("operation", ""))
        if operation == "delete_dir":
            path = str(tool_input.get("path", ""))
            return _ask(
                f"Recursively deletes the directory '{path}' and everything in it.",
                "policy",
            )
        if operation in ("delete_file", "copy_file", "copy_dir", "move_file", "move_dir"):
            return _allow(
                f"Workspace-confined {operation.replace('_', ' ')}; checkpointed.",
                "policy",
            )
        return _ask(f"Unrecognized filesystem operation '{operation}'.", "policy")

    @staticmethod
    def __evaluate_toolchain_deps(tool_input: dict[str, object]) -> SecurityDecision:
        """A plain registry name+constraint is ordinary dependency work; URL /
        VCS / path / flag-shaped names are the supply-chain shapes that ask."""
        name = str(tool_input.get("name", ""))
        version = str(tool_input.get("version", "") or "")
        if not name or _SUSPICIOUS_DEP_RE.search(name):
            return _ask(
                f"The dependency name '{name}' is not a plain registry package name.",
                "policy",
            )
        if "://" in version or version.startswith("git+"):
            return _ask(
                f"The version constraint '{version}' points at an external source.",
                "policy",
            )
        return _allow(
            "Ordinary registry dependency change; the sub-agent's own commands "
            "are gated individually.",
            "policy",
        )

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
