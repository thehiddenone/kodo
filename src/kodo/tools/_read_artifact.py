"""``read_artifact`` tool — reads artifacts from the workspace."""

from __future__ import annotations

import json
import logging

from kodo.workspace import ArtifactType, Verdict

from ._serialize import serialize_artifact
from ._tool import Tool

__all__ = ["ReadArtifactTool"]

_log = logging.getLogger(__name__)


class ReadArtifactTool(Tool):
    """Read artifacts matching the given filters and return them as JSON."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        artifact_id = str(tool_input["artifact_id"]) if "artifact_id" in tool_input else None
        author = str(tool_input["author"]) if "author" in tool_input else None
        project_code = str(tool_input["project_code"]) if "project_code" in tool_input else None
        responsibility_code = (
            str(tool_input["responsibility_code"]) if "responsibility_code" in tool_input else None
        )
        requirement_id = (
            str(tool_input["requirement_id"]) if "requirement_id" in tool_input else None
        )
        type_filter = str(tool_input["type"]) if "type" in tool_input else None
        verdict_str = str(tool_input["verdict"]) if "verdict" in tool_input else None
        concern_kind = str(tool_input["concern_kind"]) if "concern_kind" in tool_input else None
        include_content = bool(tool_input.get("include_content", True))
        version = str(tool_input["version"]) if "version" in tool_input else None

        try:
            artifacts = await ctx.workspace.read(
                artifact_id=artifact_id,
                author=author,
                project_code=project_code,
                responsibility_code=responsibility_code,
                requirement_id=requirement_id,
                artifact_type=ArtifactType(type_filter) if type_filter else None,
                verdict=Verdict(verdict_str) if verdict_str else None,
                concern_kind=concern_kind,
                include_content=include_content,
                version=version,
            )
        except Exception as exc:
            _log.exception("read_artifact failed for %s: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        return json.dumps({"artifacts": [serialize_artifact(a) for a in artifacts]})
