"""Behavior tests for kodo.tools.shell._server.Shell.

Shell registers a run_command tool with FastMCP.
Tests cover the constructor (tool registration) and the command-execution
behavior by invoking the bound tool function directly.
"""

from __future__ import annotations

from pathlib import Path

from kodo.tools.shell._server import Shell

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(instance: Shell) -> object:
    return vars(instance)["_Shell__app"]


def _tool_fn(instance: Shell, tool_name: str) -> object:
    app = _app(instance)
    return app._tool_manager._tools[tool_name].fn


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_shell_can_be_created() -> None:
    """
    Given no arguments,
    when Shell is instantiated,
    then no exception is raised.
    """
    Shell()


def test_shell_registers_run_command_tool() -> None:
    """
    Given a Shell instance,
    when tools are listed,
    then 'run_command' is present.
    """
    shell = Shell()
    app = _app(shell)
    assert "run_command" in app._tool_manager._tools


def test_shell_registers_exactly_one_tool() -> None:
    """
    Given a Shell instance,
    when tools are counted,
    then exactly one tool is registered.
    """
    shell = Shell()
    app = _app(shell)
    assert len(app._tool_manager._tools) == 1


# ---------------------------------------------------------------------------
# run_command behavior
# ---------------------------------------------------------------------------


def test_run_command_returns_three_content_blocks() -> None:
    """
    Given a simple echo command,
    when run_command is invoked,
    then exactly three TextContent blocks are returned (exit_code, stdout, stderr).
    """
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="echo hello")
    assert len(result) == 3


def test_run_command_first_block_contains_exit_code() -> None:
    """
    Given a command that succeeds (exit 0),
    when run_command is invoked,
    then the first block contains 'exit_code: 0'.
    """
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="echo hello")
    assert "exit_code: 0" in result[0].text


def test_run_command_second_block_contains_stdout() -> None:
    """
    Given a command that prints to stdout,
    when run_command is invoked,
    then the second block starts with 'stdout:' and contains the output.
    """
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="echo hello_kodo")
    assert result[1].text.startswith("stdout:")
    assert "hello_kodo" in result[1].text


def test_run_command_third_block_contains_stderr() -> None:
    """
    Given any command,
    when run_command is invoked,
    then the third block starts with 'stderr:'.
    """
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="echo hello")
    assert result[2].text.startswith("stderr:")


def test_run_command_failing_command_returns_nonzero_exit_code() -> None:
    """
    Given a command that exits with a non-zero code,
    when run_command is invoked,
    then the exit_code block reflects the failure.
    """
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="exit 1")
    assert "exit_code: 0" not in result[0].text


def test_run_command_with_working_dir(tmp_path: Path) -> None:
    """
    Given a working_dir argument pointing to a temp directory,
    when run_command lists files,
    then the command executes in that directory without error.
    """
    (tmp_path / "canary.txt").write_text("x", encoding="utf-8")
    shell = Shell()
    fn = _tool_fn(shell, "run_command")
    result = fn(command="echo in_dir", working_dir=str(tmp_path))
    assert "exit_code: 0" in result[0].text
