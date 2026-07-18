"""The heuristic command rule engine: allow / ask without an LLM.

Judges one ``run_command`` shell command deterministically, from structure
alone (doc/SECURITY_RULES_PLAN.md). The verdict ladder, first hit wins:

1. **Workspace escape** — any argument/redirection resolving outside every
   workspace root *and* outside the OS temp directory asks (the temp-directory
   carve-out — see :mod:`kodo.common._tempdir`). Judged **per segment**, not
   per line (doc/SECURITY_RULES_PLAN.md §2.7): a segment whose executable is
   in the read-only/``cd`` set and isn't otherwise disqualified gets a
   permanent "always allow" offer keyed on the *resolved absolute path* —
   matched by resolving a future call's own argument the same way before
   comparing, not by literal string equality. Everything else (unknown/
   destructive executables, a sensitive-looking path) still asks with no
   offer, exactly as before.
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

A command with more than one asking segment (a pipeline/``&&``/``;`` chain)
does **not** collapse to one undifferentiated ask: every segment is judged
independently, and every one that still needs the user's attention —
deduplicated by shape — becomes one :class:`AskPart` in ``RuleDecision.parts``
(doc/SECURITY_RULES_PLAN.md §2.6), each with its own offer eligibility.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from ._analysis import (
    _READONLY_CMDLETS,
    _READONLY_EXECUTABLES,
    _within_any_root,
    analyze_command,
)
from ._classify import SHELL_EXECUTABLES, NormalizedSegment

__all__ = ["AskPart", "CommandRule", "RuleDecision", "evaluate_command"]

# Recursive evaluation (command substitutions, `sh -c` nesting) gives up —
# and asks — beyond this depth; legitimate dev commands never nest this far.
_MAX_DEPTH = 3


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
class AskPart:
    """One elementary command within a ``run_command`` line that still needs
    the user's attention (doc/SECURITY_RULES_PLAN.md §2.6).

    A simple, single-segment command produces exactly one part. A pipeline
    joined by ``|``/``||``/``&&``/``;`` may produce one part per distinct
    elementary command that isn't already silenced by an existing rule or an
    unconditional built-in allow — deduplicated by shape when the same
    elementary command repeats in the chain.

    Attributes:
        reason: One user-facing sentence explaining why this part asks.
        rule_offer: The ``(executable, subcommand)`` shape this part may be
            permanently allowed as, or ``None`` when not offer-eligible. For
            a ``kind="path"`` part, ``subcommand`` actually holds a resolved
            absolute filesystem path, not a command subcommand (§2.7) — the
            wire protocol carries it in the same ``{executable, subcommand}``
            shape either way; only ``kind`` (Python-internal, never
            serialized — see ``runtime._gates.fire_permission``) tells
            ``kodo.tools._dispatch`` which store to grant it into.
        kind: ``"command"`` (the ordinary per-segment danger-category ask,
            granted via ``add_security_rule``) or ``"path"`` (a workspace-
            escape ask for an eligible read-only/``cd`` command, granted via
            ``add_security_path_rule`` — doc/SECURITY_RULES_PLAN.md §2.7).
            Never sent over the wire; purely routes the grant call.
    """

    reason: str
    rule_offer: tuple[str, str] | None = None
    kind: str = "command"


@dataclass(frozen=True)
class RuleDecision:
    """The engine's verdict on one command.

    Attributes:
        action: ``"allow"`` or ``"ask"``.
        reason: One user-facing sentence — identical to ``parts[0].reason``
            when there is exactly one part; a ``"; "``-joined summary of
            every part's reason when there is more than one.
        category: The matched danger category (``benign-dev`` for allows) of
            the *first* asking part.
        source: ``"workspace"`` (escape finding), ``"static"`` (read-only
            fast path), or ``"rules"`` (the rule ladder), for the first
            asking part.
        shape: The generalized ``(executable, subcommand)`` of the first
            asking segment — the Phase 2 user-rule shape — or ``None``.
        rule_eligible: Whether an existing user rule matching ``shape`` may
            silently turn the first asking part into an allow, and whether a
            *new* one may be offered (subject to the additional shape checks
            in :func:`_rule_offer` — category eligibility is necessary but
            not sufficient for the offer itself; see ``rule_offer`` below).
        rule_offer: The ``(executable, subcommand)`` shape the permission
            prompt should offer to permanently allow for the first asking
            part, or ``None`` when it isn't offer-eligible
            (doc/SECURITY_RULES_PLAN.md §2.2) — set only on an ``"ask"`` that
            survived the known-rules check (an ask already silenced by an
            existing rule becomes an ``"allow"`` and never reaches this
            field). Mirrors ``parts[0].rule_offer``.
        known_command: Whether the first asking part matched an explicit,
            named ``CommandRule`` in the built-in table (``git push``, ``apt
            install``, …) rather than falling through to the generic
            "not in the known-safe command set" default. Feeds
            :func:`_rule_offer`'s path-argument check (§2.2 rule 3): a known
            command's danger is already bounded by its category, so its
            offer ignores path-like *arguments* entirely (the shape never
            stores them anyway); an unknown command's offer is only exempted
            for a path-like *subcommand* itself, since that's the one case
            where the literal shape still pins the rule to this exact
            invocation.
        parts: Every elementary command in the pipeline that still needs the
            user's attention, in command order, deduplicated by shape —
            empty when ``action == "allow"``. The single source of truth for
            the permission prompt's checkboxes (§2.6); the singular fields
            above exist only so a zero-or-one-part decision (the overwhelming
            majority of commands) reads exactly as it always has.
    """

    action: str
    reason: str
    category: str
    source: str
    shape: tuple[str, str] | None = None
    rule_eligible: bool = False
    rule_offer: tuple[str, str] | None = None
    known_command: bool = False
    parts: tuple[AskPart, ...] = ()


def _ask(
    reason: str,
    category: str,
    source: str = "rules",
    shape: tuple[str, str] | None = None,
    eligible: bool = False,
    known: bool = False,
    rule_offer: tuple[str, str] | None = None,
    kind: str = "command",
) -> RuleDecision:
    return RuleDecision(
        action="ask",
        reason=reason,
        category=category,
        source=source,
        shape=shape,
        rule_eligible=eligible,
        rule_offer=rule_offer,
        known_command=known,
        parts=(AskPart(reason=reason, rule_offer=rule_offer, kind=kind),),
    )


def evaluate_command(
    command: str,
    *,
    cwd: str,
    roots: tuple[str, ...],
    windows: bool | None = None,
    rules: tuple[CommandRule, ...] | None = None,
    known_rules: frozenset[tuple[str, str]] = frozenset(),
    known_path_rules: frozenset[tuple[str, str]] = frozenset(),
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
        known_rules: The caller's already-granted Phase 2 user rules —
            session-scoped and global, merged — as ``(executable,
            subcommand)`` shapes. Consulted for every ask this evaluation
            would otherwise produce whose ``rule_eligible`` is ``True``
            (doc/SECURITY_RULES_PLAN.md §2.4); threaded into every recursive
            call (command substitutions, nested shells) so a rule silences a
            wrapped occurrence exactly like a bare one.
        known_path_rules: The caller's already-granted workspace-escape path
            rules — session-scoped and global, merged — as ``(executable,
            resolved_absolute_path)`` shapes (doc/SECURITY_RULES_PLAN.md
            §2.7). Unlike ``known_rules`` this is matched against a value
            ``analyze_command`` already resolved to absolute form, so
            membership is a literal-tuple check here too — the "resolve a
            relative argument first" step happens once, upstream, in
            ``_analysis.analyze_command``, not on every lookup. Threaded into
            every recursive call the same way ``known_rules`` is.

    Returns:
        RuleDecision: allow or ask, with reason, category, and the Phase 2
        shape/eligibility/offer facts.
    """
    win = os.name == "nt" if windows is None else windows
    if rules is None:
        from ._defaults import default_rules

        rules = default_rules(win)

    if _depth > _MAX_DEPTH:
        return _ask("The command nests other commands too deeply to analyze.", "obfuscation")

    analysis = analyze_command(command, cwd=cwd, roots=roots, windows=win)

    # `$(...)` / backticks execute their content: judge each nested command.
    # This still short-circuits the WHOLE evaluation on a bad nested
    # command — a failing substitution means the whole line is suspect, not
    # just one part of it — unlike the per-segment workspace-escape handling
    # below (§2.7).
    for snippet in analysis.command_subs:
        inner = _strip_command_sub(snippet)
        if not inner:
            return _ask(f"Unparseable command substitution: {snippet}", "obfuscation")
        verdict = evaluate_command(
            inner,
            cwd=cwd,
            roots=roots,
            windows=win,
            rules=rules,
            known_rules=known_rules,
            known_path_rules=known_path_rules,
            _depth=_depth + 1,
        )
        if verdict.action != "allow":
            return _ask(
                f"Embedded command substitution `{snippet}`: {verdict.reason}",
                verdict.category,
            )

    # The read-only fast path must ALSO require no segment has an outside-
    # workspace finding — `_is_read_only` only knows about executables/
    # writes, nothing about paths, so `cat /etc/hosts` (a lone, otherwise-
    # readonly segment) would silently auto-allow here if this weren't
    # ANDed in. `cat file.txt` (a plain in-workspace relative) is unaffected:
    # `_resolve` never even resolves it, so `segment_outside_paths` is empty
    # tuples all the way down and `any(...)` is `False`.
    if analysis.read_only and not any(analysis.segment_outside_paths):
        return RuleDecision(
            action="allow",
            reason="Read-only command with all targets inside the workspace.",
            category="benign-dev",
            source="static",
        )

    asks: list[RuleDecision] = []
    seen_shapes: set[tuple[str, str]] = set()
    seen_paths: set[tuple[str, str]] = set()
    sensitive_roots = _sensitive_roots()
    for i, segment in enumerate(analysis.segments):
        if not segment.executable:
            continue

        outside_here = analysis.segment_outside_paths[i]
        if outside_here:
            # A segment with ANY outside-workspace finding is judged solely
            # on that — it unconditionally skips `_judge_segment` below,
            # regardless of how many of its paths turn out to already be
            # covered by `known_path_rules`. Falling through instead would
            # let e.g. `cd /outside/path` (once granted) hit the
            # unconditional built-in `cd` allow-rule for an UNGRANTED path,
            # or produce a spurious redundant ask on a segment like
            # `cat /etc/hosts > /etc/hosts2` once only the read side is
            # granted.
            eligible = _is_path_offer_eligible(segment)
            for path in dict.fromkeys(outside_here):  # already deduped per-segment; defensive
                # `match_path` (case/slash-folded on Windows) is what gets
                # offered, deduped, and checked against `known_path_rules` —
                # `path` (original resolved casing) is only for the
                # human-facing reason text, so a granted rule reliably
                # silences a differently-cased future call.
                match_path = _normalize_path_key(path, windows=win)
                key = (segment.executable, match_path)
                if key in known_path_rules or key in seen_paths:
                    continue
                seen_paths.add(key)
                offer = _path_rule_offer(
                    segment.executable,
                    match_path,
                    eligible=eligible,
                    sensitive_roots=sensitive_roots,
                    windows=win,
                )
                asks.append(
                    _ask(
                        f"The command targets a path outside the workspace: {path}.",
                        "workspace",
                        source="workspace",
                        rule_offer=offer,
                        kind="path",
                    )
                )
            continue

        verdict = _judge_segment(
            segment,
            rules=rules,
            cwd=cwd,
            roots=roots,
            windows=win,
            depth=_depth,
            known_rules=known_rules,
            known_path_rules=known_path_rules,
        )
        if verdict.action == "allow":
            continue
        shape = verdict.shape
        if verdict.rule_eligible and shape is not None and shape in known_rules:
            continue  # Silenced by an existing session/global rule.
        if shape is not None:
            if shape in seen_shapes:
                continue  # Same elementary command already represented.
            seen_shapes.add(shape)
        if verdict.rule_eligible and shape is not None:
            offer = _rule_offer(segment, shape, known=verdict.known_command)
            if offer is not None:
                # Sync `parts[0]` alongside the top-level mirror field — the
                # final assembly below reads `a.parts[0]` as its single
                # source of truth, so a post-hoc `rule_offer` update that
                # only touched the top-level field would silently vanish.
                verdict = replace(
                    verdict,
                    rule_offer=offer,
                    parts=(replace(verdict.parts[0], rule_offer=offer),),
                )
        asks.append(verdict)

    if not asks:
        return RuleDecision(
            action="allow",
            reason="Every command in the pipeline is a known-safe development operation.",
            category="benign-dev",
            source="rules",
        )

    primary = asks[0]
    reason = primary.reason if len(asks) == 1 else "; ".join(a.reason for a in asks)
    parts = tuple(a.parts[0] for a in asks)
    return replace(primary, reason=reason, parts=parts)


