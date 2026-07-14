"""The heuristic command rule engine: allow / ask without an LLM.

Judges one ``run_command`` shell command deterministically, from structure
alone (doc/SECURITY_RULES_PLAN.md). The verdict ladder, first hit wins:

1. **Workspace escape** — any argument/redirection resolving outside every
   workspace root *and* outside the OS temp directory asks (unchanged from
   the static analysis era; the temp-directory carve-out is new — see
   :mod:`kodo.common._tempdir`).
2. **Command substitutions** — ``$(...)`` / backticks *execute* their content,
   so each one is recursively evaluated; a dangerous inner command asks.
3. **Read-only fast path** — every executable on the conservative read-only
   list and no file-writing redirection allows. Value expansions (``$VAR``)
   are tolerated here: an unknown value fed to a reader cannot mutate.
4. **Per-segment rules** — every pipeline segment must individually clear:
   structural red flags (bare shell as a pipe target, inline/encoded code,
   ``xargs`` feeding unknown args to a mutator), then the ordered
   :class:`CommandRule` table (ask-rules and allow-rules interleaved,
   specific before general — see :mod:`._defaults`). Nested shell commands
   (``sh -c "…"``) are evaluated recursively.
5. **Default: ask.** A command not in the known-safe set produces the same
   deterministic, explainable ask every time.

An allow-rule match is voided by an embedded substitution: a state-changing
command whose arguments cannot be statically resolved is referred to the user
no matter how benign its executable.

``category`` and ``rule_eligible`` flow into the decision for the permission
prompt and the Phase 2 "always allow" affordance; ``shape`` is the
generalized ``(executable, subcommand)`` a future user rule would store.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from ._analysis import _READONLY_EXECUTABLES, analyze_command
from ._classify import SHELL_EXECUTABLES, NormalizedSegment

__all__ = ["CommandRule", "RuleDecision", "evaluate_command"]

# Recursive evaluation (command substitutions, `sh -c` nesting) gives up —
# and asks — beyond this depth; legitimate dev commands never nest this far.
_MAX_DEPTH = 3

# PowerShell cmdlets that only read; the POSIX equivalents live in
# ``_analysis._READONLY_EXECUTABLES`` (which segment-level checks also use).
_READONLY_CMDLETS = frozenset(
    {
        "get-childitem",
        "get-content",
        "get-location",
        "get-item",
        "get-itemproperty",
        "get-date",
        "get-command",
        "get-help",
        "get-process",
        "get-service",
        "select-string",
        "test-path",
        "resolve-path",
        "measure-object",
        "write-output",
        "tasklist",
        "findstr",
        "more",
    }
)


@dataclass(frozen=True)
class _DualModeCommand:
    """A command that is benign when read-only and dangerous when mutating.

    Neither ``_analysis._READONLY_EXECUTABLES`` (blanket, unconditional) nor
    ``CommandRule.flags_any`` (can't express "a positional value follows" or
    "an argument contains ``=``") can represent this — so these are matched
    by executable name directly in :func:`_judge_segment`, the same way the
    ``xargs`` structural check is, rather than through the rule table.
    """

    mutates: Callable[[NormalizedSegment], bool]
    reason: str


def _sysctl_mutates(segment: NormalizedSegment) -> bool:
    """``-w``/``--write``/``-p``/``--load``/``--system``, or the Linux
    flag-less assignment form (``sysctl vm.swappiness=10``)."""
    if any(_flag_hit(f, segment.flags) for f in ("-w", "--write", "-p", "--load", "--system")):
        return True
    return any("=" in a for a in segment.args)


def _ulimit_mutates(segment: NormalizedSegment) -> bool:
    """A numeric or ``unlimited`` positional sets a limit; a bare query
    (``ulimit``, ``ulimit -a``, ``ulimit -n``) only reads."""
    return any(a.lower() == "unlimited" or a.lstrip("-").isdigit() for a in segment.args)


def _date_mutates(segment: NormalizedSegment) -> bool:
    """``-s``/``--set``, or a bare positional (BSD/macOS/cmd.exe direct-set
    form, e.g. ``date 010112002026``). A ``+FORMAT`` string stays read-only."""
    if any(_flag_hit(f, segment.flags) for f in ("-s", "--set")):
        return True
    return any(a and not a.startswith("+") for a in segment.args)


def _hostname_mutates(segment: NormalizedSegment) -> bool:
    """``-F``/``--file``, or any positional (``hostname NAME`` sets it; a
    bare ``hostname`` reads)."""
    if any(_flag_hit(f, segment.flags) for f in ("-f", "--file")):
        return True
    return bool(segment.args)


# Dialect-agnostic by design: verified against the cmd.exe/PowerShell forms
# too (`date /t` is a single-slash flag, filtered out before `args` is built,
# so it reads; Windows `hostname` has no legitimate positional at all, so the
# predicate is at worst an unnecessary — never unsafe — ask there).
_DUAL_MODE: dict[str, _DualModeCommand] = {
    "sysctl": _DualModeCommand(
        _sysctl_mutates, "'sysctl -w'/assignment form changes a running kernel parameter."
    ),
    "ulimit": _DualModeCommand(
        _ulimit_mutates, "'ulimit' with a value changes a resource limit for this shell."
    ),
    "date": _DualModeCommand(
        _date_mutates, "'date' with a value or -s/--set changes the system clock."
    ),
    "hostname": _DualModeCommand(
        _hostname_mutates, "'hostname' with a value changes the system hostname."
    ),
}


@dataclass(frozen=True)
class CommandRule:
    """One entry of the ordered rule table.

    Attributes:
        executable: Canonical executable name(s) to match (post-alias,
            lowercase leaf name).
        subcommand: Required first positional (lowercase), or ``None`` to
            match any.
        flags_any: When non-empty, at least one of these flags must be
            present (``-r`` also matches inside a short-flag cluster like
            ``-rf``; ``--long`` also matches ``--long=value``).
        verdict: ``"allow"`` or ``"ask"``.
        category: Danger taxonomy for ask-rules (``deployment`` /
            ``destructive`` / ``system`` / ``network`` / ``privilege`` /
            ``obfuscation`` / ``unknown``), or ``benign-dev`` for allow-rules.
        reason: One user-facing sentence (ask-rules; shown in the prompt).
        rule_eligible: Whether a Phase 2 user rule may generalize/override
            this finding ("always allow `git push`"). Never set on
            destructive / privilege / obfuscation rules.
    """

    executable: str | tuple[str, ...]
    subcommand: str | None = None
    flags_any: tuple[str, ...] = ()
    verdict: str = "ask"
    category: str = "unknown"
    reason: str = ""
    rule_eligible: bool = False


@dataclass(frozen=True)
class RuleDecision:
    """The engine's verdict on one command.

    Attributes:
        action: ``"allow"`` or ``"ask"``.
        reason: One user-facing sentence.
        category: The matched danger category (``benign-dev`` for allows).
        source: ``"workspace"`` (escape finding), ``"static"`` (read-only
            fast path), or ``"rules"`` (the rule ladder).
        shape: The generalized ``(executable, subcommand)`` of the deciding
            segment — the Phase 2 user-rule shape — or ``None``.
        rule_eligible: Whether Phase 2 may offer "always allow" for this ask.
    """

    action: str
    reason: str
    category: str
    source: str
    shape: tuple[str, str] | None = None
    rule_eligible: bool = False


def _ask(
    reason: str,
    category: str,
    source: str = "rules",
    shape: tuple[str, str] | None = None,
    eligible: bool = False,
) -> RuleDecision:
    return RuleDecision(
        action="ask",
        reason=reason,
        category=category,
        source=source,
        shape=shape,
        rule_eligible=eligible,
    )


def evaluate_command(
    command: str,
    *,
    cwd: str,
    roots: tuple[str, ...],
    windows: bool | None = None,
    rules: tuple[CommandRule, ...] | None = None,
    _depth: int = 0,
) -> RuleDecision:
    """Judge *command* against the workspace and the rule table.

    Args:
        command: The raw shell command line.
        cwd: The directory the command runs in (absolute, workspace-confined).
        roots: Absolute workspace root paths.
        windows: Parse/match as PowerShell-cmd (``True``) or POSIX
            (``False``); defaults to the current platform.
        rules: Rule table override; defaults to the built-in table for the
            dialect (:mod:`._defaults`).

    Returns:
        RuleDecision: allow or ask, with reason, category, and the Phase 2
        shape/eligibility facts.
    """
    win = os.name == "nt" if windows is None else windows
    if rules is None:
        from ._defaults import default_rules

        rules = default_rules(win)

    if _depth > _MAX_DEPTH:
        return _ask("The command nests other commands too deeply to analyze.", "obfuscation")

    analysis = analyze_command(command, cwd=cwd, roots=roots, windows=win)

    if analysis.outside_paths:
        listed = ", ".join(analysis.outside_paths[:5])
        return _ask(
            f"The command targets paths outside the workspace: {listed}.",
            "workspace",
            source="workspace",
        )

    # `$(...)` / backticks execute their content: judge each nested command.
    for snippet in analysis.command_subs:
        inner = _strip_command_sub(snippet)
        if not inner:
            return _ask(f"Unparseable command substitution: {snippet}", "obfuscation")
        verdict = evaluate_command(
            inner, cwd=cwd, roots=roots, windows=win, rules=rules, _depth=_depth + 1
        )
        if verdict.action != "allow":
            return _ask(
                f"Embedded command substitution `{snippet}`: {verdict.reason}",
                verdict.category,
            )

    if analysis.read_only:
        return RuleDecision(
            action="allow",
            reason="Read-only command with all targets inside the workspace.",
            category="benign-dev",
            source="static",
        )

    for segment in analysis.segments:
        if not segment.executable:
            continue
        verdict = _judge_segment(
            segment, rules=rules, cwd=cwd, roots=roots, windows=win, depth=_depth
        )
        if verdict.action != "allow":
            return verdict

    return RuleDecision(
        action="allow",
        reason="Every command in the pipeline is a known-safe development operation.",
        category="benign-dev",
        source="rules",
    )


def _judge_segment(
    segment: NormalizedSegment,
    *,
    rules: tuple[CommandRule, ...],
    cwd: str,
    roots: tuple[str, ...],
    windows: bool,
    depth: int,
) -> RuleDecision:
    """One pipeline segment through red flags, the rule table, the default."""
    exe = segment.executable
    shape = (exe, segment.subcommand)
    display = f"{exe} {segment.subcommand}".strip()
    ok = RuleDecision(action="allow", reason="", category="benign-dev", source="rules")

    if exe in SHELL_EXECUTABLES:
        if segment.nested_opaque:
            return _ask(
                f"'{display}' carries an inline or encoded command that cannot be "
                "statically analyzed.",
                "obfuscation",
            )
        if segment.nested_command is not None:
            verdict = evaluate_command(
                segment.nested_command,
                cwd=cwd,
                roots=roots,
                windows=windows,
                rules=rules,
                _depth=depth + 1,
            )
            if verdict.action != "allow":
                return _ask(f"Nested shell command: {verdict.reason}", verdict.category)
            return ok
        if segment.piped_input:
            return _ask(
                "The command pipes data into a shell for execution.",
                "obfuscation",
            )
        if segment.args:
            return ok  # `sh build.sh`: runs a workspace script, like `python x.py`.
        return _ask(f"'{exe}' starts a bare interactive shell.", "unknown", shape=shape)

    if segment.nested_opaque:
        return _ask(
            f"'{exe}' is given inline code that cannot be statically analyzed.",
            "obfuscation",
        )

    if exe == "xargs":
        child = segment.subcommand
        if child and child in (_READONLY_EXECUTABLES | _READONLY_CMDLETS):
            return ok
        return _ask(
            f"'xargs' feeds arguments that cannot be statically resolved to "
            f"'{child or 'a command'}'.",
            "unknown",
        )

    dual = _DUAL_MODE.get(exe)
    if dual is not None:
        if segment.has_substitution:
            # Unlike a pure reader, an unresolvable value could be the
            # mutating form — no leniency here.
            return _ask(
                f"'{display}' contains substitutions whose values cannot be statically resolved.",
                "unknown",
            )
        if dual.mutates(segment):
            return _ask(dual.reason, "system", shape=shape)
        return ok

    # Read-only executables tolerate value expansions: an unknown value fed
    # to a pure reader cannot mutate anything (writing redirections still
    # disqualify — the target file is a mutation).
    readonly = _READONLY_EXECUTABLES | _READONLY_CMDLETS if windows else _READONLY_EXECUTABLES
    if exe in readonly and not segment.writes_file:
        return ok

    for rule in rules:
        if not _matches(rule, segment):
            continue
        if rule.verdict == "allow":
            if segment.has_substitution:
                return _ask(
                    f"'{display}' contains substitutions whose values cannot be "
                    "statically resolved.",
                    "unknown",
                )
            return ok
        return _ask(rule.reason, rule.category, shape=shape, eligible=rule.rule_eligible)

    return _ask(
        f"'{display}' is not in the known-safe command set.",
        "unknown",
        shape=shape,
        eligible=True,
    )


def _matches(rule: CommandRule, segment: NormalizedSegment) -> bool:
    execs = rule.executable if isinstance(rule.executable, tuple) else (rule.executable,)
    if segment.executable not in execs:
        return False
    if rule.subcommand is not None and segment.subcommand != rule.subcommand:
        return False
    return not rule.flags_any or any(_flag_hit(f, segment.flags) for f in rule.flags_any)


def _flag_hit(wanted: str, flags: tuple[str, ...]) -> bool:
    """Whether *wanted* appears in *flags* — exact, ``=value``-attached, or
    (for a single-letter flag) inside a short-flag cluster (``-r`` in ``-rf``)."""
    for token in flags:
        base = token.split("=", 1)[0]
        if base == wanted:
            return True
        if (
            re.fullmatch(r"-[a-z0-9]", wanted)
            and re.fullmatch(r"-[a-z0-9]+", base)
            and wanted[1] in base[1:]
        ):
            return True
    return False


def _strip_command_sub(snippet: str) -> str:
    """``$(inner)`` / ```inner``` → ``inner`` (best-effort, may be empty)."""
    if snippet.startswith("$("):
        return snippet[2:].rstrip(")").strip()
    return snippet.strip("`").strip()
