"""``list_local_llms`` tool — what the local-LLM catalog already serves.

Dispatch handler for :data:`kodo.toolspecs.LIST_LOCAL_LLMS`, a thin wrapper
over the engine's :class:`~kodo.tools.LocalCatalogLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ListLocalLlmsTool"]


class ListLocalLlmsTool(Tool):
    """List every served catalog family and the context knobs an entry may name."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        try:
            result = await self.context.services.local_catalog().list_catalog()
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
