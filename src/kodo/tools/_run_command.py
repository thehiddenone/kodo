"""``run_command`` tool — runs a shell command inside the project root."""

from __future__ import annotations

import asyncio
import json
import logging

from ._paths import resolve_within
from ._tool import Tool

__all__ = ["RunCommandTool"]

_log = logging.getLogger(__name__)


class RunCommandTool(Tool):
    """Run a shell command and return its exit code, stdout, and stderr."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        command = str(tool_input.get("command", ""))
        working_dir_raw = tool_input.get("working_dir")
        try:
            cwd = (
                resolve_within(ctx.workspace.project_root, str(working_dir_raw))
                if working_dir_raw
                else ctx.workspace.project_root
            )
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})

        _log.info("run_command from %s: %s (cwd=%s)", ctx.agent_name, command[:120], cwd)
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return json.dumps(
            {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        )
