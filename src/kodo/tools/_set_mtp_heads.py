"""``set_mtp_heads`` tool — add standalone MTP draft heads to a catalog family.

Dispatch handler for :data:`kodo.toolspecs.SET_MTP_HEADS`, a thin wrapper over
the engine's :class:`~kodo.tools.LocalCatalogLike` service, which validates
each head against its GGUF header.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["SetMtpHeadsTool"]


class SetMtpHeadsTool(Tool):
    """Add a family's self-contained MTP heads to its ``mtp_sidecars.json``."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        base_llm = tool_input.get("base_llm")
        heads = tool_input.get("heads")
        if not isinstance(base_llm, str) or not base_llm.strip():
            return json.dumps({"error": "set_mtp_heads requires a non-empty 'base_llm'."})
        if not isinstance(heads, list) or not heads:
            return json.dumps({"error": "set_mtp_heads requires a non-empty 'heads' array."})
        try:
            result = await self.context.services.local_catalog().set_mtp_heads(
                base_llm.strip(), heads
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(result, ensure_ascii=False)
