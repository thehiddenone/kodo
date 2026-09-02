"""Reduce a shell script to the primitive commands it may execute.

Where :func:`kodo.shellparser.parse_command` splits a command line on a fixed
separator set, :func:`flatten_command` walks the control-flow structure —
``if``/``for``/``while``/``until``/``case``, function definitions and their
call sites, ``[[ … ]]`` conditionals, grouping — and emits the set of simple
commands that *may* run, as an ordinary :class:`~kodo.shellparser.
ParsedCommand`. Callers judge those commands exactly as they always have; the
only thing that changes is which commands they get to see.

Three properties make this safe to judge on:

- **It over-approximates.** Both branches of an ``if`` are emitted, so is a
  loop body that might run zero times. That yields extra commands to judge,
  never a missing one — the correct bias for a permission gate.
- **It never silently drops a construct it cannot reduce.** A function that
  recurses, or nests past :data:`_MAX_DEPTH`, becomes an *opaque* segment
  (:func:`opaque_reason`) that a caller must treat as unanalyzable rather
  than as "nothing here". Anything else this walker does not recognize keeps
  its existing behavior: the tokens stay ordinary words and reach the caller
  as an ordinary (fail-closed) command.
- **It reports structure it cannot express as commands.** ``ParsedCommand.
  contexts`` carries, per segment, the constructs it sits inside — ``loop``,
  ``conditional``, ``background`` — because some risk is compositional
  (a loop runs its body N times over values that were never resolvable) and
  is invisible in a flat list of commands.

Deliberately bounded, in the spirit of the rest of this package: this is a
keyword-driven linear walk, not a bash grammar. It is judgement-free like
every other module here — "opaque" is a *parse* fact ("this reduces to no
command list"), not a security verdict.
"""

from __future__ import annotations

import re

from ._parser import (
    _SEGMENT_SEPARATORS,
    ParsedCommand,
    build_parsed,
    prepare_tokens,
)

__all__ = ["OPAQUE_PREFIX", "flatten_command", "opaque_reason"]

# How deep function-call inlining goes before giving up and emitting an
# opaque segment. Real scripts nest a helper or two; anything deeper is not
# worth chasing when the fail-closed answer is available.
_MAX_DEPTH = 3

# Marks a segment that reduces to no command list at all. It rides in the
# segment's *executable* slot — the same opaque-token technique `_parser`
# already uses for redirects and newlines — so no dataclass anywhere needs a
# new field and every existing positional alignment survives untouched.
OPAQUE_PREFIX = "\x00opaque\x00"

# Reserved words that open a block, and the context they put their body in.
# `while`/`until`/`if` deliberately do NOT skip their condition: a condition
# is itself a command (`while read line; do …`) and must be judged.
_BLOCK_OPEN = {
    "if": "conditional",
    "case": "conditional",
    "while": "loop",
    "until": "loop",
    "for": "loop",
    "select": "loop",
}
_BLOCK_CLOSE = frozenset({"fi", "esac", "done"})

# `for`/`select` headers are a word list, not commands — skipped wholesale.
_HEADER_SKIP = frozenset({"for", "select"})

# Grouping. `(`/`)` and `{`/`}` don't change what runs inside; they only bound
# it, which is exactly why the flattener keeps them where `parse_command`
# drops them (a function body's extent, a `case` pattern's end).
_GROUP_OPEN = frozenset({"(", "{"})
_GROUP_CLOSE = frozenset({")", "}"})

# Reserved words that are pure punctuation between commands.
_INERT = frozenset({"then", "else", "elif", "do", "!", "time", "coproc"})

# `case` arm terminators. `shlex` emits `;;` as one punctuation token.
_CASE_TERMINATORS = frozenset({";;", ";&", ";;&"})

# `[[ … ]]` is a bash conditional *expression*, not a command — unlike `[`,
# which really is `test` and is already on the read-only allow-list.
_COND_OPEN = "[["
_COND_CLOSE = "]]"

# A plausible function name, for the `name () { … }` definition form. Keeps
# an empty array assignment (`files=()`) from being read as a definition.
_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")

_MATCHING_CLOSE = {"{": "}", "(": ")"}


def opaque_reason(executable: str) -> str | None:
    """The reason *executable* marks an unanalyzable construct, else ``None``.

    Callers must check this before treating a segment as an ordinary command
    — an opaque segment has no executable, no arguments and no shape, and
    exists precisely to say "something runs here that could not be reduced".
    """
    if executable.startswith(OPAQUE_PREFIX):
        return executable[len(OPAQUE_PREFIX) :]
    return None


