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
from kodo.shellparser import ParsedCommand, parse_command, parse_powershell_command

from ._classify import SUB_MARK, NormalizedSegment, normalize_segments

__all__ = ["CommandAnalysis", "analyze_command"]

# Device sinks that read/write nowhere; never counted as outside targets.
_DEVICE_PATHS = frozenset(
    {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul"}
)

# Substitution/expansion markers that defeat static resolution. One regex per
# family so findings can quote the exact snippet. The first two families are
# *command* substitutions — they execute a nested command, which the rule
# engine analyzes recursively — the rest are value expansions.
_COMMAND_SUB_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\([^)]*\)?"),  # $(command) / $( unterminated
    re.compile(r"`[^`]*`?"),  # `command`
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

# Redirection targets like `&1` / `&2` merge streams; they are not files.
_FD_MERGE_RE = re.compile(r"^&\d+$")

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
    """

    outside_paths: tuple[str, ...]
    unresolved: tuple[str, ...]
    read_only: bool
    command_subs: tuple[str, ...] = ()
    segments: tuple[NormalizedSegment, ...] = ()
    operators: tuple[str, ...] = ()
    segment_outside_paths: tuple[tuple[str, ...], ...] = ()


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
            workspace-confined by the path resolver).
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
    # each affected token in one (marked, skipped) piece.
    unresolved: list[str] = []
    command_subs: list[str] = []
    masked = command
    for pattern in _SUBSTITUTION_RES:
        for match in pattern.finditer(masked):
            snippet = match.group()
            if snippet not in unresolved:
                unresolved.append(snippet)
                if pattern in _COMMAND_SUB_RES:
                    command_subs.append(snippet)
        masked = pattern.sub(_SUB_MARK, masked)

    parsed: ParsedCommand = parse_powershell_command(masked) if win else parse_command(masked)

    # Each segment gets its own accumulating list, so a path repeated across
    # segments (`cat /etc/hosts && grep x /etc/hosts`) is attributed to BOTH
    # occurrences — `_classify`'s own within-list dedup (`if resolved in
    # outside: return`) must not see a path a *different* segment already
    # recorded, or the second segment's finding would be silently dropped.
    per_segment: list[list[str]] = [[] for _ in parsed.segments]
    for i, segment in enumerate(parsed.segments):
        for arg in segment.args:
            _classify(arg, cwd, roots, win, per_segment[i])
        for redir in segment.redirections:
            target = redir.target
            if not target or _FD_MERGE_RE.match(target):
                continue
            _classify(target, cwd, roots, win, per_segment[i], force_path=True)

    # Flatten to the whole-command view, first-occurrence order, deduped —
    # reproduces exactly what the old single-shared-list pass computed.
    outside: list[str] = []
    seen: set[str] = set()
    for paths in per_segment:
        for path in paths:
            if path not in seen:
                seen.add(path)
                outside.append(path)

    segments = normalize_segments(parsed, windows=win)
    read_only = _is_read_only(segments, windows=win)
    return CommandAnalysis(
        outside_paths=tuple(outside),
        unresolved=tuple(unresolved),
        read_only=read_only,
        command_subs=tuple(command_subs),
        segments=segments,
        operators=parsed.operators,
        segment_outside_paths=tuple(tuple(paths) for paths in per_segment),
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
    tokens (statically unresolvable — reported separately), plain relative
    tokens (confined by *cwd*), and device sinks.  ``force_path`` marks tokens
    that are definitely paths (redirection targets), bypassing the flag check.
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

    resolved = _resolve(token, cwd, windows)
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


def _resolve(token: str, cwd: str, windows: bool) -> str | None:
    """Normalize *token* to an absolute path, or ``None`` when it cannot
    reference anything outside *cwd*'s subtree (plain relative / an option
    switch)."""
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

    # Relative: only worth resolving when `..` could climb out of cwd.
    parts = re.split(r"[\\/]", text)
    if ".." not in parts:
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
