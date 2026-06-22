"""``run_subagent`` tool — spawns a leaf sub-agent via the injected runner."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RunSubagentTool"]

_log = logging.getLogger(__name__)


class RunSubagentTool(Tool):
    """Run a leaf sub-agent and return the artifact IDs it published."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", ""))
        task_message = str(tool_input.get("task_message", ""))
        input_ids_raw = tool_input.get("input_artifact_ids", [])
        input_ids = [str(i) for i in input_ids_raw] if isinstance(input_ids_raw, list) else []

        caller = self.context.agent_name
        _log.info("run_subagent: caller=%s name=%s input_ids=%s", caller, name, input_ids)
        try:
            artifact_ids = await self.context.services.run_subagent(
                caller, name, task_message, input_ids
            )
        except PermissionError as exc:
            _log.warning("run_subagent denied: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"artifact_ids": artifact_ids})