def flatten_command(command: str) -> ParsedCommand:
    """Flatten *command* to the primitive commands it may execute.

    Args:
        command: A shell script — one line or many, with arbitrary control
            flow, function definitions, grouping and redirections.

    Returns:
        ParsedCommand: The same structural view :func:`parse_command`
        returns, but with segments drawn from every branch and body rather
        than from a flat separator split, and with ``contexts`` filled in.
        Never raises; anything unrecognized degrades to ordinary words.
    """
    tokens, stream_redirs, bodies = prepare_tokens(command)
    if not tokens:
        return ParsedCommand(raw=command)
    functions, spans = _collect_functions(tokens)
    walker = _Walker(functions)
    walker.emit(_without_spans(tokens, spans), frozenset(), 0)
    return build_parsed(command, walker.out, stream_redirs, bodies, walker.contexts)


class _Walker:
    """Emits the flattened token stream, tracking per-segment context.

    Output is a plain token list in exactly the shape
    :func:`kodo.shellparser._parser.build_parsed` already consumes, so
    segment building, redirection decoding and here-doc attachment are all
    shared verbatim with :func:`parse_command` rather than reimplemented.
    """

    def __init__(self, functions: dict[str, list[str]]) -> None:
        self.functions = functions
        self.out: list[str] = []
        self.contexts: list[frozenset[str]] = [frozenset()]
        self.has_words = False

    def _boundary(self) -> None:
        """End the current segment if it has anything in it."""
        if self.has_words:
            self._sep(";")

    def _sep(self, operator: str) -> None:
        if not self.out:
            # Nothing to the left to separate — a leading separator only
            # manufactures an empty segment. Reached whenever a construct
            # that emits no commands opens the script: a function definition
            # (cut out by `_without_spans`, which leaves a `;` behind), a
            # `for` header, a blank first line.
            return
        if operator == "&":
            # `&` backgrounds the command to its LEFT — the slot just closing.
            self.contexts[-1] = self.contexts[-1] | {"background"}
        self.out.append(operator)
        self.contexts.append(frozenset())
        self.has_words = False

    def _word(self, token: str, context: frozenset[str]) -> None:
        self.out.append(token)
        self.contexts[-1] = self.contexts[-1] | context
        self.has_words = True

    def _opaque(self, reason: str, context: frozenset[str]) -> None:
        self._boundary()
        self._word(f"{OPAQUE_PREFIX}{reason}", context)
        self._sep(";")

    def emit(
        self,
        tokens: list[str],
        inlining: frozenset[str],
        depth: int,
        inherited: frozenset[str] = frozenset(),
    ) -> None:
        """Walk *tokens*, appending the commands they may run.

        *inlining* is the set of function names currently being expanded
        further up the stack (so direct or mutual recursion is caught rather
        than chased), and *depth* counts those expansions. *inherited* is the
        context of the call site, carried into the body so a function called
        from inside a loop reports its commands as running in one.
        """
        stack: list[str] = []
        pattern_skip = False
        i = 0
        n = len(tokens)

        while i < n:
            token = tokens[i]
            context = inherited | frozenset(entry for entry in stack if entry != "group")

            # Inside a `case`, an arm's pattern (`a|b)`) is data, not a command.
            if pattern_skip:
                if token == ")":
                    pattern_skip = False
                    i += 1
                    continue
                if token != "esac":
                    i += 1
                    continue
                pattern_skip = False

            if token in _SEGMENT_SEPARATORS:
                self._sep(token)
                i += 1
                continue

            if token in _CASE_TERMINATORS:
                self._boundary()
                pattern_skip = True
                i += 1
                continue

            # A closer is checked before the command-position gate: in
            # `(cd x && cat y)` the `)` is glued to the last command's
            # arguments and would otherwise be emitted as one of them. An
            # unquoted bare `)`/`}` is always grouping — quoted content and
            # the `{}`/`/tmp/{a,b}` forms are single tokens that never look
            # like this.
            if token in _GROUP_CLOSE:
                self._boundary()
                if stack:
                    stack.pop()
                i += 1
                continue

            # Reserved words only act as reserved in command position; a
            # trailing `done` in `grep done file` is an ordinary argument.
            if not self.has_words:
                consumed = self._reserved(tokens, i, token, stack, context, inlining, depth)
                if consumed is not None:
                    i, pattern_skip_now = consumed
                    pattern_skip = pattern_skip or pattern_skip_now
                    continue

            self._word(token, context)
            i += 1

    def _reserved(
        self,
        tokens: list[str],
        i: int,
        token: str,
        stack: list[str],
        context: frozenset[str],
        inlining: frozenset[str],
        depth: int,
    ) -> tuple[int, bool] | None:
        """Handle one reserved word / call site at command position.

        Returns:
            tuple[int, bool] | None: the next index and whether a `case`
            pattern skip starts here, or ``None`` when *token* is an
            ordinary word the caller should emit itself.
        """
        if token == _COND_OPEN:
            # `[[ … ]]` evaluates an expression; nothing in it is a command.
            self._boundary()
            return _skip_to(tokens, i + 1, {_COND_CLOSE}), False

        if token in _BLOCK_OPEN:
            self._boundary()
            stack.append(_BLOCK_OPEN[token])
            if token in _HEADER_SKIP:
                # `for f in *.txt; do …` — the word list is data. Stop at the
                # `do`/`;` that ends the header, not at one nested inside a
                # C-style `for ((i=0; i<n; i++))` header.
                return _skip_to(tokens, i + 1, {"do", ";"}, keep=True), False
            if token == "case":
                return _skip_to(tokens, i + 1, {"in"}), True
            return i + 1, False

        if token in _BLOCK_CLOSE:
            self._boundary()
            if stack:
                stack.pop()
            return i + 1, False

        if token in _GROUP_OPEN:
            self._boundary()
            stack.append("group")
            return i + 1, False

        if token in _INERT:
            self._boundary()
            return i + 1, False

        if token in self.functions:
            self._boundary()
            if token in inlining:
                self._opaque(f"'{token}' is a shell function that calls itself", context)
            elif depth >= _MAX_DEPTH:
                self._opaque(f"'{token}' nests shell functions too deeply to analyze", context)
            else:
                self.emit(self.functions[token], inlining | {token}, depth + 1, context)
            # The call's own arguments become `$1`…`$n` inside the body; they
            # are never a command of their own, so they are skipped rather
            # than left to be read as one.
            return _skip_to(tokens, i + 1, _SEGMENT_SEPARATORS, keep=True), False

        return None


