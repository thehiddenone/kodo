"""``add_local_llm_quant`` tool — write one quant into the user's local-LLM catalog.

Dispatch handler for :data:`kodo.toolspecs.ADD_LOCAL_LLM_QUANT`. Every field is
validated by the engine's :class:`~kodo.tools.LocalCatalogLike` service, which
is also what checks the call's claims against the GGUF header — this wrapper
only turns its refusals into the error envelope.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["AddLocalLlmQuantTool"]


class AddLocalLlmQuantTool(Tool):
    """Add one GGUF quant as a user catalog entry file."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        try:
            result = await self.context.services.local_catalog().add_quant(tool_input)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
