"""Shared subprocess helper for the ``fd`` / ``rg`` search tools.

``find_files`` and ``find_text_in_files`` both shell out to a bundled
third-party CLI util under a resolved root directory. This module centralises
the one thing they share — launching that util with stdin closed and a bounded
timeout, killing the whole process tree on timeout (POSIX) — so each tool module
keeps to the input-handling/output-shaping logic that is actually its own.

It deliberately holds no tool dispatch: the per-tool ``Tool`` subclasses live in
their own ``_<tool_name>.py`` modules, matching the package convention.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

__all__ = ["UtilTimeout", "run_util"]

_POSIX = os.name == "posix"

# Searches are interactive-latency operations; a tree that has not finished in
# this many seconds is treated as wedged and killed (the agent gets an error).
_DEFAULT_TIMEOUT = 60.0
_DRAIN_TIMEOUT = 5.0


class UtilTimeout(Exception):
    """Raised when a search util exceeds :data:`_DEFAULT_TIMEOUT`."""


async def run_util(
    binary: str,
    args: list[str],
    cwd: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int | None, bytes, bytes]:
    """Run *binary* with *args* in *cwd* and capture its output.

    stdin is closed (``DEVNULL``) so the util never blocks on input. On POSIX the
    util runs in its own process group so a timeout kills the whole tree.

    Args:
        binary: Absolute path to the util binary.
        args: Argument vector (excluding the binary itself).
        cwd: Working directory to run under.
        timeout: Seconds before the util is killed.

    Returns:
        tuple[int | None, bytes, bytes]: ``(returncode, stdout, stderr)``.

    Raises:
        UtilTimeout: The util did not finish within *timeout*.
    """
    process = await asyncio.create_subprocess_exec(
        binary,
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=_POSIX,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        _terminate(process)
        # Best-effort drain of the killed tree; never let it block the raise.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.communicate(), timeout=_DRAIN_TIMEOUT)
        raise UtilTimeout(f"search timed out after {timeout:g}s") from None
    return process.returncode, stdout, stderr


def _terminate(process: asyncio.subprocess.Process) -> None:
    """Hard-kill the util's process tree (group on POSIX)."""
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
