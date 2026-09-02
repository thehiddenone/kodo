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

__all__ = [
    "ParsedCommand",
    "Redirection",
    "Segment",
    "is_fd_merge_target",
    "parse_command",
    "redirection_writes_file",
]

# Tokens that separate one pipeline segment from the next.
_SEGMENT_SEPARATORS = frozenset({"|", "|&", "||", "&&", ";", "&"})
# Redirection operators that stay *inside* a segment; the following token (if
# any) is the redirection target (a filename, or a here-doc/here-string word).
_REDIRECTION_OPS = frozenset({">", ">>", ">|", "<", "<<", "<<<", "<>", "&>", "&>>"})

# A redirection target like `&1` / `&2` merges/duplicates a stream; it never
# names a file. Shared by `redirection_writes_file` below and by
# `kodo.security._analysis` (which also needs to recognize a merge target on
# its own, before deciding whether to resolve it as a path at all).
_FD_MERGE_RE = re.compile(r"^&\d+$")

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

# A stream-number prefix (`2>`) and/or fd-duplication suffix (`>&1`) glued
# directly onto `>`, `>>`, `>|`, `<>`, or `<` — POSIX IO_NUMBER grammar, which
# only applies when the digit touches the operator with no intervening
# whitespace (`cmd 1 > file` is the plain word "1" then a redirect; `cmd
# 1>file` is fd 1 redirected). `shlex` (see `_tokenize`) can't tell those two
# apart — it only tracks character-class transitions, not whitespace — so
# this match happens on the raw (heredoc-reduced) text, before tokenization,
# exactly like `_extract_heredocs` above. The digit group's negative
# lookbehind keeps a word that merely *ends* in digits (`file2>x`) from being
# misread as an IO_NUMBER: `shlex` already keeps "file2" intact on its own
# (it only splits at word/punctuation-class boundaries), so this regex only
# needs to reject a digit run that isn't its own token. `<<`/`<<<` are
# deliberately excluded — heredoc bodies are already extracted above, and a
# stream-numbered heredoc (`2<<EOF`) is not a supported form.
_STREAM_REDIR_RE = re.compile(r"(?:(?<![\w])([0-9]{1,3}))?(>>|>\||<>|>|<)(?:(&[0-9]{1,3}))?")

# A single- or double-quoted span (best-effort — mirrors the rest of this
# parser's non-raising, non-exhaustive quoting support), used to keep
# `_protect_stream_redirects` from rewriting literal `>`/`<`/digits inside a
# quoted string.
_QUOTE_SPAN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")

# Placeholder wrapping a `_STREAM_REDIR_RE` match that actually carries an
# IO_NUMBER and/or fd-duplication target (a bare `>`/`<` with neither is left
# untouched — see `_protect_stream_redirects`). `\x00` never appears in a
# real command and isn't one of `shlex`'s punctuation/whitespace characters,
# so the placeholder survives `_tokenize` as a single opaque word.
_REDIR_PLACEHOLDER_RE = re.compile(r"\x00RDR(\d+)\x00")

# A newline is a POSIX command separator, exactly equivalent to `;` — but
# `shlex` classifies it as ordinary whitespace, so without this pass every
# line after the first silently collapses into the *arguments* of the first
# line's command (`ls\nrm -rf src` parsed to a single `ls` segment carrying
# "rm -rf src" as args). Multi-line commands are routine here — `run_command`
# supports here-documents — so the separator is masked to an opaque token
# before tokenization, the same way `_protect_stream_redirects` protects an
# IO_NUMBER redirect, and folded back into a `;` afterwards.
#
# The alternation is scanned in order, so each branch consumes its match
# whole: a backslash-newline is a *line continuation* (bash removes it; it
# becomes a single space here, never a separator), any other backslash-escape
# is passed through untouched so an escaped backslash can't swallow the
# newline behind it (`\\` then a real newline still separates), and only a
# bare newline becomes the separator token.
_LINE_BREAK_RE = re.compile(r"\\\r?\n|\\.|\r?\n", re.DOTALL)

# The masked separator. Padded with spaces on both sides when inserted, and
# placed *after* the newline it replaces — `shlex`'s `#` comment handling
# runs to the end of the line, so a token placed before the newline would be
# eaten along with the comment (`ls # note\nrm -rf x`) and the separator lost.
_LINE_BREAK_TOKEN = "\x00NL\x00"


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


