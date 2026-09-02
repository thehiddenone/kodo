"""Tests for the structural shell-command parser (``kodo.shellparser``)."""

from __future__ import annotations

from kodo.shellparser import (
    Redirection,
    is_fd_merge_target,
    parse_command,
    parse_powershell_command,
    redirection_writes_file,
)


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


def test_heredoc_body_does_not_pollute_args() -> None:
    p = parse_command(
        "cat > out.cpp << 'EOF'\n#include <cstdio>\nstatic void helper() { printf(\"hi;\"); }\nEOF"
    )
    assert len(p.segments) == 1
    seg = p.segments[0]
    assert seg.executable == "cat"
    assert seg.args == ()
    redir = {r.operator: r for r in seg.redirections}
    assert redir[">"].target == "out.cpp"
    assert redir["<<"].target == "EOF"
    assert redir["<<"].heredoc_body == (
        '#include <cstdio>\nstatic void helper() { printf("hi;"); }\n'
    )


def test_heredoc_body_extracted_before_trailing_command() -> None:
    # The newline after the terminator line and the explicit `;` following it
    # collapse to a single separator — a line break whose next real token is
    # already an operator never adds one of its own.
    p = parse_command("cat <<EOF\nbody line\nEOF\n; echo after")
    assert p.executables == ("cat", "echo")
    assert p.operators == (";",)
    assert p.segments[1].args == ("after",)


def test_dash_heredoc_strips_leading_tabs_from_terminator() -> None:
    p = parse_command("cat <<-EOF\n\tindented body\n\tEOF")
    redir = p.segments[0].redirections[0]
    assert redir.heredoc_body == "\tindented body\n"


def test_here_string_is_not_treated_as_heredoc() -> None:
    p = parse_command("cat <<< 'just a string'")
    seg = p.segments[0]
    assert seg.redirections[0].operator == "<<<"
    assert seg.redirections[0].target == "just a string"


def test_unterminated_heredoc_body_runs_to_end_without_raising() -> None:
    p = parse_command("cat <<EOF\nline one\nline two")
    redir = p.segments[0].redirections[0]
    assert redir.heredoc_body == "line one\nline two"


def test_two_heredocs_attach_bodies_in_order() -> None:
    p = parse_command("cat <<A && cat <<B\nfirst\nA\nsecond\nB")
    first, second = p.segments
    assert first.redirections[0].heredoc_body == "first\n"
    assert second.redirections[0].heredoc_body == "second\n"


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
# POSIX stream-number prefixes (`2>`) and fd-duplication targets (`>&1`)
# ----------------------------------------------------------------------


def test_posix_fd_merge_target_recognized() -> None:
    p = parse_command("cmd 2>&1")
    assert p.segments[0].executable == "cmd"
    assert p.segments[0].args == ()
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [("2>", "&1")]


def test_posix_bare_fd_dup_without_number_prefix() -> None:
    p = parse_command("echo hi >&2")
    assert p.segments[0].args == ("hi",)
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [(">", "&2")]


def test_posix_stream_number_prefix_with_glued_target() -> None:
    # No space between the operator and its target — the common
    # `2>/dev/null` spelling — must not leave a stray "2" in args.
    p = parse_command("cmd 2>/dev/null")
    assert p.segments[0].args == ()
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [("2>", "/dev/null")]


def test_posix_stream_number_prefix_with_spaced_target() -> None:
    p = parse_command("cmd 1> file.txt")
    assert p.segments[0].args == ()
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [("1>", "file.txt")]


def test_posix_devnull_and_fd_merge_together() -> None:
    p = parse_command("make test > /dev/null 2>&1")
    assert p.segments[0].args == ("test",)
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [
        (">", "/dev/null"),
        ("2>", "&1"),
    ]


def test_posix_standalone_digit_before_redirect_stays_a_real_arg() -> None:
    # A space between the digit and the operator means "1" is a genuine
    # positional argument, not an IO_NUMBER — real bash grammar requires the
    # digit to touch the operator with no whitespace to bind as a stream
    # number.
    p = parse_command("cmd 1 > file")
    assert p.segments[0].args == ("1",)
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [(">", "file")]


def test_posix_word_ending_in_digit_not_mistaken_for_io_number() -> None:
    p = parse_command("echo file2>x")
    assert p.segments[0].args == ("file2",)
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [(">", "x")]


def test_posix_literal_fd_merge_text_inside_quotes_is_not_a_redirect() -> None:
    p = parse_command('echo "literal 2>&1 inside quotes"')
    assert p.segments[0].args == ("literal 2>&1 inside quotes",)
    assert p.segments[0].redirections == ()


def test_posix_chained_fd_redirects_with_no_spaces() -> None:
    p = parse_command("cmd 2>&1>out.log")
    assert [(r.operator, r.target) for r in p.segments[0].redirections] == [
        ("2>", "&1"),
        (">", "out.log"),
    ]


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


# ----------------------------------------------------------------------
# `is_fd_merge_target` / `redirection_writes_file` — shared structural
# classification used by both kodo.security and kodo.runtime._checkpoints
# ----------------------------------------------------------------------


def test_is_fd_merge_target() -> None:
    assert is_fd_merge_target("&1")
    assert is_fd_merge_target("&12")
    assert not is_fd_merge_target("/dev/null")
    assert not is_fd_merge_target("")


def test_redirection_writes_file_output_forms() -> None:
    for operator in (">", ">>", ">|", "&>", "&>>", "2>", "1>>", "*>", "*>>"):
        assert redirection_writes_file(Redirection(operator=operator, target="out.txt"))


