"""``return_result`` tool — a sub-agent's terminal "return" call.

Validates the ``result`` payload against the running sub-agent's
``output_schema`` (injected on the :class:`~kodo.tools.ToolContext`), stashes the
normalized result on the context for the engine to read back, and ends the run by
setting ``stop_requested`` (joining the same stop mechanism as
``escalate_blocker``).
"""

from __future__ import annotations

import json
import logging

from kodo.toolspecs import normalize_output

from ._tool import Tool

__all__ = ["ReturnResultTool"]

_log = logging.getLogger(__name__)


class ReturnResultTool(Tool):
    """Capture a sub-agent's result, validate it, and end the run."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        raw = tool_input.get("result")
        schema = self.context.output_schema
        if schema is None:
            # No spec for this agent (entry agents). Record the raw payload and
            # stop; this path should not occur for schema-bearing sub-agents.
            _log.warning(
                "return_result called by %s, which has no output schema",
                self.context.agent_name,
            )
            normalized: dict[str, object] = raw if isinstance(raw, dict) else {"result": str(raw)}
            compliant = isinstance(raw, dict)
        else:
            normalized, compliant = normalize_output(schema, raw)

        self.context.returned_output = normalized
        self.context.stop_requested = True
        _log.info(
            "return_result: agent=%s compliant=%s keys=%s",
            self.context.agent_name,
            compliant,
            sorted(normalized.keys()),
        )
        return json.dumps({"status": "received" if compliant else "received_with_repair"})
