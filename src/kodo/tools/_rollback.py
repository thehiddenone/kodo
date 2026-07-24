"""``rollback`` tool — invokes the injected rollback procedure."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["RollbackTool"]

_log = logging.getLogger(__name__)


class RollbackTool(Tool):
    """Roll one bound root's mirror back to ``target_sha`` and rebuild session state."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        root = str(tool_input.get("root", "")).strip()
        target_sha = str(tool_input.get("target_sha", "")).strip()
        if not root:
            return json.dumps({"error": "root is required"})
        if not target_sha:
            return json.dumps({"error": "target_sha is required"})
        try:
            resolved_root = self.context.resolver.resolve(root)
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})
        _log.info("rollback: root=%s target_sha=%s", resolved_root, target_sha[:12])
        try:
            await self.context.services.rollback(str(resolved_root), target_sha)
        except Exception as exc:
            _log.warning("rollback failed: %s", exc)
            return json.dumps({"error": str(exc)})
        return json.dumps({"status": "completed"})