def _skip_to(
    tokens: list[str], start: int, targets: frozenset[str] | set[str], *, keep: bool = False
) -> int:
    """Index just past the first *targets* token at paren depth zero.

    Depth tracking keeps a `for ((i=0; i<n; i++))` header's inner `;` from
    being mistaken for the one that ends the header. *keep* returns the index
    *of* the terminator instead of past it, for a terminator the caller still
    wants to process (a separator that must close the segment).
    """
    depth = 0
    i = start
    while i < len(tokens):
        token = tokens[i]
        if token in _GROUP_OPEN:
            depth += 1
        elif token in _GROUP_CLOSE:
            depth -= 1
            if depth < 0:
                return i  # Unbalanced: hand the closer back to the caller.
        elif depth == 0 and token in targets:
            return i if keep else i + 1
        i += 1
    return i


def _collect_functions(tokens: list[str]) -> tuple[dict[str, list[str]], list[tuple[int, int]]]:
    """Find every function definition: its body tokens, and its full extent.

    Handles both POSIX ``name () { … }`` and bash ``function name { … }``,
    with either a brace group or a subshell as the body. A definition's body
    is deliberately **not** emitted where it is defined — defining a function
    runs nothing — and is instead spliced in at each call site
    (:meth:`_Walker._reserved`). A function that is never called therefore
    contributes no commands at all, which is exactly what happens at runtime.

    Returns:
        tuple: name → body tokens, and the ``(start, end)`` token spans to
        cut out of the stream before walking it.
    """
    functions: dict[str, list[str]] = {}
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(tokens)
    while i < n:
        name: str | None = None
        body_at = -1
        if tokens[i] == "function" and i + 1 < n:
            name = tokens[i + 1]
            body_at = i + 2
            if tokens[body_at : body_at + 2] == ["(", ")"]:
                body_at += 2
        elif tokens[i : i + 3][1:] == ["(", ")"] and _FUNCTION_NAME_RE.match(tokens[i]):
            name = tokens[i]
            body_at = i + 3
        if (
            name is None
            or not _FUNCTION_NAME_RE.match(name)
            or body_at >= n
            or tokens[body_at] not in _MATCHING_CLOSE
        ):
            i += 1
            continue
        end = _matching_close(tokens, body_at)
        if end is None:
            i += 1
            continue
        functions[name] = tokens[body_at + 1 : end]
        spans.append((i, end + 1))
        i = end + 1
    return functions, spans


def _matching_close(tokens: list[str], opener: int) -> int | None:
    """Index of the token closing the group opened at *opener*, or ``None``."""
    want = _MATCHING_CLOSE[tokens[opener]]
    depth = 0
    for i in range(opener, len(tokens)):
        if tokens[i] == tokens[opener]:
            depth += 1
        elif tokens[i] == want:
            depth -= 1
            if depth == 0:
                return i
    return None


def _without_spans(tokens: list[str], spans: list[tuple[int, int]]) -> list[str]:
    """*tokens* with each span replaced by a `;`, so definitions leave a
    clean segment boundary behind instead of fusing their neighbours."""
    if not spans:
        return list(tokens)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.extend(tokens[cursor:start])
        out.append(";")
        cursor = end
    out.extend(tokens[cursor:])
    return out