def is_fd_merge_target(target: str) -> bool:
    """Whether *target* is a stream merge/duplication (``&1``, ``&2``, …)
    rather than a filename — the target half of a redirection like ``2>&1``
    or PowerShell's ``2>&1``/``*>&1``.
    """
    return bool(_FD_MERGE_RE.match(target))


def redirection_writes_file(redirection: Redirection) -> bool:
    """Whether *redirection* opens a real file for writing.

    True for any output-direction operator — ``>``, ``>>``, ``>|``, ``&>``,
    ``&>>``, PowerShell's ``*>``/``*>>``, and their POSIX stream-qualified
    forms (``2>``, ``1>>``, …) — and for ``<>`` (opens for reading *and*
    writing). False for a pure input redirection (``<``, ``<<``, ``<<<``,
    stream-qualified ``N<``) and for any operator whose target is a stream
    merge/duplication (:func:`is_fd_merge_target`) rather than a file —
    ``2>&1`` doesn't write anywhere, it just merges stderr into stdout.

    Judged purely on operator shape, shared by every caller that needs to
    know "does this redirection touch a file on disk" —
    :mod:`kodo.security` (workspace-escape/read-only classification) and
    :mod:`kodo.runtime` (the checkpoint mutation heuristic) — so the two
    layers can't quietly drift apart on what counts as a write.
    """
    if is_fd_merge_target(redirection.target):
        return False
    # Strip a POSIX IO_NUMBER prefix (`2>` -> `>`) — irrelevant to direction.
    op = redirection.operator.lstrip("0123456789")
    if op == "<>":
        return True
    return not op.startswith("<")


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
    broken = _protect_newlines(reduced)
    protected, stream_redirs = _protect_stream_redirects(broken)
    tokens = _fold_line_breaks(_strip_grouping(_tokenize(protected)))
    if not tokens:
        return ParsedCommand(raw=raw)

    segments: list[Segment] = []
    operators: list[str] = []
    words: list[str] = []
    redirs: list[Redirection] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        placeholder = _REDIR_PLACEHOLDER_RE.fullmatch(tok)
        if tok in _SEGMENT_SEPARATORS:
            segments.append(_make_segment(words, redirs))
            operators.append(tok)
            words, redirs = [], []
        elif tok in _REDIRECTION_OPS or placeholder is not None:
            if placeholder is not None:
                io_number, base_op, fd_target = stream_redirs[int(placeholder.group(1))]
                operator = f"{io_number}{base_op}" if io_number else base_op
            else:
                operator, fd_target = tok, None
            if fd_target is not None:
                redirs.append(Redirection(operator=operator, target=fd_target))
            else:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                valid_target = (
                    target
                    and target not in _SEGMENT_SEPARATORS
                    and target not in _REDIRECTION_OPS
                    and _REDIR_PLACEHOLDER_RE.fullmatch(target) is None
                )
                if valid_target:
                    redirs.append(Redirection(operator=operator, target=target))
                    i += 1
                else:
                    redirs.append(Redirection(operator=operator, target=""))
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


def _protect_newlines(text: str) -> str:
    """Mark every newline in *text* that acts as a command separator, so it
    survives ``shlex`` (which treats it as ordinary whitespace) as its own
    token instead of silently vanishing.

    Quoted spans are passed through verbatim via `_QUOTE_SPAN_RE` — the same
    guard `_protect_stream_redirects` uses — so a literal newline inside
    ``echo "line one\nline two"`` stays data. Here-document bodies were
    already lifted out by :func:`_extract_heredocs` before this runs, so
    their newlines are never seen here either. A backslash-newline line
    continuation collapses to a space (what bash does with it) rather than
    separating.

    Returns:
        str: The rewritten text. Every separator newline is kept *and*
        followed by a space-padded `_LINE_BREAK_TOKEN`; :func:`_fold_line_breaks`
        turns the surviving tokens into `;` operators once tokenization has
        run.
    """

    def replace(match: re.Match[str]) -> str:
        text = match.group()
        if text.startswith("\\"):
            # `\<newline>` is a line continuation; any other `\x` escape is
            # passed through so it can't swallow a following newline.
            return " " if text.endswith("\n") else text
        return f"{text} {_LINE_BREAK_TOKEN} "

    out: list[str] = []
    cursor = 0
    for qm in _QUOTE_SPAN_RE.finditer(text):
        out.append(_LINE_BREAK_RE.sub(replace, text[cursor : qm.start()]))
        out.append(qm.group())
        cursor = qm.end()
    out.append(_LINE_BREAK_RE.sub(replace, text[cursor:]))
    return "".join(out)