def _judge_segment(
    segment: NormalizedSegment,
    *,
    rules: tuple[CommandRule, ...],
    cwd: str,
    roots: tuple[str, ...],
    windows: bool,
    depth: int,
    known_rules: frozenset[tuple[str, str]] = frozenset(),
    known_path_rules: frozenset[tuple[str, str]] = frozenset(),
) -> RuleDecision:
    """One pipeline segment through red flags, the rule table, the default.

    Only ever reached for a segment with no outside-workspace finding of its
    own — the caller (``evaluate_command``) judges/offers those directly and
    never calls in here for them (doc/SECURITY_RULES_PLAN.md §2.7). The one
    place this function still needs ``known_path_rules`` is its own recursive
    ``evaluate_command`` call for a nested shell (``sh -c "…"``), so a path
    rule silences a wrapped occurrence exactly like a bare one.
    """
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
                known_rules=known_rules,
                known_path_rules=known_path_rules,
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
        return _ask(
            rule.reason, rule.category, shape=shape, eligible=rule.rule_eligible, known=True
        )

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


_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_path_like(token: str) -> bool:
    """A token that looks like it names a filesystem location.

    doc/SECURITY_RULES_PLAN.md §2.2 rule 3: a rule-eligible ask is only
    *offered* when none of its positional arguments are path-shaped — ``git
    push`` generalizes safely, ``pytest ../other/`` does not.
    """
    return bool(
        token and ("/" in token or "\\" in token or token == ".." or token.startswith("~"))
    ) or bool(_DRIVE_RE.match(token))


