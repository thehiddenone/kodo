"""MCP stdio server exposing publish_artifact and read_artifact workspace tools."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from kodo.workspace import Artifact, ArtifactType, Concern, Verdict, Workspace


class WorkspaceTool:
    """MCP stdio server wrapping the virtual workspace.

    Exposes ``publish_artifact`` and ``read_artifact`` as MCP tools.
    Each method is a thin adapter that converts primitive MCP inputs into
    typed workspace calls and serializes results back to plain dicts.
    """

    __workspace: Workspace
    __app: FastMCP

    def __init__(self, project_root: str | Path) -> None:
        """Initialise the server and register workspace tools.

        Args:
            project_root (str | Path): Root directory of the Kodo project.
        """
        self.__workspace = Workspace(Path(project_root))
        self.__app = FastMCP("kodo-workspace")

        self.__app.tool(
            name="publish_artifact",
            description=(
                "Publish a new artifact into the workspace. Returns the assigned artifact ID. "
                "Supply 'supersedes' to retire existing artifacts in the same operation."
            ),
        )(self.__publish_artifact)

        self.__app.tool(
            name="read_artifact",
            description=(
                "Query live artifacts from the workspace. At least one filter must be supplied. "
                "All filters are ANDed. Set include_content=false to retrieve metadata only."
            ),
        )(self.__read_artifact)

    def run(self) -> None:
        """Start the MCP stdio server and block until the client disconnects."""
        self.__app.run(transport="stdio")

    async def __publish_artifact(
        self,
        type: str,
        author: str,
        project_code: str,
        responsibility_code: str,
        content: str,
        filename_hint: str | None = None,
        requirement_ids: list[str] | None = None,
        supersedes: list[str] | None = None,
        reviewed_artifact_id: str | None = None,
        verdict: str | None = None,
        concerns: list[dict[str, object]] | None = None,
        metadata: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Publish a new artifact and return its UUID.

        Args:
            type (str): Artifact type (narrative, architecture, requirements,
                functional-design, design-plan, tech-stack, code, test, feedback).
            author (str): Name of the publishing agent.
            project_code (str): PROJECTCODE assigned by Architect (e.g. ETRD).
            responsibility_code (str): RESPONSIBILITYCODE (e.g. AUTH). Use the
                project_code value for project-wide artifacts.
            content (str): Full text content of the artifact.
            filename_hint (str | None): Suggested leaf filename for materialization.
            requirement_ids (list[str] | None): Requirement IDs satisfied by this artifact.
            supersedes (list[str] | None): IDs of live artifacts to retire.
            reviewed_artifact_id (str | None): Required for feedback: ID of the
                artifact under review.
            verdict (str | None): Required for feedback: 'accepted' or 'rejected'.
            concerns (list[dict] | None): Required for feedback with verdict=rejected.
                Each entry: {kind, description, first_line?, last_line?, excerpt?}.
            metadata (dict[str, str] | None): Supplementary key-value context.

        Returns:
            str: UUID of the new artifact.
        """
        concern_objects = self.__parse_concerns(concerns)
        return await self.__workspace.publish(
            artifact_type=ArtifactType(type),
            author=author,
            project_code=project_code,
            responsibility_code=responsibility_code,
            content=content,
            filename_hint=filename_hint,
            requirement_ids=requirement_ids,
            supersedes=supersedes,
            reviewed_artifact_id=reviewed_artifact_id,
            verdict=Verdict(verdict) if verdict else None,
            concerns=concern_objects,
            metadata=metadata,
            session_id=session_id,
        )

    async def __read_artifact(
        self,
        artifact_id: str | None = None,
        author: str | None = None,
        project_code: str | None = None,
        responsibility_code: str | None = None,
        requirement_id: str | None = None,
        type: str | None = None,
        verdict: str | None = None,
        concern_kind: str | None = None,
        include_content: bool = True,
        version: str | None = None,
    ) -> list[dict[str, object]]:
        """Query live artifacts from the workspace.

        Args:
            artifact_id (str | None): Return the artifact with this exact ID.
            author (str | None): Filter by publishing agent name.
            project_code (str | None): Filter by PROJECTCODE.
            responsibility_code (str | None): Filter by RESPONSIBILITYCODE.
            requirement_id (str | None): Filter to artifacts containing this
                requirement ID in their requirement_ids list.
            type (str | None): Filter by artifact type.
            verdict (str | None): Filter feedback artifacts by verdict.
            concern_kind (str | None): Filter feedback artifacts containing at
                least one concern of this kind.
            include_content (bool): When False, omit content and concerns.
            version (str | None): Required when artifact_id is absent.
                'in_flight' or 'stable'.

        Returns:
            list[dict]: Matching live artifacts as plain dicts.
        """
        artifacts = await self.__workspace.read(
            artifact_id=artifact_id,
            author=author,
            project_code=project_code,
            responsibility_code=responsibility_code,
            requirement_id=requirement_id,
            artifact_type=ArtifactType(type) if type else None,
            verdict=Verdict(verdict) if verdict else None,
            concern_kind=concern_kind,
            include_content=include_content,
            version=version,
        )
        return [self.__serialize(a) for a in artifacts]

    @staticmethod
    def __parse_concerns(raw: list[dict[str, object]] | None) -> list[Concern]:
        if not raw:
            return []
        result: list[Concern] = []
        for item in raw:
            fl = item.get("first_line")
            ll = item.get("last_line")
            ex = item.get("excerpt")
            result.append(
                Concern(
                    kind=str(item["kind"]),
                    description=str(item["description"]),
                    first_line=int(fl) if isinstance(fl, (int, float)) else None,
                    last_line=int(ll) if isinstance(ll, (int, float)) else None,
                    excerpt=str(ex) if ex is not None else None,
                )
            )
        return result

    @staticmethod
    def __serialize(artifact: Artifact) -> dict[str, object]:
        return {
            "id": artifact.id,
            "type": artifact.type.value,
            "author": artifact.author,
            "project_code": artifact.project_code,
            "responsibility_code": artifact.responsibility_code,
            "created_at": artifact.created_at.isoformat(),
            "content": artifact.content,
            "requirement_ids": artifact.requirement_ids,
            "filename_hint": artifact.filename_hint,
            "supersedes": artifact.supersedes,
            "reviewed_artifact_id": artifact.reviewed_artifact_id,
            "verdict": artifact.verdict.value if artifact.verdict else None,
            "concerns": [
                {
                    "kind": c.kind,
                    "description": c.description,
                    "first_line": c.first_line,
                    "last_line": c.last_line,
                    "excerpt": c.excerpt,
                }
                for c in artifact.concerns
            ],
            "metadata": artifact.metadata,
            "session_id": artifact.session_id,
        }
