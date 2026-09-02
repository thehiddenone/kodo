"""Tests for the control-flow flattener (``kodo.shellparser.flatten_command``).

The invariant under test throughout: flattening must never make a command
*less* visible than the flat parse did. It may add commands (a loop body, an
`if` branch, a called function's body) and it may remove pseudo-commands that
were never programs (`for`, `do`, `done`), but a real command must never
vanish.
"""

from __future__ import annotations

import pytest

from kodo.shellparser import flatten_command, opaque_reason


def _commands(command: str) -> list[str]:
    """Every command the script may run, as ``"exe arg arg"`` strings."""
    out: list[str] = []
    for segment in flatten_command(command).segments:
        if not segment.executable:
            continue
        reason = opaque_reason(segment.executable)
        out.append("<opaque>" if reason else " ".join((segment.executable, *segment.args)))
    return out


def _contexts(command: str) -> list[frozenset[str]]:
    parsed = flatten_command(command)
    return [parsed.contexts[i] for i, segment in enumerate(parsed.segments) if segment.executable]


# ----------------------------------------------------------------------
# Branches, loops and case arms contribute their bodies
# ----------------------------------------------------------------------


def test_if_emits_every_branch() -> None:
    assert _commands("if [ -f x ]; then rm -rf src; else ls; fi") == [
        "[ -f x ]",
        "rm -rf src",
        "ls",
    ]


def test_elif_chain_emits_every_branch() -> None:
    assert _commands("if a; then b; elif c; then d; else e; fi") == ["a", "b", "c", "d", "e"]


def test_for_loop_emits_the_body_and_drops_the_word_list() -> None:
    # `for f in *.txt` is a word list, not a command — it used to surface as
    # a bogus `for f` pseudo-command while the body stayed invisible.
    assert _commands("for f in *.txt; do rm -f $f; done") == ["rm -f $f"]


def test_c_style_for_header_is_skipped_whole() -> None:
    # The `;` inside `((…))` must not be mistaken for the one ending the
    # header, or the header's fragments leak out as commands.
    assert _commands("for ((i=0;i<n;i++)); do rm -rf src; done") == ["rm -rf src"]


def test_while_condition_is_a_command_and_body_is_emitted() -> None:
    assert _commands("while read line; do echo $line; done") == ["read line", "echo $line"]


def test_until_loop_body_is_emitted() -> None:
    assert _commands("until ping -c1 host; do sleep 1; done") == ["ping -c1 host", "sleep 1"]


def test_case_emits_arms_and_drops_patterns() -> None:
    assert _commands("case $x in a) rm -rf src ;; b|c) ls ;; *) echo hi ;; esac") == [
        "rm -rf src",
        "ls",
        "echo hi",
    ]


def test_nested_control_flow() -> None:
    assert _commands("for f in a; do if [ -f $f ]; then rm -rf $f; fi; done") == [
        "[ -f $f ]",
        "rm -rf $f",
    ]


def test_conditional_expression_is_not_a_command() -> None:
    # `[[ … ]]` is a bash keyword, unlike `[`, which really is `test`.
    assert _commands("[[ -f x ]] && rm x") == ["rm x"]
    assert _commands("[ -f x ] && rm x") == ["[ -f x ]", "rm x"]


def test_subshell_and_brace_group_are_transparent() -> None:
    assert _commands("(cd /etc && cat passwd)") == ["cd /etc", "cat passwd"]
    assert _commands("{ ls; rm x; }") == ["ls", "rm x"]


# ----------------------------------------------------------------------
# Functions: recorded at the definition, inlined at the call
# ----------------------------------------------------------------------


def test_function_body_is_not_emitted_where_it_is_defined() -> None:
    # Defining a function runs nothing, so an uncalled one contributes no
    # commands at all — which is exactly what happens at runtime.
    assert _commands("f() { rm -rf src; }") == []


def test_function_body_is_inlined_at_the_call_site() -> None:
    assert _commands("f() { rm -rf src; }; f") == ["rm -rf src"]


def test_bash_function_keyword_form() -> None:
    assert _commands("function g { rm -rf src; }; g") == ["rm -rf src"]


def test_function_call_arguments_are_not_read_as_a_command() -> None:
    # `a b` are `$1`/`$2` inside the body, never a command of their own.
    assert _commands("f() { echo hi; }; f a b") == ["echo hi"]


def test_mutually_referring_functions_inline_through() -> None:
    assert _commands("f() { g; }; g() { rm -rf src; }; f") == ["rm -rf src"]


