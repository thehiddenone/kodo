"""``list_artifacts`` tool — filtered metadata listing of the index."""

from __future__ import annotations

import json

from kodo.workspace import ArtifactType

from ._tool import Tool

__all__ = ["ListArtifactsTool"]


class ListArtifactsTool(Tool):
    """List artifact metadata matching at least one of the given filters."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        artifact_id = tool_input.get("artifact_id")
        type_filter = tool_input.get("type")
        resp_code = tool_input.get("responsibility_code")
        req_id = tool_input.get("requirement_id")
        author = tool_input.get("author")
        state = tool_input.get("state")

        if not any([artifact_id, type_filter, resp_code, req_id, author, state]):
            return json.dumps({"error": "At least one filter is required."})

        entries = self.context.index.all_entries()

        if artifact_id:
            entries = [e for e in entries if e.artifact_id == artifact_id]
        if type_filter:
            try:
                t = ArtifactType(str(type_filter))
                entries = [e for e in entries if e.type == t]
            except ValueError:
                return json.dumps({"error": f"Unknown artifact type: {type_filter!r}"})
        if resp_code:
            entries = [e for e in entries if e.responsibility_code == resp_code]
        if req_id:
            entries = [e for e in entries if str(req_id) in e.requirement_ids]
        if author:
            entries = [e for e in entries if e.author == author]
        if state:
            entries = [e for e in entries if e.state == state]

        result = [
            {
                "artifact_id": e.artifact_id,
                "type": e.type.value,
                "responsibility_code": e.responsibility_code,
                "filename_hint": e.filename_hint,
                "state": e.state,
                "author": e.author,
            }
            for e in entries
        ]
        return json.dumps({"artifacts": result})
