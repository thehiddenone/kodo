"""``run_command`` tool — runs a shell command inside the project root."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from ._tool import Tool

__all__ = ["RunCommandTool"]

_log = logging.getLogger(__name__)

_POSIX = os.name == "posix"
# After killing a timed-out command we still drain its pipes, but a wedged
# grandchild could keep them open forever. Bound the drain so the engine
# worker is always released even in the pathological case.
_DRAIN_TIMEOUT = 5.0


class RunCommandTool(Tool):
    """Run a shell command and return its exit code, stdout, and stderr."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        command = str(tool_input.get("command", ""))
        working_dir_raw = tool_input.get("working_dir")
        try:
            timeout = self.__resolve_timeout(tool_input.get("timeout"))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        try:
            cwd = (
                ctx.resolver.resolve(str(working_dir_raw))
                if working_dir_raw
                else ctx.resolver.default_cwd
            )
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})

        _log.info(
            "run_command from %s: %s (cwd=%s, timeout=%ss)",
            ctx.agent_name,
            command[:120],
            cwd,
            timeout,
        )
        # stdin is closed (DEVNULL) so a command that reads interactive input
        # gets immediate EOF instead of blocking forever on the server's stdin.
        # On POSIX the command runs in its OWN process group/session
        # (start_new_session) so a timeout can kill the whole tree — not just
        # the wrapping shell — which is what prevents a backgrounded grandchild
        # from holding the output pipes open and wedging the drain forever.
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=_POSIX,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            stdout, stderr = await self.__kill(process)
            note = f"Command timed out after {timeout:g}s and was killed."
            err_text = stderr.decode("utf-8", errors="replace")
            return json.dumps(
                {
                    "exit_code": None,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": f"{note}\n{err_text}" if err_text else note,
                }
            )
        return json.dumps(
            {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        )

    @staticmethod
    def __resolve_timeout(raw: object) -> float:
        """Validate the mandatory ``timeout`` (seconds) parameter."""
        if raw is None:
            raise ValueError("run_command requires a 'timeout' (seconds).")
        try:
            timeout = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(
                f"run_command 'timeout' must be a number of seconds, got {raw!r}."
            ) from None
        if timeout <= 0:
            raise ValueError("run_command 'timeout' must be greater than 0 seconds.")
        return timeout

    @classmethod
    async def __kill(cls, process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """Kill a timed-out process tree and drain whatever output it produced.

        The drain is bounded by ``_DRAIN_TIMEOUT``: if a surviving child keeps
        the pipes open, we give up draining and return empty output rather than
        blocking the single engine worker forever (the bug this guards against).
        """
        cls.__terminate(process)
        try:
            return await asyncio.wait_for(process.communicate(), timeout=_DRAIN_TIMEOUT)
        except (TimeoutError, Exception):  # noqa: BLE001 — best-effort drain
            return b"", b""

    @staticmethod
    def __terminate(process: asyncio.subprocess.Process) -> None:
        """Hard-kill the command. On POSIX this kills the whole process group
        (set up via ``start_new_session``) so grandchildren die too; elsewhere
        it kills the spawned process directly."""
        try:
            if _POSIX:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
