"""Tests for the structural shell-command parser (``kodo.shellparser``)."""

from __future__ import annotations

from kodo.shellparser import parse_command, parse_powershell_command


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


# ----------------------------------------------------------------------
# Bare subshell `(...)` / brace-group `{...;}` flattening (POSIX)
# ----------------------------------------------------------------------


def test_subshell_grouping_is_flattened() -> None:
    p = parse_command("(rm -rf x)")
    assert p.executables == ("rm",)
    assert p.segments[0].args == ("-rf", "x")


def test_subshell_with_operators_inside_splits_normally() -> None:
    p = parse_command("(cmd1 && cmd2)")
    assert p.executables == ("cmd1", "cmd2")
    assert p.operators == ("&&",)


def test_brace_group_is_flattened() -> None:
    p = parse_command("{ rm -rf /tmp/x; }")
    assert p.segments[0].executable == "rm"
    assert p.segments[0].args == ("-rf", "/tmp/x")


def test_grouping_merged_with_adjacent_operator_still_splits() -> None:
    # shlex merges runs of pure punctuation (`)|`, `&&(`) into one token;
    # the parser must still recover the real operator underneath.
    p = parse_command("(rm -rf x)|cat")
    assert p.executables == ("rm", "cat")
    assert p.operators == ("|",)
    p2 = parse_command("a&&(b||c)")
    assert p2.executables == ("a", "b", "c")
    assert p2.operators == ("&&", "||")


def test_grouping_does_not_touch_quoted_literal_parens() -> None:
    p = parse_command('grep "(error)" file.txt')
    assert p.segments[0].args == ("(error)", "file.txt")


def test_grouping_does_not_touch_brace_expansion_or_placeholder() -> None:
    p = parse_command("rm -rf /tmp/{a,b}")
    assert p.segments[0].args == ("-rf", "/tmp/{a,b}")
    p2 = parse_command("find . -exec rm {} ;")
    assert p2.segments[0].args == (".", "-exec", "rm", "{}")


def test_grouped_redirection_still_captured() -> None:
    p = parse_command("(cmd)>out.txt")
    assert p.segments[0].executable == "cmd"
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [(">", "out.txt")]


# ----------------------------------------------------------------------
# Bare subshell `(...)` / script-block `{...}` flattening (PowerShell)
# ----------------------------------------------------------------------


def test_powershell_paren_wrapper_is_flattened() -> None:
    p = parse_powershell_command("(Get-Content foo)")
    assert p.executables == ("Get-Content",)
    assert p.segments[0].args == ("foo",)


def test_powershell_call_operator_script_block_is_flattened() -> None:
    p = parse_powershell_command("& { Remove-Item C:\\x -Recurse }")
    assert p.executables == ("Remove-Item",)
    assert p.segments[0].args == ("C:\\x", "-Recurse")


def test_powershell_grouping_does_not_touch_quoted_literal_parens() -> None:
    p = parse_powershell_command('Write-Output "(hello)"')
    assert p.segments[0].args == ("(hello)",)


def test_powershell_grouped_redirection_still_captured() -> None:
    p = parse_powershell_command("(rm -rf x)>out.txt")
    assert p.segments[0].executable == "rm"
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [(">", "out.txt")]