def _rule_offer(
    segment: NormalizedSegment,
    shape: tuple[str, str],
    *,
    known: bool,
) -> tuple[str, str] | None:
    """The Phase 2 "always allow" offer for *shape*, or ``None`` when this
    segment's own shape disqualifies it (doc/SECURITY_RULES_PLAN.md §2.2/
    §2.6). Category/rule eligibility (``RuleDecision.rule_eligible``) is
    checked by the caller before this runs — that gate is necessary but not
    sufficient: a segment carrying a value substitution or a nested/opaque
    command is still never offered. Judged **per segment**, not over the
    whole command line — a pipeline/`&&`/`;` chain is split and each
    elementary command is offered independently (§2.6); a plain output/input
    redirection to a workspace-confined file (``cat file.txt > out.txt``) no
    longer disqualifies the offer either, since the outside-workspace check
    still runs on every future invocation regardless of any granted rule,
    and the one real risk — a script piped into a shell/interpreter via
    ``<``/``<<`` — is caught upstream by the ``nested_command``/
    ``nested_opaque`` checks below, which were never offer-eligible to begin
    with.

    The path-argument check (§2.2 rule 3) is tiered by *known* (mirrors
    ``RuleDecision.known_command``):

    - A **known** command (matched an explicit, named ``CommandRule`` — e.g.
      ``apt install``) has its shape stored as ``(executable, subcommand)``
      regardless of what follows; granting the rule already ignores every
      argument after the subcommand, so a path-like one changes nothing —
      the rule generalizes over paths precisely as it generalizes over any
      other trailing argument (``git push`` generalizes over the remote).
    - An **unknown** command (the generic "not in the known-safe command
      set" default — e.g. a bespoke project CLI) has no such bounded
      category, so a path-like argument *after* the subcommand still
      disqualifies the offer: the stored shape can't capture it, and a
      future call with a different path would silently match the same
      rule. A path-like *subcommand itself* (``1brc ./data.txt``) is fine —
      the shape already pins the rule to that exact literal text, so a
      different file produces a different, non-matching shape and still
      asks.
    """
    if segment.has_substitution:
        return None
    if segment.nested_command is not None or segment.nested_opaque:
        return None
    if known:
        return shape
    if any(_is_path_like(arg) for arg in segment.args[1:]):
        return None
    return shape


