"""The ``kodo-headless`` command line (doc/HEADLESS.md), run in-process.

``main`` is called directly. Runs here use a cloud model whose API key is not
in the environment, so they end at startup before anything is spawned; the
event loop ``main`` creates records signal handlers instead of installing
them, so the process-wide signal table is never touched.
"""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest

from kodo.headless import RunOutcome, main, vendor_credential_env_names

_STARTUP = RunOutcome.STARTUP_ERROR.exit_code


@pytest.fixture
def recorded_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[int, Callable[[], object]]:
    """Isolate HOME, drop the Anthropic keys, and record loop signal handlers."""
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in vendor_credential_env_names("anthropic"):
        monkeypatch.delenv(name, raising=False)
    installed: dict[int, Callable[[], object]] = {}

    def loop_factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        setattr(  # noqa: B010 - an instance override, not a constant attribute
            loop,
            "add_signal_handler",
            lambda signum, callback: installed.update({signum: callback}),
        )
        return loop

    real_run = asyncio.run

    def run(main_coro: Coroutine[object, object, int], **_kwargs: object) -> int:
        return real_run(main_coro, loop_factory=loop_factory)

    monkeypatch.setattr(asyncio, "run", run)
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))
    return installed


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _stdout_events(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_run_options_reach_the_run_and_its_exit_code_is_returned(
    sandbox: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--prompt",
            "fix the bug",
            "--model",
            "anthropic/claude-sonnet-5",
            "--agent",
            "kodo_custom",
            "--cwd",
            str(sandbox),
        ]
    )

    assert code == _STARTUP
    events = _stdout_events(capsys.readouterr().out)
    started = events[0]
    assert started["type"] == "run.start"
    assert (started["agent"], started["model"], started["cwd"]) == (
        "kodo_custom",
        "anthropic/claude-sonnet-5",
        str(sandbox.resolve()),
    )
    assert started["llama_url"] is None
    assert "ANTHROPIC_API_KEY" in str(events[-1]["error"])
    assert set(recorded_signals) == {signal.SIGINT, signal.SIGTERM}


def test_llama_url_loses_its_trailing_slash(
    sandbox: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--prompt",
            "go",
            "--model",
            "anthropic/claude-sonnet-5",
            "--llama-url",
            "http://host:9000/",
            "--cwd",
            str(sandbox),
        ]
    )

    assert code == _STARTUP
    assert _stdout_events(capsys.readouterr().out)[0]["llama_url"] == "http://host:9000"


def test_cwd_defaults_to_the_launch_directory_and_text_format_is_honoured(
    sandbox: Path,
    recorded_signals: dict[int, Callable[[], object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(sandbox)

    code = main(["--prompt", "go", "--model", "anthropic/claude-sonnet-5", "--format", "text"])

    assert code == _STARTUP
    out = capsys.readouterr().out
    assert f"cwd={sandbox.resolve()}" in out.splitlines()[0]
    assert "result: {" in out
    assert out.splitlines()[-1].startswith("KODO-RESULT outcome=startup_error")


def test_prompt_file_is_read(
    tmp_path: Path,
    sandbox: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing\n", encoding="utf-8")

    code = main(
        [
            "--prompt-file",
            str(prompt_file),
            "--model",
            "anthropic/claude-sonnet-5",
            "--cwd",
            str(sandbox),
        ]
    )

    assert code == _STARTUP
    assert _stdout_events(capsys.readouterr().out)[0]["type"] == "run.start"


def test_unreadable_prompt_file_is_a_startup_error(
    tmp_path: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--prompt-file", str(tmp_path / "missing.md"), "--model", "m"])

    assert code == _STARTUP
    captured = capsys.readouterr()
    assert "cannot read --prompt-file" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("from_file", [False, True])
def test_empty_prompt_is_a_startup_error(
    from_file: bool,
    tmp_path: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    if from_file:
        prompt_file = tmp_path / "blank.md"
        prompt_file.write_text("  \n\n", encoding="utf-8")
        args = ["--prompt-file", str(prompt_file)]
    else:
        args = ["--prompt", "  "]

    code = main([*args, "--model", "m"])

    assert code == _STARTUP
    captured = capsys.readouterr()
    assert "the prompt is empty" in captured.err
    assert captured.out == ""


def test_cwd_that_is_not_a_directory_is_a_startup_error(
    tmp_path: Path,
    recorded_signals: dict[int, Callable[[], object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--prompt", "go", "--model", "m", "--cwd", str(tmp_path / "nowhere")])

    assert code == _STARTUP
    captured = capsys.readouterr()
    assert "--cwd is not a directory" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--model", "m"],  # no prompt
        ["--prompt", "go"],  # no model
        ["--prompt", "go", "--prompt-file", "p.md", "--model", "m"],
        ["--prompt", "go", "--model", "m", "--llama-url", "http://h:1", "--llama-port", "2"],
        ["--prompt", "go", "--model", "m", "--format", "xml"],
    ],
)
def test_malformed_command_lines_are_refused_by_the_parser(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(argv)

    assert exited.value.code == 2
    assert "usage: kodo-headless" in capsys.readouterr().err