def _fold_line_breaks(tokens: list[str]) -> list[str]:
    """Turn each surviving `_LINE_BREAK_TOKEN` into a `;` separator.

    A newline that directly follows a control operator *continues* the
    command rather than separating it (``curl x |\n    sh``, ``a &&\n b``) —
    emitting a separator there would split the pipeline and lose the
    operator's own meaning, notably ``|``'s piped-stdin fact, which the
    security layer keys its "pipes data into a shell" finding on. Those
    line breaks are dropped instead, leaving the real operator to join the
    two sides exactly as if they had been written on one line. A leading
    line break (a command starting with a blank or comment-only line) is
    dropped for the same reason: there is nothing to its left to separate,
    and so is a trailing one, or one whose next real token is itself a
    separator — nothing to its right, or an explicit operator already doing
    the job.

    `;` rather than a distinct operator because a newline *is* `;` in POSIX
    sequencing semantics, and every downstream consumer already handles `;`
    correctly — `._analysis._SEQUENTIAL_OPERATORS` (inline-``cd`` tracking)
    and `._classify.normalize_segments` (the ``|``-only piped-input check).
    """
    out: list[str] = []
    for i, token in enumerate(tokens):
        if token != _LINE_BREAK_TOKEN:
            out.append(token)
            continue
        if not out or out[-1] in _SEGMENT_SEPARATORS:
            continue
        following = next((t for t in tokens[i + 1 :] if t != _LINE_BREAK_TOKEN), None)
        if following is None or following in _SEGMENT_SEPARATORS:
            # Nothing left to separate: a trailing newline (every heredoc's
            # operator line ends in one), or an explicit operator on the next
            # line taking over the join (`cmd\n; other`). Emitting here would
            # only manufacture an empty segment.
            continue
        out.append(";")
    return out


def _protect_stream_redirects(text: str) -> tuple[str, list[tuple[str | None, str, str | None]]]:
    """Replace every IO_NUMBER/fd-duplication redirect in *text* with an
    opaque placeholder, so it survives ``shlex`` tokenization as one word
    instead of shattering into stray digit/punctuation tokens.

    A bare operator with neither a digit prefix nor an ``&N`` suffix (the
    overwhelming majority of redirects) is left completely untouched — this
    only touches the forms `_REDIRECTION_OPS`/plain ``shlex`` tokenizing
    can't already handle correctly. Quoted spans are matched and passed
    through verbatim (via `_QUOTE_SPAN_RE`) so a literal ``"2>&1"`` inside a
    string is never mistaken for a redirect.

    The placeholder is always padded with a leading and trailing space,
    regardless of the original spacing — a real target is very often glued
    directly onto the operator with no space at all (``2>/dev/null``,
    ``1>file``), and without a synthetic space there `shlex` would fuse the
    placeholder onto that adjacent text into one unrecognizable word (same
    risk on the left: ``foo>&2`` would otherwise fuse "foo" onto the
    placeholder). The padding only ever *adds* a token boundary — `shlex`
    collapses whitespace runs, so it can't merge or drop anything that was
    a real token already — which lets :func:`parse_command` fall back to its
    ordinary "target is the following token" handling for the glued-target
    case (``fd_target is None`` in the registry entry) exactly as it already
    does for a space-separated redirect.

    Returns:
        tuple[str, list[tuple[str | None, str, str | None]]]: The rewritten
        text, and a registry of ``(io_number, base_operator, fd_target)`` —
        indexed by the placeholder's embedded integer — for
        :func:`parse_command` to decode back into a :class:`Redirection`.
    """
    registry: list[tuple[str | None, str, str | None]] = []

    def replace(match: re.Match[str]) -> str:
        io_number, base_op, fd_target = match.group(1), match.group(2), match.group(3)
        if io_number is None and fd_target is None:
            return match.group(0)
        registry.append((io_number, base_op, fd_target))
        return f" \x00RDR{len(registry) - 1}\x00 "

    out: list[str] = []
    cursor = 0
    for qm in _QUOTE_SPAN_RE.finditer(text):
        out.append(_STREAM_REDIR_RE.sub(replace, text[cursor : qm.start()]))
        out.append(qm.group())
        cursor = qm.end()
    out.append(_STREAM_REDIR_RE.sub(replace, text[cursor:]))
    return "".join(out), registry


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