# `cd`/PowerShell `Set-Location` (POSIX `chdir`/`sl` and the PowerShell
# aliases `chdir`/`sl` all normalize to one of these two canonical names
# during `_classify._normalize` before a segment ever reaches this module —
# no dialect branching needed here).
_CD_EXECUTABLES = frozenset({"cd", "set-location"})

# A small, hardcoded denylist: never offer-eligible even for an otherwise-
# eligible read-only/cd command (doc/SECURITY_RULES_PLAN.md §2.7) — the ask
# still happens (the user can always manually Allow once), just with no
# checkbox for that specific path. Expressed as (home-relative parts) tuples
# rather than bare strings so `_sensitive_roots` can join them cleanly.
_SENSITIVE_HOME_PARTS: tuple[tuple[str, ...], ...] = (
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".kube",),
    (".docker",),
    (".netrc",),
    (".npmrc",),
    (".pypirc",),
    (".config", "gcloud"),
)


def _sensitive_roots() -> tuple[str, ...]:
    """Credential-shaped locations under the current home directory.

    Computed fresh on every call — never cached at import time — so a test
    that redirects ``HOME`` (as ``test_security_store.py``'s ``_temp_home``
    fixture already does for the global rule store) sees the right roots,
    mirroring how ``_resolve`` already expands ``~`` per-token rather than
    once globally.
    """
    home = os.path.expanduser("~")
    return tuple(os.path.join(home, *parts) for parts in _SENSITIVE_HOME_PARTS)


