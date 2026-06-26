"""``run_subagent`` tool — spawns a leaf sub-agent via the injected runner."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RunSubagentTool"]

_log = logging.getLogger(__name__)


class RunSubagentTool(Tool):
    """Run a leaf sub-agent and return its structured result."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", ""))
        task_input_raw = tool_input.get("task_input", {})
        task_input = task_input_raw if isinstance(task_input_raw, dict) else {}

        caller = self.context.agent_name
        _log.info("run_subagent: caller=%s name=%s", caller, name)
        try:
            result = await self.context.services.run_subagent(caller, name, task_input)
        except PermissionError as exc:
            _log.warning("run_subagent denied: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(result)
