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


@dataclass(frozen=True)
class Redirection:
    """One redirection within a segment.

    Attributes:
        operator: The redirection operator verbatim (e.g. ``'>'``, ``'>>'``,
            ``'<'``, ``'<<'``).
        target: The token following the operator — a filename for file
            redirections, or the delimiter/word for here-docs/here-strings.
            Empty when the command ended right after the operator.
    """

    operator: str
    target: str


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
        command: A shell command line (single logical line; here-doc bodies on
            following lines are not consumed).

    Returns:
        ParsedCommand: The structural parse. Never raises; unparseable input
        falls back to a single best-effort segment.
    """
    raw = command
    tokens = _strip_grouping(_tokenize(command))
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
    return ParsedCommand(raw=raw, segments=tuple(segments), operators=tuple(operators))


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