def _normalize_path_key(path: str, *, windows: bool) -> str:
    """Fold *path* for rule-*matching* purposes only (mirrors
    ``_within_any_root``'s own comparison normalization: case-fold +
    slash-fold on Windows, unchanged on POSIX).

    Windows paths are case-insensitive, so a rule granted for ``C:\\Outside``
    must still silence a later call spelled ``c:\\outside`` — the *displayed*
    reason text keeps the original resolved casing (built from the
    unnormalized path in ``evaluate_command``); only the value used as the
    offer/dedup/``known_path_rules`` key goes through this fold, and the
    offer shown to the user is this normalized form (a small, deliberate
    cosmetic tradeoff — a lowercase drive letter — in exchange for the grant
    actually working reliably).
    """
    return path.replace("/", "\\").lower() if windows else path


def _is_path_offer_eligible(segment: NormalizedSegment) -> bool:
    """Whether *segment*'s executable is in the read-only/``cd`` bucket
    (doc/SECURITY_RULES_PLAN.md §2.7) and the segment carries no file-writing
    redirection.

    Judged **per segment**, not per argument: a write anywhere in the
    segment disqualifies *every* path offer for that segment, including an
    unrelated read target (``cat /etc/hosts > /etc/hosts2`` — the read of
    ``/etc/hosts`` doesn't get offered either, even though only the write
    target is the risky part). This mirrors ``_rule_offer``'s own
    segment-wide granularity for ``has_substitution``/nested-shell — a
    deliberate, conservative simplification, not an oversight.
    """
    eligible = _READONLY_EXECUTABLES | _READONLY_CMDLETS | _CD_EXECUTABLES
    return segment.executable in eligible and not segment.writes_file


def _path_rule_offer(
    executable: str,
    resolved_path: str,
    *,
    eligible: bool,
    sensitive_roots: tuple[str, ...],
    windows: bool,
) -> tuple[str, str] | None:
    """The Phase 2 "always allow" offer for one workspace-escaping path, or
    ``None`` when disqualified (doc/SECURITY_RULES_PLAN.md §2.7).

    Mirrors ``_rule_offer``'s role for command-shape asks, but for the
    workspace-escape category: ``eligible`` (the segment's own executable +
    no write, from ``_is_path_offer_eligible``) is necessary but not
    sufficient — a credential-shaped path (``_sensitive_roots``) is never
    offered even for an eligible executable. The returned shape's second
    element is the resolved *absolute path* itself (already computed by
    ``_analysis.analyze_command`` before this is ever called), not a
    subcommand — matching a future call means resolving its own argument the
    same way and comparing, not literal string equality (§2.7's
    "known_path_rules" lookup in ``evaluate_command`` does exactly this).
    """
    if not eligible:
        return None
    if _within_any_root(resolved_path, sensitive_roots, windows):
        return None
    return (executable, resolved_path)


def _strip_command_sub(snippet: str) -> str:
    """``$(inner)`` / ```inner``` → ``inner`` (best-effort, may be empty)."""
    if snippet.startswith("$("):
        return snippet[2:].rstrip(")").strip()
    return snippet.strip("`").strip()
