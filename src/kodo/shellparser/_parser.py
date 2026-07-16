"""Tokenize a shell command line into a neutral structural view.

The parser is intentionally lossy and judgement-free: it recognises pipeline
segments, the operators joining them, each segment's executable + arguments, and
output/input redirections.  It does **not** evaluate variables, expand globs,
consume here-doc bodies, or decide whether a command mutates anything.  It never
raises — malformed input degrades to a best-effort single segment.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["ParsedCommand", "Redirection", "Segment", "parse_command"]

# Tokens that separate one pipeline segment from the next.
_SEGMENT_SEPARATORS = frozenset({"|", "|&", "||", "&&", ";", "&"})
# Redirection operators that stay *inside* a segment; the following token (if
# any) is the redirection target (a filename, or a here-doc/here-string word).
_REDIRECTION_OPS = frozenset({">", ">>", ">|", "<", "<<", "<<<", "<>", "&>", "&>>"})

# Characters `shlex` (with `punctuation_chars=True`) treats as operator
# punctuation — a token built entirely from these is a pure operator/grouping
# cluster, never a word or quoted content (both always contain some other
# character). Used to safely find `(`/`)` even when shlex has merged them
# with an adjacent operator (`)|`, `&&(`, …) into one token.
_OPERATOR_CHARS = frozenset("()|&;<>")
_GROUPING_RE = re.compile(r"([()])")

# `<<[-]DELIM` here-document start — deliberately excludes `<<<` (here-string,
# single-line, no body) via the lookaround pair. The delimiter is whichever of
# the quoted/bare groups matched; a `-` in group 1 means leading tabs are
# stripped from the terminator line (and, in real bash, from body lines —
# irrelevant here since the body is only ever discarded or recursed into
# verbatim, never re-emitted).
_HEREDOC_START_RE = re.compile(r"(?<!<)<<(?!<)(-)?\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+))")


@dataclass(frozen=True)
class Redirection:
    """One redirection within a segment.

    Attributes:
        operator: The redirection operator verbatim (e.g. ``'>'``, ``'>>'``,
            ``'<'``, ``'<<'``).
        target: The token following the operator — a filename for file
            redirections, or the delimiter/word for here-docs/here-strings.
            Empty when the command ended right after the operator.
        heredoc_body: For a ``'<<'`` operator, the here-document body text
            extracted from the lines following the operator (POSIX only —
            see :func:`_extract_heredocs`); ``None`` for every other
            redirection, and for a ``'<<'`` whose body couldn't be resolved
            (no matching terminator line).
    """

    operator: str
    target: str
    heredoc_body: str | None = None


@dataclass(frozen=True)
class Segment:
    """One command in a pipeline.

    Attributes:
        executable: The first word of the segment (the program), or ``''`` for
            an empty segment (e.g. a trailing operator).
        args: The remaining words, excluding redirection operators and their
            targets.
        redirections: The redirections attached to this segment, in order.
    """

    executable: str
    args: tuple[str, ...] = ()
    redirections: tuple[Redirection, ...] = ()


@dataclass(frozen=True)
class ParsedCommand:
    """Structural view of a full command line.

    Attributes:
        raw: The original command string.
        segments: The pipeline segments, in order.
        operators: The separator tokens joining the segments (``'|'``,
            ``'&&'``, ``';'``, …), in order. There is one operator between each
            adjacent pair of segments (a trailing operator yields a final empty
            segment).
    """

    raw: str
    segments: tuple[Segment, ...] = ()
    operators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def executables(self) -> tuple[str, ...]:
        """Every non-empty segment executable, in order."""
        return tuple(s.executable for s in self.segments if s.executable)

    @property
    def redirections(self) -> tuple[Redirection, ...]:
        """Every redirection across all segments, in order."""
        return tuple(r for s in self.segments for r in s.redirections)


def parse_command(command: str) -> ParsedCommand:
    """Parse *command* into a :class:`ParsedCommand`.

    Args:
        command: A shell command line, possibly spanning multiple physical
            lines when it carries a here-document (``<<DELIM`` /
            ``<<-DELIM``) — the body between the operator and its terminator
            line is extracted first (see :func:`_extract_heredocs`) so it
            never pollutes tokenization as bogus words/segments, and is
            attached to the owning ``'<<'`` :class:`Redirection` as
            ``heredoc_body`` for callers that want to recurse into it
            (:mod:`kodo.security`).

    Returns:
        ParsedCommand: The structural parse. Never raises; unparseable input
        falls back to a single best-effort segment.
    """
    raw = command
    reduced, bodies = _extract_heredocs(command)
    tokens = _strip_grouping(_tokenize(reduced))
    if not tokens:
        return ParsedCommand(raw=raw)

    segments: list[Segment] = []
    operators: list[str] = []
    words: list[str] = []
    redirs: list[Redirection] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SEGMENT_SEPARATORS:
            segments.append(_make_segment(words, redirs))
            operators.append(tok)
            words, redirs = [], []
        elif tok in _REDIRECTION_OPS:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target and target not in _SEGMENT_SEPARATORS and target not in _REDIRECTION_OPS:
                redirs.append(Redirection(operator=tok, target=target))
                i += 1
            else:
                redirs.append(Redirection(operator=tok, target=""))
        else:
            words.append(tok)
        i += 1

    segments.append(_make_segment(words, redirs))
    if bodies:
        body_iter = iter(bodies)
        segments = [_attach_heredoc_bodies(s, body_iter) for s in segments]
    return ParsedCommand(raw=raw, segments=tuple(segments), operators=tuple(operators))


def _attach_heredoc_bodies(segment: Segment, bodies: Iterator[str]) -> Segment:
    """Fill in ``heredoc_body`` on this segment's ``'<<'`` redirections, in
    the same left-to-right order :func:`_extract_heredocs` extracted them."""
    if not any(r.operator == "<<" for r in segment.redirections):
        return segment
    return Segment(
        executable=segment.executable,
        args=segment.args,
        redirections=tuple(
            r if r.operator != "<<" else Redirection(r.operator, r.target, next(bodies, None))
            for r in segment.redirections
        ),
    )


def _extract_heredocs(command: str) -> tuple[str, list[str]]:
    """Strip here-document bodies out of *command*, returning the reduced
    text (safe to tokenize as a single logical line) and the extracted body
    strings in left-to-right (source) order.

    Bodies begin right after the newline that ends the line carrying the
    operator(s) — real shells allow *more than one* ``<<DELIM`` on the same
    line (``cmd1 <<A | cmd2 <<B``), consuming their bodies consecutively in
    the order the operators appear, so this collects every marker on a
    triggering line before resolving any of their bodies (each search
    bounded to that one line, so a later heredoc's body text is never
    mistaken for a marker of its own).

    Best-effort, like the rest of this parser: an unterminated here-doc (no
    matching terminator line) has its body run to the end of the string
    rather than raising, and a delimiter this parser fails to recognize
    simply leaves the surrounding text untouched — the caller degrades to
    treating any leftover body text as ordinary (safe-failing) tokens, same
    as before this function existed.
    """
    bodies: list[str] = []
    out: list[str] = []
    cursor = 0
    length = len(command)
    while cursor < length:
        newline = command.find("\n", cursor)
        line_end = newline if newline != -1 else length
        markers: list[tuple[bool, str]] = []
        pos = cursor
        while True:
            m = _HEREDOC_START_RE.search(command, pos, line_end)
            if not m:
                break
            pos = m.end()
            delim = m.group(2) if m.group(2) is not None else m.group(3)
            if delim is None:
                delim = m.group(4)
            if delim:
                markers.append((m.group(1) == "-", delim))
        if not markers or newline == -1:
            out.append(command[cursor : line_end + 1] if newline != -1 else command[cursor:])
            cursor = line_end + 1 if newline != -1 else length
            continue
        out.append(command[cursor : newline + 1])
        body_start = newline + 1
        for strip_tabs, delim in markers:
            indent = r"[ \t]*" if strip_tabs else ""
            terminator = re.compile(rf"^{indent}{re.escape(delim)}[ \t]*\r?$", re.MULTILINE)
            tm = terminator.search(command, body_start)
            if tm is None:
                bodies.append(command[body_start:])
                body_start = length
                break
            bodies.append(command[body_start : tm.start()])
            line_end2 = command.find("\n", tm.end())
            body_start = line_end2 + 1 if line_end2 != -1 else length
        cursor = body_start
    return "".join(out), bodies


def _make_segment(words: list[str], redirs: list[Redirection]) -> Segment:
    executable = words[0] if words else ""
    args = tuple(words[1:]) if len(words) > 1 else ()
    return Segment(executable=executable, args=args, redirections=tuple(redirs))


def _strip_grouping(tokens: list[str]) -> list[str]:
    """Drop bare `(`/`)` subshell and `{`/`}` brace-group punctuation.

    Subshell/brace grouping only wraps a command sequence — it doesn't
    change what runs inside, so for the purposes of this parser (and its
    judgement-making callers) it is inert and can simply disappear, letting
    whatever separators live inside (`;`, `&&`, `|`, …) do their normal job.
    Two independent cases:

    - `(`/`)`: `shlex` already splits these out as their own tokens, but
      merges *runs* of pure operator characters together — `(cmd)|cat`
      yields a `")|"`token, `a&&(b` yields `"&&("` — so a token is only
      touched here when it is built *entirely* from operator characters
      (never true of a word or quoted content, which always contain some
      other character, e.g. a quoted literal `"(error)"` stays untouched).
    - `{`/`}`: not `shlex` punctuation, so a bare brace only ever appears as
      its own whitespace-delimited token (`{ cmd; }`) — never merged into
      `/tmp/{a,b}` or `find`'s `{}` placeholder, both single tokens already.
    """
    out: list[str] = []
    for tok in tokens:
        if tok in ("{", "}"):
            continue
        if tok and set(tok) <= _OPERATOR_CHARS and ("(" in tok or ")" in tok):
            out.extend(
                piece for piece in _GROUPING_RE.split(tok) if piece and piece not in ("(", ")")
            )
            continue
        out.append(tok)
    return out


def _tokenize(command: str) -> list[str]:
    """Best-effort POSIX tokenization that keeps operators as their own tokens."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes or similar — fall back to a naive split so the
        # parser still returns something usable rather than raising.
        return command.split()
