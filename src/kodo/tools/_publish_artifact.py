"""``publish_artifact`` tool — writes a new artifact to the workspace."""

from __future__ import annotations

import json
import logging

from kodo.workspace import ArtifactType, Concern, Verdict

from ._tool import Tool

__all__ = ["PublishArtifactTool"]

_log = logging.getLogger(__name__)


class PublishArtifactTool(Tool):
    """Publish an artifact authored by the running agent.

    Appends the new artifact ID to the context's ``published_ids``.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        try:
            artifact_type = ArtifactType(str(tool_input["type"]))
        except (KeyError, ValueError) as exc:
            return json.dumps({"error": f"Invalid artifact type: {exc}"})

        project_code = str(tool_input.get("project_code", ""))
        responsibility_code = str(tool_input.get("responsibility_code", ""))
        content = str(tool_input.get("content", ""))

        if not (project_code and responsibility_code and content):
            return json.dumps(
                {"error": "project_code, responsibility_code, and content are required"}
            )

        req_ids_raw = tool_input.get("requirement_ids")
        supersedes_raw = tool_input.get("supersedes")
        concerns_raw = tool_input.get("concerns")
        verdict_raw = tool_input.get("verdict")
        metadata_raw = tool_input.get("metadata")

        concern_objects: list[Concern] = []
        if isinstance(concerns_raw, list):
            for item in concerns_raw:
                if isinstance(item, dict):
                    fl = item.get("first_line")
                    ll = item.get("last_line")
                    ex = item.get("excerpt")
                    concern_objects.append(
                        Concern(
                            kind=str(item.get("kind", "")),
                            description=str(item.get("description", "")),
                            first_line=int(fl) if isinstance(fl, (int, float)) else None,
                            last_line=int(ll) if isinstance(ll, (int, float)) else None,
                            excerpt=str(ex) if ex is not None else None,
                        )
                    )

        try:
            artifact_id = await ctx.workspace.publish(
                artifact_type=artifact_type,
                author=ctx.agent_name,
                project_code=project_code,
                responsibility_code=responsibility_code,
                content=content,
                filename_hint=str(tool_input["filename_hint"])
                if "filename_hint" in tool_input
                else None,
                requirement_ids=[str(r) for r in req_ids_raw]
                if isinstance(req_ids_raw, list)
                else None,
                supersedes=[str(s) for s in supersedes_raw]
                if isinstance(supersedes_raw, list)
                else None,
                reviewed_artifact_id=str(tool_input["reviewed_artifact_id"])
                if "reviewed_artifact_id" in tool_input
                else None,
                verdict=Verdict(str(verdict_raw)) if verdict_raw else None,
                concerns=concern_objects if concern_objects else None,
                metadata={str(k): str(v) for k, v in metadata_raw.items()}
                if isinstance(metadata_raw, dict)
                else None,
                session_id=ctx.session_id,
            )
        except Exception as exc:
            _log.exception("publish_artifact failed for %s: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        ctx.published_ids.append(artifact_id)
        _log.info(
            "publish_artifact: %s published type=%s id=%s",
            ctx.agent_name,
            artifact_type.value,
            artifact_id[:8],
        )
        return json.dumps({"id": artifact_id})
