"""``run_author_critic_iteration`` tool — one Author/Critic round."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RunAuthorCriticIterationTool"]

_log = logging.getLogger(__name__)


class RunAuthorCriticIterationTool(Tool):
    """Run one Author/Critic iteration and return verdict + concerns."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        author_name = str(tool_input.get("author_name", ""))
        critic_name = str(tool_input.get("critic_name", ""))
        input_ids_raw = tool_input.get("input_artifact_ids", [])
        input_ids = [str(i) for i in input_ids_raw] if isinstance(input_ids_raw, list) else []
        previous_id = tool_input.get("previous_artifact_id")

        caller = self.context.agent_name
        _log.info(
            "run_author_critic_iteration: caller=%s author=%s critic=%s previous=%s",
            caller,
            author_name,
            critic_name,
            previous_id,
        )
        try:
            result = await self.context.services.run_author_critic_iteration(
                caller,
                author_name,
                critic_name,
                input_ids,
                str(previous_id) if previous_id is not None else None,
            )
        except PermissionError as exc:
            _log.warning("run_author_critic_iteration denied: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(result)
