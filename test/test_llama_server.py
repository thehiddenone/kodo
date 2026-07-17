"""Behavioral tests for LlamaServer's startup-crash diagnostics.

:meth:`LlamaServer.start` redirects the child's stdout/stderr into a
per-launch startup-log file (see ``_llama_server.py``'s module docstring) so
that a process which exits before the health check passes can have its own
output folded into the raised ``RuntimeError`` — otherwise the user sees only
"exited before becoming ready" with no clue why (e.g. a bad CLI flag from a
hand-edited flavor). These tests exercise that behavior end-to-end against a
small fake "llama-server" executable rather than mocking any private method.
"""

from __future__ import annotations

import socket
import stat
import sys
from pathlib import Path
from typing import cast

import pytest

from kodo.llms.llamacpp._llama_server import LlamaServer, LlamaServerConfig


def _make_fake_executable(tmp_path: Path, script: str) -> Path:
    """A tiny fake "llama-server" the test can launch as a real child process.

    ``asyncio.create_subprocess_exec`` runs the path directly (no shell), so
    on Windows it must be something ``CreateProcess`` can launch on its own —
    a ``.py`` file with a POSIX shebang is not (``WinError 193``). A ``.bat``
    wrapper that re-invokes the current interpreter works on both platforms.
    """
    script_path = tmp_path / "fake-llama-server.py"
    script_path.write_text(script, encoding="utf-8")
    if sys.platform == "win32":
        path = tmp_path / "fake-llama-server.bat"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")
        return path
    path = tmp_path / "fake-llama-server"
    path.write_text(f"#!/usr/bin/env python3\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _free_port() -> int:
    """An OS-assigned free TCP port.

    ``LlamaServerConfig``'s default port (8042) is llama-server's
    machine-wide well-known port — a real instance may already be listening
    there on the developer's machine. The health check in ``__wait_ready``
    would then hit that real server and get a live 200, masking whatever the
    fake executable under test actually did. Binding to port 0 and reading
    back the assigned port keeps these tests isolated from any such
    already-running server.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return cast(int, s.getsockname()[1])


def _config(tmp_path: Path, executable: Path) -> LlamaServerConfig:
    return LlamaServerConfig(
        executable=executable,
        model_path=tmp_path / "model.gguf",
        kodo_dir=tmp_path / "kodo",
        model_name="fake-model",
        port=_free_port(),
    )


async def test_start_folds_captured_output_into_crash_message(tmp_path: Path) -> None:
    executable = _make_fake_executable(
        tmp_path,
        "import sys\n"
        "print('error: invalid argument: --bogus-flag', file=sys.stderr)\n"
        "sys.exit(1)\n",
    )
    server = LlamaServer(_config(tmp_path, executable), {}, flavor_id="vram-tight")

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    message = str(exc_info.value)
    assert "exited before becoming ready" in message
    assert "error: invalid argument: --bogus-flag" in message


async def test_start_crash_message_nudges_default_flavor_when_non_default_active(
    tmp_path: Path,
) -> None:
    executable = _make_fake_executable(tmp_path, "import sys\nsys.exit(1)\n")
    server = LlamaServer(_config(tmp_path, executable), {}, flavor_id="1m-context")

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    assert "default flavor" in str(exc_info.value)


async def test_start_crash_message_has_no_nudge_for_default_flavor(tmp_path: Path) -> None:
    executable = _make_fake_executable(tmp_path, "import sys\nsys.exit(1)\n")
    server = LlamaServer(_config(tmp_path, executable), {}, flavor_id="default")

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    assert "default flavor" not in str(exc_info.value)


async def test_start_crash_message_omits_output_section_when_nothing_written(
    tmp_path: Path,
) -> None:
    executable = _make_fake_executable(tmp_path, "import sys\nsys.exit(1)\n")
    server = LlamaServer(_config(tmp_path, executable), {})

    with pytest.raises(RuntimeError) as exc_info:
        await server.start()

    assert "Output from llama-server" not in str(exc_info.value)
