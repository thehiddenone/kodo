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

import stat
from pathlib import Path

import pytest

from kodo.llms.llamacpp._llama_server import LlamaServer, LlamaServerConfig


def _make_fake_executable(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "fake-llama-server"
    path.write_text(f"#!/usr/bin/env python3\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _config(tmp_path: Path, executable: Path) -> LlamaServerConfig:
    return LlamaServerConfig(
        executable=executable,
        model_path=tmp_path / "model.gguf",
        kodo_dir=tmp_path / "kodo",
        model_name="fake-model",
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
