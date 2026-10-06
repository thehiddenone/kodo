"""``read_gguf_header`` tool — one GGUF file's header metadata.

Dispatch handler for :data:`kodo.toolspecs.READ_GGUF_HEADER`, a thin wrapper
over the engine's :class:`~kodo.tools.LocalCatalogLike` service.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ReadGgufHeaderTool"]


class ReadGgufHeaderTool(Tool):
    """Read a GGUF file's key/value header over HTTP range requests."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        repo_id = tool_input.get("repo_id")
        filename = tool_input.get("filename")
        if not isinstance(repo_id, str) or not repo_id.strip():
            return json.dumps({"error": "read_gguf_header requires a non-empty 'repo_id'."})
        if not isinstance(filename, str) or not filename.strip():
            return json.dumps({"error": "read_gguf_header requires a non-empty 'filename'."})
        try:
            result = await self.context.services.local_catalog().gguf_header(
                repo_id.strip(), filename.strip()
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
