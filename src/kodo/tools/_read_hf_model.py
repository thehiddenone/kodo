"""``read_hf_model`` tool — one Hugging Face repo's card, license and GGUF files.

Dispatch handler for :data:`kodo.toolspecs.READ_HF_MODEL`, a thin wrapper over
the engine's :class:`~kodo.tools.LocalCatalogLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ReadHfModelTool"]


class ReadHfModelTool(Tool):
    """Read a repo's card metadata, model card and sorted GGUF file list."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        repo_id = tool_input.get("repo_id")
        if not isinstance(repo_id, str) or not repo_id.strip():
            return json.dumps({"error": "read_hf_model requires a non-empty 'repo_id'."})
        try:
            result = await self.context.services.local_catalog().model_info(repo_id.strip())
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
