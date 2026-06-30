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
        path = str(tool_input.get("path", ""))
        instructions = str(tool_input.get("instructions", ""))
        for_revision = bool(tool_input.get("for_revision", False))
        input_paths_raw = tool_input.get("input_paths", {})
        input_paths = (
            {str(k): str(v) for k, v in input_paths_raw.items()}
            if isinstance(input_paths_raw, dict)
            else {}
        )

        if for_revision and not path:
            return json.dumps({"error": "`path` is required when `for_revision` is true."})

        caller = self.context.agent_name
        _log.info(
            "run_author_critic_iteration: caller=%s author=%s critic=%s path=%s for_revision=%s",
            caller,
            author_name,
            critic_name,
            path,
            for_revision,
        )
        try:
            result = await self.context.services.run_author_critic_iteration(
                caller,
                author_name,
                critic_name,
                path,
                input_paths,
                instructions,
                for_revision,
            )
        except PermissionError as exc:
            _log.warning("run_author_critic_iteration denied: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(result)
