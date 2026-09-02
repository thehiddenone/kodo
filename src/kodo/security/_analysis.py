"""Workspace-target analysis of a ``run_command`` shell command.

Bridges the judgement-free :mod:`kodo.shellparser` structural parse to the one
question the security layer needs answered statically: **does this command
name any filesystem path outside the workspace roots?**  Plus two supporting
facts — whether the command contains shell substitutions that make its targets
statically unresolvable, and whether every executable is on a conservative
read-only allow-list (the "provably boring" fast path).

Only *arguments and redirection targets* are inspected; the executables
themselves are exempt (running ``/usr/bin/python`` is normal — the program is
not a *target*).  Relative tokens are only resolved when they contain ``..``;
plain relatives cannot escape the (already workspace-confined) working
directory and are skipped, which keeps subcommand words like ``install`` from
being mistaken for files.

A path under the OS temp directory (``kodo.common.system_temp_roots()``,
e.g. ``/tmp`` on POSIX) is never counted as "outside" even when it sits
outside every workspace root — scratch files there are expected agent
territory, not a workspace escape.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass

from kodo.common import system_temp_roots
from kodo.shellparser import (
    ParsedCommand,
    is_fd_merge_target,
    parse_command,
    parse_powershell_command,
)

from ._classify import CD_EXECUTABLES, SUB_MARK, NormalizedSegment, leaf_name, normalize_segments

__all__ = ["CommandAnalysis", "analyze_command"]

# Device sinks that read/write nowhere; never counted as outside targets.
_DEVICE_PATHS = frozenset(
    {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul"}
)

# PowerShell's null-device *variable* (not a filesystem path — `Get-Content
# x 2>$null` is the idiomatic devnull-equivalent on that dialect). Checked
# by literal (case-insensitive) text, alongside `is_fd_merge_target`, before a
# redirection target ever reaches `_resolve`/`_DEVICE_PATHS`: `$null` doesn't
# look like a path (no `/`, no drive letter), so it would otherwise fall
# through `_resolve`'s relative-token branch untouched anyway — this makes
# the exemption explicit and intentional instead of an accident of that
# branch, and (via `_mask_unless_ps_null` below) keeps it out of `unresolved`.
_PS_NULL_RE = re.compile(r"^\$null$", re.IGNORECASE)


def _mask_unless_ps_null(match: re.Match[str]) -> str:
    """Substitution callback: mask everything except a literal ``$null``."""
    snippet = match.group()
    return snippet if _PS_NULL_RE.match(snippet) else _SUB_MARK


# Substitution/expansion markers that defeat static resolution. One regex per
# family so findings can quote the exact snippet. The first two families are
# *command* substitutions — they execute a nested command, which the rule
# engine analyzes recursively — the rest are value expansions.
_COMMAND_SUB_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\([^)]*\)?"),  # $(command) / $( unterminated
    re.compile(r"`[^`]*`?"),  # `command`
    # Process substitution `<(cmd)` / `>(cmd)`: bash runs *cmd* and hands the
    # caller a /dev/fd path to its stream. It executes exactly like `$(...)`
    # does, so it is recursively judged the same way. Without this the whole
    # construct tokenized down to the inner command's stray arguments —
    # `diff <(rm -rf src) o.txt` parsed to a lone read-only `diff` segment
    # and auto-allowed on the read-only fast path.
    re.compile(r"[<>]\([^)]*\)?"),
)
_VALUE_SUB_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\{[^}]*\}?"),  # ${VAR}
    re.compile(r"\$[A-Za-z_][A-Za-z0-9_:.]*"),  # $VAR / $env:VAR
    re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%"),  # %VAR%
)
_SUBSTITUTION_RES: tuple[re.Pattern[str], ...] = _COMMAND_SUB_RES + _VALUE_SUB_RES

# Executables that only read (no flag of theirs writes a file) — the fast-path
# allow-list for SMART mode. Deliberately stricter than the checkpoint
# heuristic's list: `find` (-delete/-exec), `sort` (-o), and anything else with
# a write/exec flag is excluded, because a wrong answer here skips a review
# instead of just skipping a no-op git sweep. `date` and `hostname` are
# deliberately absent — both have a mutating form (`date -s`, `hostname
# <name>`) and are judged per-segment instead (`._rules._DUAL_MODE`).
_READONLY_EXECUTABLES = frozenset(
    {
        "echo",
        "printf",
        "ls",
        "dir",
        "pwd",
        "cat",
        "type",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "fd",
        "which",
        "where",
        "whoami",
        "id",
        "env",
        "printenv",
        "uname",
        "true",
        "false",
        "test",
        "[",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "stat",
        "file",
        "du",
        "df",
        "tree",
        "diff",
        "cmp",
        "uniq",
        "cut",
        "column",
        "tac",
        "nl",
        "seq",
        "expr",
        "sleep",
    }
)

# `which`/Windows `where` only report where a program lives on PATH — their
# positional arguments are program-name lookups, not data targets, the same
# way an executable's own name is exempt from the outside-workspace check
# (see the module docstring). So unlike every other reader's arguments
# (`cat /etc/hosts` still asks), an absolute-path argument here
# (`which /usr/bin/python3`) is never classified as a workspace-escape
# target either — unconditionally allowed via `read_only` (both names are
# already on `_READONLY_EXECUTABLES` above).
_PATH_QUERY_EXECUTABLES = frozenset({"which", "where"})

# PowerShell cmdlets that only read — the aliased form ``ls``/``cat``/etc.
# resolve to on Windows (``._classify._PS_ALIASES``) before reaching here, so
# the POSIX names above never match; checked in addition to
# ``_READONLY_EXECUTABLES`` when the segment was normalized in Windows mode.
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

# Substitutions are masked out of the command BEFORE parsing (see
# analyze_command): shlex would otherwise split `$(pwd)/y` into fragments and
# a bare `/y` would masquerade as an absolute path. Any token carrying the
# marker is statically unresolvable and skipped by the outside-path check.
# The marker itself lives in ._classify, which shares it with the rule engine.
_SUB_MARK = SUB_MARK


@dataclass(frozen=True)
class CommandAnalysis:
    """Static facts about one shell command, relative to the workspace roots.

    Attributes:
        outside_paths: Argument/redirection tokens that resolve to a path
            *outside* every workspace root and outside the OS temp directory
            (normalized absolute form), flattened across the whole command in
            first-occurrence order — derived from ``segment_outside_paths``
            below, kept for callers that only care about the whole-command
            fact (e.g. logging).
        segment_outside_paths: The same findings, but attributed per segment
            — one sub-tuple per ``segments`` entry, positionally aligned
            (``normalize_segments`` never filters, so the two tuples are
            always the same length). This is what the rule engine judges on
            (doc/SECURITY_RULES_PLAN.md §2.7): a segment with a non-empty
            entry here gets its own per-path ask, independent of what other
            segments in the same pipeline are doing.
        unresolved: Substitution snippets (``$(...)``, ``$VAR``, ``%VAR%`` …)
            that make targets statically unresolvable.
        command_subs: The subset of ``unresolved`` that *executes* a nested
            command (``$(...)`` / backticks) — the rule engine recurses into
            these; value expansions merely defeat path resolution.
        read_only: ``True`` when every executable in the pipeline is on the
            conservative read-only allow-list and no redirection writes a file.
        segments: The normalized per-segment view (:mod:`._classify`) the rule
            engine matches on.
        operators: The separators joining the segments, verbatim from the
            parse (``'|'``, ``'&&'``, …).
        segment_toolchain_script: Per segment (positionally aligned with
            ``segments``), whether that segment's own executable directly
            invokes one of ``toolchain_builder``'s generated
            ``scripts/<step>.{sh,ps1}`` entrypoints under a workspace root
            (``build``/``format``/``static_analysis``/``test``/
            ``full_build`` — see ``subagent_toolchain_builder.md`` Phase 4).
            Running one *through* a shell (``bash scripts/build.sh``) is
            already covered by the "shell running a workspace script" rule
            (``._rules._judge_segment``'s ``SHELL_EXECUTABLES`` branch); this
            covers the direct-invocation form (``./scripts/build.sh``,
            ``.\\scripts\\build.ps1``) that would otherwise fall through to
            the generic "not in the known-safe command set" default, since
            the script's own basename is never on any allow-list. Matched
            against each segment's own *effective* cwd (``_track_cwd``), not
            necessarily the command's declared one — ``cd <project> &&
            ./scripts/build.sh`` is recognized the same as passing
            ``working_dir`` separately.
    """

    outside_paths: tuple[str, ...]
    unresolved: tuple[str, ...]
    read_only: bool
    command_subs: tuple[str, ...] = ()
    segments: tuple[NormalizedSegment, ...] = ()
    operators: tuple[str, ...] = ()
    segment_outside_paths: tuple[tuple[str, ...], ...] = ()
    segment_toolchain_script: tuple[bool, ...] = ()


def analyze_command(
    command: str,
    *,
    cwd: str,
    roots: tuple[str, ...],
    windows: bool | None = None,
) -> CommandAnalysis:
    """Analyze *command*'s filesystem targets against the workspace *roots*.

    Args:
        command: The raw shell command line.
        cwd: The directory the command will run in (absolute; already
            workspace-confined by the path resolver) — the *declared*
            starting cwd. An inline ``cd``/``Set-Location`` earlier in the
            same ``&&``/``;``/``||`` chain shifts the *effective* cwd used
            for everything after it (see :func:`_track_cwd`); *cwd* itself
            is only ever what the first segment sees.
        roots: Absolute workspace root paths.
        windows: Parse as PowerShell/cmd (``True``) or POSIX (``False``);
            defaults to the current platform.

    Returns:
        CommandAnalysis: The static findings. Purely lexical over the
        command's own tokens — no filesystem access beyond ``~`` expansion
        and resolving the small, fixed set of OS-temp-directory candidates
        (``kodo.common.system_temp_roots()``).
    """
    win = os.name == "nt" if windows is None else windows

    # Collect substitution snippets, then mask them so the tokenizer keeps
    # each affected token in one (marked, skipped) piece. `$null` is PowerShell's
    # devnull-equivalent (a variable, not a path) — masking it here would still
    # leave it harmless (`_classify` already skips `_SUB_MARK`-carrying tokens),
    # but recognizing it explicitly, like `is_fd_merge_target` below, keeps it out of
    # `unresolved` and documents the exemption as intentional rather than
    # incidental.
    unresolved: list[str] = []
    command_subs: list[str] = []
    masked = command
    for pattern in _SUBSTITUTION_RES:
        for match in pattern.finditer(masked):
            snippet = match.group()
            if win and _PS_NULL_RE.match(snippet):
                continue
            if snippet not in unresolved:
                unresolved.append(snippet)
                if pattern in _COMMAND_SUB_RES:
                    command_subs.append(snippet)
        if win:
            masked = pattern.sub(_mask_unless_ps_null, masked)
        else:
            masked = pattern.sub(_SUB_MARK, masked)

    parsed: ParsedCommand = parse_powershell_command(masked) if win else parse_command(masked)
    segments = normalize_segments(parsed, windows=win)

    # The cwd each segment actually runs in — shifts after an inline `cd`/
    # `Set-Location` earlier in the same &&/;/|| chain (see `_track_cwd`).
    effective_cwds = _track_cwd(segments, parsed.operators, cwd, win)

    # Each segment gets its own accumulating list, so a path repeated across
    # segments (`cat /etc/hosts && grep x /etc/hosts`) is attributed to BOTH
    # occurrences — `_classify`'s own within-list dedup (`if resolved in
    # outside: return`) must not see a path a *different* segment already
    # recorded, or the second segment's finding would be silently dropped.
    per_segment: list[list[str]] = [[] for _ in parsed.segments]
    for i, segment in enumerate(parsed.segments):
        seg_cwd = effective_cwds[i]
        exe_leaf = leaf_name(segment.executable) if segment.executable else ""
        if exe_leaf not in _PATH_QUERY_EXECUTABLES:
            for arg in segment.args:
                _classify(arg, seg_cwd, roots, win, per_segment[i])
        for redir in segment.redirections:
            target = redir.target
            if not target or is_fd_merge_target(target) or (win and _PS_NULL_RE.match(target)):
                continue
            _classify(target, seg_cwd, roots, win, per_segment[i], force_path=True)

    # Flatten to the whole-command view, first-occurrence order, deduped —
    # reproduces exactly what the old single-shared-list pass computed.
    outside: list[str] = []
    seen: set[str] = set()
    for paths in per_segment:
        for path in paths:
            if path not in seen:
                seen.add(path)
                outside.append(path)

    read_only = _is_read_only(segments, windows=win)
    segment_toolchain_script = tuple(
        bool(segment.executable)
        and _toolchain_script_hit(segment.executable, effective_cwds[i], roots, win)
        for i, segment in enumerate(parsed.segments)
    )
    return CommandAnalysis(
        outside_paths=tuple(outside),
        unresolved=tuple(unresolved),
        read_only=read_only,
        command_subs=tuple(command_subs),
        segments=segments,
        operators=parsed.operators,
        segment_outside_paths=tuple(tuple(paths) for paths in per_segment),
        segment_toolchain_script=segment_toolchain_script,
    )


def _is_read_only(segments: tuple[NormalizedSegment, ...], *, windows: bool) -> bool:
    """Every executable allow-listed and no file-writing redirection.

    Operates on the *normalized* (wrapper-peeled) segments rather than the
    raw parse, so a transparent wrapper can't hide a mutating command behind
    a read-only-looking prefix — ``env rm -rf x`` must resolve to ``rm``, not
    short-circuit on ``env`` itself. In Windows mode, ``._classify`` has
    already resolved PowerShell aliases (``ls`` → ``get-childitem``), so the
    allow-list is widened with the cmdlet names.
    """
    if any(segment.writes_file for segment in segments):
        return False
    named = [segment for segment in segments if segment.executable]
    if not named:
        return False
    readonly = _READONLY_EXECUTABLES | _READONLY_CMDLETS if windows else _READONLY_EXECUTABLES
    return all(
        segment.executable in readonly
        and segment.nested_command is None
        and not segment.nested_opaque
        for segment in named
    )


# Operators that keep running in the SAME shell process, so an inline `cd`
# on the left is still in effect for the right (`cd x && build`, `cd x;
# build`, `cd x || build`). `|`/`|&` fork a subshell per side (POSIX; a
# PowerShell pipe stage is likewise its own process), and `&` backgrounds its
# left side without waiting — a `cd` there is never reliably in effect (in
# this process or any other) before the next segment starts. Neither is
# tracked across; a `cd` on the far side of one just doesn't update the
# chain, the same as an unresolvable target (see `_track_cwd`).
_SEQUENTIAL_OPERATORS = frozenset({"&&", ";", "||"})


def _resolve_absolute(token: str, cwd: str, windows: bool) -> str:
    """Normalize *token* to an absolute path, joined against *cwd* when
    relative.

    Unlike ``_resolve`` (which returns ``None`` for a plain relative token
    that can't escape a *confined* cwd — the outside-workspace check only
    needs an escape verdict, not the concrete path), this always resolves:
    for callers that need the literal absolute path itself — the
    toolchain-script exact-path match, and tracking an inline ``cd``'s effect
    on later segments' cwd (``_track_cwd``).
    """
    mod = ntpath if windows else posixpath
    text = os.path.expanduser(token) if token.startswith("~") else token
    if windows:
        is_abs = bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("\\\\")
    else:
        is_abs = text.startswith("/")
    return str(mod.normpath(text) if is_abs else mod.normpath(mod.join(cwd, text)))


def _track_cwd(
    segments: tuple[NormalizedSegment, ...],
    operators: tuple[str, ...],
    cwd: str,
    windows: bool,
) -> tuple[str, ...]:
    """The effective working directory for each segment, following any
    inline ``cd``/``Set-Location`` earlier in the same ``&&``/``;``/``||``
    chain.

    A ``run_command`` call states its cwd once (``working_dir``, defaulting
    to the project root) — but an agent very often writes ``cd <dir> &&
    <rest>`` directly in the command string instead, the same way a person
    would at a terminal. Every cwd-relative check downstream — the
    outside-workspace resolver in ``analyze_command``, and the
    toolchain-builder-script fast path in ``_toolchain_script_hit`` — used to
    stay anchored to the single declared *cwd* regardless, so an ordinary
    ``cd /project/subdir && ./scripts/build.sh`` resolved
    ``./scripts/build.sh`` against the wrong directory and fell through to
    the generic default-ask instead of the toolchain-script allow.

    Only a ``cd`` whose target is a single, substitution-free positional
    argument updates the tracked cwd. A bare ``cd`` (goes to ``$HOME``),
    ``cd -`` (previous directory), or a ``cd $VAR``/``cd $(...)`` target all
    leave the chain right where it was and are otherwise ignored — this
    fails *closed*, not silently correct: every segment from that point on
    keeps the last *known-good* cwd rather than a guess, so an unrecognized
    command still asks instead of being misjudged as confined to (or
    escaping) the wrong directory.
    """
    effective = [cwd] * len(segments)
    current = cwd
    last = len(segments) - 1
    for i, segment in enumerate(segments):
        effective[i] = current
        if i >= last or operators[i] not in _SEQUENTIAL_OPERATORS:
            continue
        if segment.executable not in CD_EXECUTABLES:
            continue
        if segment.has_substitution or len(segment.args) != 1 or segment.args[0] == "-":
            continue
        current = _resolve_absolute(segment.args[0], current, windows)
    return tuple(effective)


# The five entrypoints `toolchain_builder` always generates as a per-platform
# pair under `scripts/` at the project root (subagent_toolchain_builder.md
# Phase 4) — a fixed, project-relative convention, not configurable.
_TOOLCHAIN_SCRIPT_STEPS = ("build", "format", "static_analysis", "test", "full_build")


def _toolchain_script_hit(token: str, cwd: str, roots: tuple[str, ...], windows: bool) -> bool:
    """Whether *token* (a segment's raw, un-normalized executable) directly
    invokes ``scripts/<step>.sh`` (POSIX) / ``scripts\\<step>.ps1`` (Windows)
    under one of *roots*, for one of the five toolchain_builder step names.

    Only a *path* invocation qualifies (``./scripts/build.sh``,
    ``scripts/build.sh``, an absolute path under a root) — a bare ``build.sh``
    found via ``PATH`` does not, since toolchain_builder never installs
    anything onto ``PATH``; requiring a path separator also keeps this from
    ever matching an unrelated same-named program. No workspace loaded
    (``roots`` empty) never matches either — there is no root to anchor
    ``scripts/`` under. *cwd* is this segment's own *effective* cwd
    (``analyze_command``'s ``_track_cwd`` result), not necessarily the
    command's originally-declared one — an inline ``cd`` earlier in the same
    ``&&``/``;``/``||`` chain already shifted it by the time this runs.
    """
    if not roots or ("/" not in token and "\\" not in token):
        return False
    mod = ntpath if windows else posixpath
    resolved = _resolve_absolute(token, cwd, windows)
    norm_cmp = resolved.replace("/", "\\").lower() if windows else resolved
    ext = "ps1" if windows else "sh"
    for root in roots:
        for step in _TOOLCHAIN_SCRIPT_STEPS:
            candidate = mod.normpath(mod.join(root, "scripts", f"{step}.{ext}"))
            candidate_cmp = candidate.replace("/", "\\").lower() if windows else candidate
            if norm_cmp == candidate_cmp:
                return True
    return False


def _classify(
    token: str,
    cwd: str,
    roots: tuple[str, ...],
    windows: bool,
    outside: list[str],
    *,
    force_path: bool = False,
) -> None:
    """Append *token*'s resolved path to *outside* when it escapes every root
    and the OS temp directory.

    Skips option flags (checking any ``=``-attached value), substitution-laden
    tokens (statically unresolvable — reported separately), and device sinks.
    A plain relative token is normally skipped too (confined by *cwd*) — but
    only when *roots* is non-empty (a workspace is loaded). With no workspace
    there is no confined *cwd* to trust, so every non-flag token, relative or
    not, is resolved and checked (doc/SECURITY_RULES_PLAN.md §2.7a): the only
    thing that still passes is the OS temp directory. ``force_path`` marks
    tokens that are definitely paths (redirection targets), bypassing the
    flag check.
    """
    if not token:
        return
    if _SUB_MARK in token:
        return  # Unresolvable — already reported via `unresolved`.
    if not force_path and token.startswith("-"):
        _, sep, value = token.partition("=")
        if sep and value:
            _classify(value, cwd, roots, windows, outside, force_path=True)
        return

    resolved = _resolve(token, cwd, windows, confined=bool(roots))
    if resolved is None:
        return
    if resolved.replace("\\", "/").lower() in _DEVICE_PATHS:
        return
    if resolved in outside:
        return
    if _within_any_root(resolved, roots, windows):
        return
    if _within_any_root(resolved, system_temp_roots(), windows):
        return
    outside.append(resolved)


def _resolve(token: str, cwd: str, windows: bool, *, confined: bool = True) -> str | None:
    """Normalize *token* to an absolute path, or ``None`` when it cannot
    reference anything outside *cwd*'s subtree (plain relative / an option
    switch).

    Args:
        confined: Whether a workspace is loaded (``roots`` non-empty). When
            ``True`` (the ordinary case), a plain relative token — no ``..``
            — is trusted to stay under the already-confined *cwd* and is
            never resolved at all (returns ``None``). When ``False`` there is
            no confined *cwd* to trust — *cwd* is then the session's private
            scratch directory, merely where a workspace-less ``run_command``
            defaults to running (``ToolContext.command_cwd``), which is
            outside every root and outside the OS temp roots — so every
            relative token is resolved the same way an absolute one would be,
            anchored against it. The empty *roots* then makes
            ``_within_any_root`` reject the result unconditionally in
            ``_classify``, same as any other unproven path, short of the OS
            temp carve-out.
    """
    mod = ntpath if windows else posixpath
    text = token

    if text.startswith("~"):
        text = os.path.expanduser(text)

    if windows:
        is_abs = bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("\\\\")
        # A single leading slash on Windows is almost always a switch (`dir /s`)
        # unless it clearly forms a path (`/etc/passwd` style, a second slash).
        if not is_abs and text.startswith("/"):
            if text.count("/") < 2:
                return None
            is_abs = True
    else:
        is_abs = text.startswith("/")

    if is_abs:
        return str(mod.normpath(text))

    # Relative: with a confined cwd, only worth resolving when `..` could
    # climb out of it — a plain relative token can't otherwise escape. With
    # no confined cwd (`confined=False`), that guarantee doesn't hold, so
    # every relative token is resolved instead of skipped.
    parts = re.split(r"[\\/]", text)
    if confined and ".." not in parts:
        return None
    return str(mod.normpath(mod.join(cwd, text)))


def _within_any_root(path: str, roots: tuple[str, ...], windows: bool) -> bool:
    """Whether *path* sits at or below any of *roots* (lexical containment)."""
    mod = ntpath if windows else posixpath
    norm = mod.normpath(path)
    norm_cmp = norm.replace("/", "\\").lower() if windows else norm
    for root in roots:
        root_norm = mod.normpath(root)
        root_cmp = root_norm.replace("/", "\\").lower() if windows else root_norm
        if norm_cmp == root_cmp or norm_cmp.startswith(root_cmp.rstrip("\\/") + mod.sep):
            return True
    return False
