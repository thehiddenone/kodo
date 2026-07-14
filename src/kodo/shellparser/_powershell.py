"""Tokenize a PowerShell / Windows command line into the neutral structural view.

The Windows sibling of :mod:`._parser`: same output dataclasses
(:class:`~kodo.shellparser.ParsedCommand` etc.), same philosophy — lossy,
judgement-free, never raises. It understands the parts of PowerShell (and, by
overlap, ``cmd.exe``) syntax that matter for *structure*:

- separators: ``;`` ``|`` ``||`` ``&&`` ``&`` (a lone ``&`` at the start of a
  segment is PowerShell's call operator, not a separator, and is dropped);
- quoting: single quotes (literal, ``''`` escapes a quote) and double quotes
  (`` ` `` backtick escapes the next char, ``""`` escapes a quote);
- `` ` `` backtick escaping outside quotes;
- redirections, including stream-qualified forms: ``>``, ``>>``, ``<``,
  ``2>``, ``2>>``, ``*>``, ``*>>``, ``3>`` … and merges like ``2>&1`` (whose
  target stays the literal ``&1`` — callers recognise fd-merge targets).

Variable/subexpression syntax (``$env:HOME``, ``$(...)``, ``%VAR%``) is NOT
expanded — tokens keep it verbatim, exactly like the POSIX parser keeps
``$VAR``; callers that care detect the markers themselves.
"""

from __future__ import annotations

import re

from ._parser import ParsedCommand, Redirection, Segment

__all__ = ["parse_powershell_command"]

# Two-char separators first so they win over their one-char prefixes.
_TWO_CHAR_SEPARATORS = ("||", "&&")
_ONE_CHAR_SEPARATORS = ("|", ";", "&")

# A redirection operator, optionally stream-qualified (2>, *>>, 3>&1, …).
# Matched at the current scan position, outside quotes.
_REDIR_RE = re.compile(r"(?:[0-9*])?>>?|<")


def parse_powershell_command(command: str) -> ParsedCommand:
    """Parse *command* (PowerShell/cmd syntax) into a :class:`ParsedCommand`.

    Args:
        command: A Windows shell command line.

    Returns:
        ParsedCommand: The structural parse. Never raises; unparseable input
        degrades to a best-effort split.
    """
    raw = command
    tokens = _tokenize(command)

    segments: list[Segment] = []
    operators: list[str] = []
    words: list[str] = []
    redirs: list[Redirection] = []

    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        if kind == "sep":
            # A lone `&` where no words have accumulated is the call operator
            # (`& "C:\app.exe" args`), not a background/statement separator.
            if text == "&" and not words and not redirs:
                i += 1
                continue
            segments.append(_make_segment(words, redirs))
            operators.append(text)
            words, redirs = [], []
        elif kind == "redir":
            if i + 1 < len(tokens) and tokens[i + 1][0] == "word":
                redirs.append(Redirection(operator=text, target=tokens[i + 1][1]))
                i += 1
            else:
                redirs.append(Redirection(operator=text, target=""))
        else:
            words.append(text)
        i += 1

    segments.append(_make_segment(words, redirs))
    return ParsedCommand(raw=raw, segments=tuple(segments), operators=tuple(operators))


def _make_segment(words: list[str], redirs: list[Redirection]) -> Segment:
    executable = words[0] if words else ""
    args = tuple(words[1:]) if len(words) > 1 else ()
    return Segment(executable=executable, args=args, redirections=tuple(redirs))


def _tokenize(command: str) -> list[tuple[str, str]]:
    """Split *command* into ``(kind, text)`` tokens.

    ``kind`` is ``"word"``, ``"sep"``, or ``"redir"``. Quotes are consumed
    (their content joins the surrounding word); backtick escapes are resolved
    outside/inside double quotes. A redirection merge target (``&1``) is
    attached to the operator token's *following* word by the caller, so here
    ``2>&1`` yields ``("redir", "2>")`` + ``("word", "&1")``.
    """
    tokens: list[tuple[str, str]] = []
    word: list[str] = []
    i = 0
    n = len(command)

    def flush() -> None:
        if word:
            tokens.append(("word", "".join(word)))
            word.clear()

    while i < n:
        ch = command[i]

        if ch.isspace():
            flush()
            i += 1
            continue

        if ch == "'":
            # Single-quoted literal; '' inside is an escaped quote.
            i += 1
            while i < n:
                if command[i] == "'":
                    if i + 1 < n and command[i + 1] == "'":
                        word.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                word.append(command[i])
                i += 1
            continue

        if ch == '"':
            # Double-quoted; backtick escapes, "" is an escaped quote.
            i += 1
            while i < n:
                c = command[i]
                if c == "`" and i + 1 < n:
                    word.append(command[i + 1])
                    i += 2
                    continue
                if c == '"':
                    if i + 1 < n and command[i + 1] == '"':
                        word.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                word.append(c)
                i += 1
            continue

        if ch == "`" and i + 1 < n:
            word.append(command[i + 1])
            i += 2
            continue

        two = command[i : i + 2]
        if two in _TWO_CHAR_SEPARATORS:
            flush()
            tokens.append(("sep", two))
            i += 2
            continue

        redir = _REDIR_RE.match(command, i)
        # Only treat as a redirection when it starts a token or the qualifier
        # is a real stream digit/star (word-embedded `>` still splits — that
        # matches how the shells parse `a>b`).
        if redir is not None and (not word or redir.group()[0] in "><"):
            flush()
            op = redir.group()
            i = redir.end()
            # `2>&1`-style merge: attach `&1` as the target word.
            if i < n and command[i] == "&" and i + 1 < n and command[i + 1].isdigit():
                tokens.append(("redir", op))
                tokens.append(("word", command[i : i + 2]))
                i += 2
            else:
                tokens.append(("redir", op))
            continue

        if ch in _ONE_CHAR_SEPARATORS:
            flush()
            tokens.append(("sep", ch))
            i += 1
            continue

        if ch in "(){}":
            # Bare (unquoted) subshell/script-block grouping — `(cmd)`,
            # `& { cmd }` — is inert for structure the same way POSIX
            # `(...)`/`{...;}` is: it doesn't change what runs inside, so it
            # is dropped rather than tokenized, letting whatever separators
            # live inside do their normal job. Quoted occurrences never reach
            # here — the quote branches above already consumed them whole.
            # Full control-flow forms (`if (...) { ... }`, `foreach`, …)
            # still fail closed to ask, unchanged — only the common
            # simple-wrapper forms are flattened.
            flush()
            i += 1
            continue

        word.append(ch)
        i += 1

    flush()
    return tokens
