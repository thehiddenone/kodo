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

        _log.info("run_subagent: name=%s input_ids=%s", name, input_ids)
        artifact_ids = await self.context.services.run_subagent(name, task_message, input_ids)
        return json.dumps({"artifact_ids": artifact_ids})
