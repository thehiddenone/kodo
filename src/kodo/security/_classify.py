"""Normalized per-segment view of a parsed shell command.

Bridges the raw :class:`kodo.shellparser.Segment` tokens to the shape the rule
engine (:mod:`._rules`) matches on: a canonical executable name (wrapper
prefixes peeled, PowerShell aliases resolved), the subcommand, the flag
tokens, and the structural red-flag facts — embedded substitutions, nested
shell command strings, and inline-code arguments that defeat static analysis.

Everything here is lexical; nothing touches the filesystem or evaluates the
command. Classification is deliberately conservative: whenever a token cannot
be confidently normalized (an unrecognized wrapper form, a flag whose value
placement is ambiguous), the segment simply keeps its raw executable and the
rule engine's default-ask does the failing closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kodo.shellparser import ParsedCommand, Redirection, Segment

__all__ = ["NormalizedSegment", "SUB_MARK", "leaf_name", "normalize_segments"]

# Substitutions are masked to this marker BEFORE parsing (see ._analysis);
# any token carrying it is statically unresolvable.
SUB_MARK = "\x00sub\x00"

# Bourne-family shells: `-c` carries a nested command string; bare (no `-c`)
# as a pipe target means "execute whatever stdin says".
_SH_FAMILY = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# PowerShell hosts: `-Command` nests, `-EncodedCommand` is opaque by design.
_PS_FAMILY = frozenset({"pwsh", "powershell"})
# cmd.exe: `/c` / `/k` nest the remainder of the line.
_CMD_FAMILY = frozenset({"cmd"})
SHELL_EXECUTABLES = _SH_FAMILY | _PS_FAMILY | _CMD_FAMILY

# Interpreters whose `-c`/`-e`/`-E` argument is inline source code in another
# language — statically opaque, never parseable as a shell command.
_INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "ruby": frozenset({"-e"}),
    "perl": frozenset({"-e", "-E"}),
}

# Wrappers that just run their trailing command: peel and classify what's
# underneath. Value = number of *positional* tokens the wrapper consumes
# before the real command starts (after its own flags).
_TRANSPARENT_WRAPPERS: dict[str, int] = {
    "env": 0,  # plus VAR=val assignments, handled separately
    "nohup": 0,
    "time": 0,
    "nice": 0,
    "stdbuf": 0,
    "timeout": 1,  # the duration
}

# `VAR=value` prefix assignment (POSIX). The value may carry SUB_MARK.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# PowerShell alias / cmd builtin → canonical cmdlet, lowercased keys. Only
# aliases whose canonical form the default ruleset actually keys on; unknown
# names stay raw and fall to the default-ask.
_PS_ALIASES: dict[str, str] = {
    "rm": "remove-item",
    "del": "remove-item",
    "ri": "remove-item",
    "erase": "remove-item",
    "rd": "remove-item",
    "rmdir": "remove-item",
    "cp": "copy-item",
    "copy": "copy-item",
    "cpi": "copy-item",
    "mv": "move-item",
    "move": "move-item",
    "mi": "move-item",
    "ren": "rename-item",
    "rni": "rename-item",
    "ls": "get-childitem",
    "dir": "get-childitem",
    "gci": "get-childitem",
    "cat": "get-content",
    "type": "get-content",
    "gc": "get-content",
    "echo": "write-output",
    "write": "write-output",
    "pwd": "get-location",
    "gl": "get-location",
    "cd": "set-location",
    "chdir": "set-location",
    "sl": "set-location",
    "iwr": "invoke-webrequest",
    "curl": "invoke-webrequest",
    "wget": "invoke-webrequest",
    "irm": "invoke-restmethod",
    "iex": "invoke-expression",
    "start": "start-process",
    "saps": "start-process",
    "kill": "stop-process",
    "spps": "stop-process",
    "ps": "get-process",
    "gps": "get-process",
}

# Multi-word executables that dispatch a wrapped command after `--`
# (``mise exec node -- npm run build``).
_DASHDASH_RUNNERS = frozenset({("mise", "exec"), ("mise", "x")})


@dataclass(frozen=True)
class NormalizedSegment:
    """One pipeline segment, normalized for rule matching.

    Attributes:
        executable: Canonical executable name — wrapper prefixes peeled, path
            and ``.exe``-style suffix stripped, PowerShell aliases resolved to
            their cmdlet, lowercased. ``""`` for an empty segment.
        subcommand: First positional token after the executable (lowercased),
            or ``""`` — ``git push`` → ``"push"``.
        flags: Every flag token (``-x`` / ``--long[=v]`` / windows ``/s``),
            lowercased, in order.
        args: Every positional token after the executable (subcommand
            included), verbatim.
        has_substitution: Any token carries a masked substitution
            (``$VAR``, ``$(...)``, ``%VAR%`` …) — targets are unresolvable.
        nested_command: The inner command string of a ``sh -c`` / ``cmd /c`` /
            ``pwsh -Command`` segment, for recursive evaluation; ``None``
            otherwise.
        nested_opaque: The segment carries inline non-shell code
            (``python -c``, ``-EncodedCommand``) that cannot be analyzed.
        piped_input: The segment's stdin is the previous segment's pipe.
        writes_file: A redirection writes to a file (not a stream merge) —
            keeps a writer out of the read-only fast path; a plain,
            workspace-confined redirection no longer disqualifies the Phase 2
            "always allow" offer on its own (doc/SECURITY_RULES_PLAN.md §2.6).
    """

    executable: str
    subcommand: str = ""
    flags: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    has_substitution: bool = False
    nested_command: str | None = None
    nested_opaque: bool = False
    piped_input: bool = False
    writes_file: bool = False


def leaf_name(executable: str) -> str:
    """``/usr/bin/rm`` → ``rm``; ``C:\\Tools\\fd.EXE`` → ``fd``."""
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def normalize_segments(parsed: ParsedCommand, *, windows: bool) -> tuple[NormalizedSegment, ...]:
    """Normalize every segment of *parsed* (which must be substitution-masked).

    Args:
        parsed: The structural parse of a masked command line.
        windows: PowerShell/cmd dialect (aliases + ``/x`` switches) vs POSIX.

    Returns:
        tuple[NormalizedSegment, ...]: One entry per parsed segment, in order.
    """
    out: list[NormalizedSegment] = []
    for i, segment in enumerate(parsed.segments):
        piped = i > 0 and parsed.operators[i - 1] in ("|", "|&")
        out.append(_normalize(segment, windows=windows, piped_input=piped))
    return tuple(out)


def _normalize(segment: Segment, *, windows: bool, piped_input: bool) -> NormalizedSegment:
    tokens = [segment.executable, *segment.args] if segment.executable else list(segment.args)
    has_sub = any(SUB_MARK in t for t in tokens)
    writes = any(
        not r.operator.startswith("<") and not re.match(r"^&\d+$", r.target)
        for r in segment.redirections
    )
    if any(SUB_MARK in r.target for r in segment.redirections):
        has_sub = True

    tokens = _peel_prefixes(tokens, windows=windows)
    if not tokens:
        return NormalizedSegment(
            executable="",
            has_substitution=has_sub,
            piped_input=piped_input,
            writes_file=writes,
        )

    exe = leaf_name(tokens[0])
    rest = tokens[1:]

    # `python -m module …` runs *module* as the program: classify it as such
    # (`python -m pip install --user x` must hit the pip rules).
    if exe in ("python", "python3") and len(rest) >= 2 and rest[0] == "-m":
        exe = rest[1].lower()
        rest = rest[2:]

    if windows:
        exe = _PS_ALIASES.get(exe, exe)

    flags = tuple(t.lower() for t in rest if _is_flag(t, windows))
    positionals = tuple(t for t in rest if not _is_flag(t, windows))
    subcommand = positionals[0].lower() if positionals and SUB_MARK not in positionals[0] else ""

    nested, opaque = _nested_command(exe, rest, windows)
    if nested is None and not opaque:
        nested, opaque = _heredoc_nested_command(exe, positionals, segment.redirections)

    return NormalizedSegment(
        executable=exe,
        subcommand=subcommand,
        flags=flags,
        args=positionals,
        has_substitution=has_sub,
        nested_command=nested,
        nested_opaque=opaque,
        piped_input=piped_input,
        writes_file=writes,
    )


def _peel_prefixes(tokens: list[str], *, windows: bool) -> list[str]:
    """Strip env assignments and transparent-wrapper prefixes, iteratively."""
    while tokens:
        # POSIX `VAR=value cmd` prefix assignments.
        if not windows and _ENV_ASSIGN_RE.match(tokens[0]):
            tokens = tokens[1:]
            continue
        exe = leaf_name(tokens[0])
        if exe in _TRANSPARENT_WRAPPERS:
            rest = tokens[1:]
            # The wrapper's own flags (and, for env, VAR=val assignments).
            while rest and (
                rest[0].startswith("-") or (exe == "env" and _ENV_ASSIGN_RE.match(rest[0]))
            ):
                rest = rest[1:]
            rest = rest[_TRANSPARENT_WRAPPERS[exe] :]
            if not rest:
                return tokens  # Wrapper with nothing underneath: keep as-is.
            tokens = rest
            continue
        if len(tokens) >= 2 and (exe, tokens[1].lower()) in _DASHDASH_RUNNERS:
            if "--" in tokens:
                tokens = tokens[tokens.index("--") + 1 :]
                continue
            return tokens  # `mise exec …` without `--`: match as mise itself.
        return tokens
    return tokens


def _is_flag(token: str, windows: bool) -> bool:
    if token.startswith("-"):
        return True
    # Windows `/s`-style switches: a short, single-slash token.
    return windows and token.startswith("/") and token.count("/") == 1 and len(token) <= 4


def _heredoc_nested_command(
    exe: str, positionals: tuple[str, ...], redirections: tuple[Redirection, ...]
) -> tuple[str | None, bool]:
    """A bare shell/interpreter fed a here-document reads it as its program —
    same trust boundary as ``-c``/``-e`` (:func:`_nested_command`), just
    supplied over stdin instead of a flag.

    Only applies when there's no other positional (``bash script.sh <<EOF``
    feeds the heredoc to *script.sh*'s stdin as data, not to bash-as-code —
    the same "runs a workspace script" trust the flagless form already gets).
    A `-` placeholder positional (`python - <<EOF`, meaning "read the program
    from stdin") is caught too: `_is_flag` already treats a bare `-` as a
    flag, so it never reaches `positionals`.
    """
    if positionals:
        return None, False
    body = next(
        (r.heredoc_body for r in redirections if r.operator == "<<" and r.heredoc_body is not None),
        None,
    )
    if body is None:
        return None, False
    if exe in _SH_FAMILY:
        return body, False
    if exe in _INLINE_CODE_FLAGS:
        return None, True
    return None, False


def _nested_command(exe: str, rest: list[str], windows: bool) -> tuple[str | None, bool]:
    """Extract a nested command string / inline-code marker from *rest*."""
    inline = _INLINE_CODE_FLAGS.get(exe)
    if inline is not None and any(t in inline for t in rest):
        return None, True

    if exe in _SH_FAMILY:
        for i, tok in enumerate(rest):
            # `-c`, possibly clustered with login/interactive letters (`-lc`).
            if re.fullmatch(r"-[a-z]*c[a-z]*", tok):
                for nxt in rest[i + 1 :]:
                    if not nxt.startswith("-"):
                        return nxt, False
                return None, True  # `-c` with no script: opaque.
        return None, False

    if exe in _PS_FAMILY:
        lowered = [t.lower() for t in rest]
        if any(t in ("-encodedcommand", "-enc", "-ec", "-e") for t in lowered):
            return None, True
        for i, tok in enumerate(lowered):
            if tok in ("-command", "-c"):
                remainder = [t for t in rest[i + 1 :] if not t.startswith("-")]
                return (" ".join(remainder), False) if remainder else (None, True)
        return None, False

    if exe in _CMD_FAMILY:
        for i, tok in enumerate(rest):
            if tok.lower() in ("/c", "/k"):
                remainder = rest[i + 1 :]
                return (" ".join(remainder), False) if remainder else (None, True)
        return None, False

    return None, False