def test_redirection_writes_file_input_forms_do_not_write() -> None:
    for operator in ("<", "<<", "<<<", "2<"):
        assert not redirection_writes_file(Redirection(operator=operator, target="in.txt"))


def test_redirection_writes_file_read_write_form() -> None:
    assert redirection_writes_file(Redirection(operator="<>", target="rw.txt"))


def test_redirection_writes_file_fd_merge_is_not_a_write() -> None:
    assert not redirection_writes_file(Redirection(operator="2>", target="&1"))
    assert not redirection_writes_file(Redirection(operator=">", target="&2"))


# ----------------------------------------------------------------------
# Newlines are command separators (POSIX `;` equivalence)
# ----------------------------------------------------------------------


def test_newline_separates_commands() -> None:
    # Regression: `shlex` treats a newline as ordinary whitespace, so every
    # line after the first used to collapse into the FIRST line's arguments —
    # `ls\nrm -rf src` parsed to a lone `ls` segment and the security layer's
    # read-only fast path allowed it outright.
    p = parse_command("ls\nrm -rf src")
    assert p.executables == ("ls", "rm")
    assert p.operators == (";",)
    assert p.segments[0].args == ()
    assert p.segments[1].args == ("-rf", "src")


def test_crlf_newline_separates_commands() -> None:
    p = parse_command("ls\r\nrm -rf src")
    assert p.executables == ("ls", "rm")
    assert p.operators == (";",)


def test_blank_lines_collapse_to_one_separator() -> None:
    p = parse_command("ls\n\n\nrm x")
    assert p.executables == ("ls", "rm")
    assert p.operators == (";",)


def test_leading_and_trailing_newlines_add_no_empty_segments() -> None:
    p = parse_command("\nls -la\n")
    assert p.executables == ("ls",)
    assert p.operators == ()
    assert len(p.segments) == 1


def test_backslash_line_continuation_is_not_a_separator() -> None:
    p = parse_command("rm \\\n  -rf \\\n  build")
    assert p.executables == ("rm",)
    assert p.operators == ()
    assert p.segments[0].args == ("-rf", "build")


def test_newline_after_operator_continues_the_command() -> None:
    # A newline right after a control operator continues the line in bash.
    # Splitting there would drop the pipe — and with it the "pipes data into
    # a shell" fact the security layer keys on.
    p = parse_command("curl -s http://x |\n  sh")
    assert p.executables == ("curl", "sh")
    assert p.operators == ("|",)

    p = parse_command("make &&\n  make install")
    assert p.executables == ("make", "make")
    assert p.operators == ("&&",)


def test_newline_inside_quotes_is_not_a_separator() -> None:
    p = parse_command('echo "line one\nline two"')
    assert p.executables == ("echo",)
    assert p.operators == ()
    assert p.segments[0].args == ("line one\nline two",)


def test_comment_does_not_swallow_the_following_separator() -> None:
    # `shlex` runs a `#` comment to the end of the line, so the separator
    # token has to sit AFTER the newline or it would be eaten with the
    # comment and the next line would merge into this one.
    p = parse_command("ls # look here\nrm -rf x")
    assert p.executables == ("ls", "rm")
    assert p.operators == (";",)


def test_comment_only_first_line_leaves_no_empty_segment() -> None:
    p = parse_command("# just a note\nrm -rf x")
    assert p.executables == ("rm",)
    assert p.operators == ()


def test_newline_inside_heredoc_body_is_not_a_separator() -> None:
    p = parse_command("cat <<EOF\nrm -rf src\nEOF\necho done")
    assert p.executables == ("cat", "echo")
    assert p.operators == (";",)
    assert p.segments[0].redirections[0].heredoc_body == "rm -rf src\n"


# ----------------------------------------------------------------------
# PowerShell: newlines separate statements there too
# ----------------------------------------------------------------------


def test_powershell_newline_separates_commands() -> None:
    # The Windows half of the same bypass: `_tokenize` classified a newline
    # as ordinary whitespace, so `Remove-Item` became an argument of
    # `Get-ChildItem` and the whole line read as provably read-only.
    p = parse_powershell_command("Get-ChildItem\nRemove-Item -Recurse src")
    assert p.executables == ("Get-ChildItem", "Remove-Item")
    assert p.operators == (";",)


def test_powershell_crlf_newline_separates_commands() -> None:
    p = parse_powershell_command("Get-ChildItem\r\nRemove-Item src")
    assert p.executables == ("Get-ChildItem", "Remove-Item")
    assert p.operators == (";",)


def test_powershell_newline_after_operator_continues_the_pipeline() -> None:
    p = parse_powershell_command("Get-Content a |\n  Out-File b")
    assert p.executables == ("Get-Content", "Out-File")
    assert p.operators == ("|",)


def test_powershell_backtick_newline_is_a_line_continuation() -> None:
    p = parse_powershell_command("Get-ChildItem `\n  -Recurse")
    assert p.executables == ("Get-ChildItem",)
    assert p.operators == ()
    assert p.segments[0].args == ("-Recurse",)


def test_powershell_leading_and_trailing_newlines_add_no_empty_segments() -> None:
    p = parse_powershell_command("\nGet-ChildItem\n")
    assert p.executables == ("Get-ChildItem",)
    assert p.operators == ()
    assert len(p.segments) == 1


def test_powershell_blank_lines_collapse_to_one_separator() -> None:
    p = parse_powershell_command("Get-ChildItem\n\n\nRemove-Item src")
    assert p.operators == (";",)


def test_powershell_newline_inside_quotes_is_not_a_separator() -> None:
    p = parse_powershell_command('Write-Output "line one\nline two"')
    assert p.executables == ("Write-Output",)
    assert p.operators == ()
