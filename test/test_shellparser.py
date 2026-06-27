"""Tests for the structural shell-command parser (``kodo.shellparser``)."""

from __future__ import annotations

from kodo.shellparser import parse_command


def test_simple_command() -> None:
    p = parse_command("echo hello world")
    assert p.executables == ("echo",)
    assert p.operators == ()
    assert p.segments[0].args == ("hello", "world")
    assert p.segments[0].redirections == ()


def test_pipeline_splits_on_pipe() -> None:
    p = parse_command("grep foo bar.txt | sort | uniq")
    assert p.executables == ("grep", "sort", "uniq")
    assert p.operators == ("|", "|")


def test_and_or_and_semicolon_operators() -> None:
    p = parse_command("a && b || c ; d")
    assert p.executables == ("a", "b", "c", "d")
    assert p.operators == ("&&", "||", ";")


def test_output_redirection_captured() -> None:
    p = parse_command("cat foo > out.txt")
    seg = p.segments[0]
    assert seg.executable == "cat"
    assert seg.args == ("foo",)
    assert [(r.operator, r.target) for r in seg.redirections] == [(">", "out.txt")]


def test_append_redirection_on_final_segment() -> None:
    p = parse_command("grep x f | sort >> result.txt")
    assert p.redirections[0].operator == ">>"
    assert p.redirections[0].target == "result.txt"


def test_quotes_are_stripped() -> None:
    p = parse_command('echo "x y" > "a b.txt"')
    assert p.segments[0].args == ("x y",)
    assert p.segments[0].redirections[0].target == "a b.txt"


def test_heredoc_operator_keeps_delimiter_as_target() -> None:
    p = parse_command("cat <<EOF")
    assert p.segments[0].redirections[0].operator == "<<"
    assert p.segments[0].redirections[0].target == "EOF"


def test_empty_command() -> None:
    p = parse_command("")
    assert p.segments == ()
    assert p.executables == ()


def test_unbalanced_quotes_do_not_raise() -> None:
    # Best-effort fallback: must return *something*, never raise.
    p = parse_command('echo "unterminated')
    assert p.executables[0] == "echo"