def test_self_recursive_function_becomes_opaque() -> None:
    assert _commands("f() { f; }; f") == ["<opaque>"]
    reasons = [
        opaque_reason(segment.executable)
        for segment in flatten_command("f() { f; }; f").segments
        if segment.executable
    ]
    assert reasons and "calls itself" in (reasons[0] or "")


def test_mutual_recursion_becomes_opaque() -> None:
    assert _commands("f() { g; }; g() { f; }; f") == ["<opaque>"]


def test_array_assignment_is_not_read_as_a_function_definition() -> None:
    # `files=()` has the shape of `name ()` but is an array assignment. Only
    # a `{`/`(` body *after* the parens makes a definition, and `files=` is
    # not a valid function name — so `rm -rf src` must still be seen. The
    # stray `(` riding along as an argument is a known cosmetic artifact of
    # array-assignment syntax; it changes nothing about what gets judged.
    assert _commands("files=(); rm -rf src")[-1] == "rm -rf src"
    assert _commands("files=(a b c); rm -rf src")[-1] == "rm -rf src"


# ----------------------------------------------------------------------
# Reserved words are only reserved in command position
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("grep done file.txt", ["grep done file.txt"]),
        ("echo if then fi", ["echo if then fi"]),
        ("ls for", ["ls for"]),
    ],
)
def test_keyword_as_an_argument_stays_an_argument(command: str, expected: list[str]) -> None:
    assert _commands(command) == expected


# ----------------------------------------------------------------------
# Structural context
# ----------------------------------------------------------------------


def test_loop_body_is_marked_as_running_in_a_loop() -> None:
    assert _contexts("for f in a; do rm $f; done") == [frozenset({"loop"})]


def test_branch_body_is_marked_conditional() -> None:
    assert _contexts("if a; then b; fi") == [
        frozenset({"conditional"}),
        frozenset({"conditional"}),
    ]


def test_backgrounded_command_is_marked() -> None:
    contexts = _contexts("npm run build & sleep 1")
    assert "background" in contexts[0]
    assert "background" not in contexts[1]


def test_inlined_function_body_inherits_the_call_site_context() -> None:
    # A helper called from inside a loop runs in that loop, and must say so.
    assert _contexts("f() { rm $x; }; for i in a b; do f; done") == [frozenset({"loop"})]


def test_nested_constructs_report_both_contexts() -> None:
    assert _contexts("for f in a; do if x; then rm $f; fi; done") == [
        frozenset({"loop", "conditional"}),
        frozenset({"loop", "conditional"}),
    ]


def test_contexts_align_one_to_one_with_segments() -> None:
    for command in [
        "ls",
        "for f in a; do rm $f; done",
        "f() { g; }; g() { ls; }; f",
        "if a; then b; else c; fi",
        "case $x in a) ls ;; esac",
    ]:
        parsed = flatten_command(command)
        assert len(parsed.contexts) == len(parsed.segments), command


# ----------------------------------------------------------------------
# The flat parse's own facts survive flattening
# ----------------------------------------------------------------------


def test_redirections_survive() -> None:
    parsed = flatten_command("if x; then cat a > b; fi")
    redirs = [r for s in parsed.segments for r in s.redirections]
    assert [(r.operator, r.target) for r in redirs] == [(">", "b")]


def test_heredoc_body_survives() -> None:
    parsed = flatten_command("cat <<EOF\nbody\nEOF")
    assert parsed.segments[0].redirections[0].heredoc_body == "body\n"


def test_pipes_and_operators_survive() -> None:
    parsed = flatten_command("if x; then cat a | grep b; fi")
    assert "|" in parsed.operators


def test_newline_separation_survives() -> None:
    assert _commands("ls\nrm -rf src") == ["ls", "rm -rf src"]


# ----------------------------------------------------------------------
# Never raises
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "if",
        "for",
        "done",
        "fi fi fi",
        "f() {",
        "f() { g; ",
        "case",
        "esac",
        "}}}",
        ")))",
        "{{{",
        "if [ -f 'unbalanced; then rm x; fi",
        "for ((;;)); do",
        "while; do; done; done; done",
        "$(((((",
        "<<<<<<",
        "a" * 5000,
        "; ; ; ;",
        "\n\n\n",
    ],
)
def test_malformed_input_never_raises(command: str) -> None:
    parsed = flatten_command(command)
    assert len(parsed.contexts) == len(parsed.segments)
