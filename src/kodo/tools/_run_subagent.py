"""``run_subagent`` tool — spawns a sub-agent via the injected runner.

Reached only in canonical form: the model calls ``run_subagent_<name>`` and
:func:`~kodo.tools.canonical_tool_call` folds that into ``{name, task_input,
max_rounds}`` before dispatch. ``max_rounds`` is forwarded untouched — the
engine decides whether it means anything (it does only when the target sub-agent
declares a critic) and applies its own default when absent.
"""

from __future__ import annotations

import json
import logging

from kodo.toolspecs import MAX_ROUNDS_KEY

from ._tool import Tool

__all__ = ["RunSubagentTool"]

_log = logging.getLogger(__name__)


class RunSubagentTool(Tool):
    """Run a sub-agent and return its structured result."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", ""))
        task_input_raw = tool_input.get("task_input", {})
        task_input = task_input_raw if isinstance(task_input_raw, dict) else {}
        max_rounds_raw = tool_input.get(MAX_ROUNDS_KEY)
        max_rounds = max_rounds_raw if isinstance(max_rounds_raw, int) else None

        caller = self.context.agent_name
        _log.info(
            "run_subagent: caller=%s name=%s max_rounds=%s",
            caller,
            name,
            max_rounds,
        )
        try:
            result = await self.context.services.run_subagent(caller, name, task_input, max_rounds)
        except PermissionError as exc:
            _log.warning("run_subagent denied: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(result)
