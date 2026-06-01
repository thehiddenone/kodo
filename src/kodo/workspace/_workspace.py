"""Virtual artifact workspace."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from kodo.project._layout import ProjectLayout

from ._errors import ArtifactNotFoundError, WorkspaceValidationError
from ._models import Artifact, ArtifactType, Concern, Verdict

_PROJECT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
_RESPONSIBILITY_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
_REQUIREMENT_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}_[A-Z][A-Z0-9]{1,15}_[A-Z][A-Z0-9]{1,31}$")


class Workspace:
    """Virtual artifact store for a Kodo project.

    All agent-produced artifacts are published through this class. The
    workspace maintains an in-memory index of live artifacts backed by an
    on-disk JSON index and an append-only event log. Each artifact is
    persisted as a JSON file under ``.kodo/workspace/``. Specification
    artifacts are additionally materialized into ``src/`` and code/test
    artifacts into ``gen/``.

    All public methods are coroutines and safe for concurrent callers;
    an internal :class:`asyncio.Lock` serialises mutations.
    """

    __project_root: Path
    __workspace_dir: Path
    __retired_dir: Path
    __index_path: Path
    __events_path: Path
    __index: dict[str, Artifact]
    __lock: asyncio.Lock
    __loaded: bool

    def __init__(self, project_root: Path) -> None:
        """Initialise workspace paths for the given project root.

        No I/O is performed here. Disk initialisation is deferred to the
        first call to :meth:`publish` or :meth:`read`.

        Args:
            project_root (Path): Root directory of the Kodo project. The
                workspace lives at ``<project_root>/.kodo/workspace/``.
        """
        self.__project_root = project_root.resolve()
        self.__workspace_dir = ProjectLayout(self.__project_root).workspace_dir
        self.__retired_dir = self.__workspace_dir / ".retired"
        self.__index_path = self.__workspace_dir / "index.json"
        self.__events_path = self.__workspace_dir / "events.jsonl"
        self.__index = {}
        self.__lock = asyncio.Lock()
        self.__loaded = False

    @property
    def project_root(self) -> Path:
        """Root directory of the Kodo project."""
        return self.__project_root

    async def publish(
        self,
        artifact_type: ArtifactType,
        author: str,
        project_code: str,
        responsibility_code: str,
        content: str,
        filename_hint: str | None = None,
        requirement_ids: list[str] | None = None,
        supersedes: list[str] | None = None,
        reviewed_artifact_id: str | None = None,
        verdict: Verdict | None = None,
        concerns: list[Concern] | None = None,
        metadata: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Publish a new artifact and return its assigned UUID.

        If ``supersedes`` is supplied, every listed artifact is retired
        atomically with this publish: removed from the live index, moved to
        ``.kodo/workspace/.retired/``, and dematerialized from ``src/``
        or ``gen/`` before the new artifact is materialized.

        Args:
            artifact_type (ArtifactType): Type of the artifact.
            author (str): Name of the publishing agent.
            project_code (str): PROJECTCODE (e.g. ``ETRD``).
            responsibility_code (str): RESPONSIBILITYCODE (e.g. ``AUTH``).
                For project-wide artifacts, pass the project_code value.
            content (str): Full text content of the artifact.
            filename_hint (str | None): Suggested leaf filename used when
                materializing to ``src/`` or ``gen/``.
            requirement_ids (list[str] | None): Requirement IDs satisfied or
                related to this artifact.
            supersedes (list[str] | None): IDs of live artifacts to retire.
            reviewed_artifact_id (str | None): Required for feedback artifacts.
            verdict (Verdict | None): Required for feedback artifacts.
            concerns (list[Concern] | None): Required for feedback artifacts
                with ``verdict=REJECTED``.
            metadata (dict[str, str] | None): Supplementary key-value context.

        Returns:
            str: UUID assigned to the new artifact.

        Raises:
            WorkspaceValidationError: If the call violates any publish rule.
            ArtifactNotFoundError: If any ID in ``supersedes`` or
                ``reviewed_artifact_id`` is not a live artifact.
        """
        req_ids = requirement_ids or []
        sup_ids = supersedes or []
        concerns_list = concerns or []
        meta = metadata or {}

        async with self.__lock:
            await self.__ensure_loaded()

            self.__validate_publish(
                artifact_type=artifact_type,
                author=author,
                project_code=project_code,
                responsibility_code=responsibility_code,
                req_ids=req_ids,
                sup_ids=sup_ids,
                reviewed_artifact_id=reviewed_artifact_id,
                verdict=verdict,
                concerns_list=concerns_list,
            )

            artifact_id = str(uuid.uuid4())
            now = datetime.now(tz=UTC)

            artifact = Artifact(
                id=artifact_id,
                type=artifact_type,
                author=author,
                project_code=project_code,
                responsibility_code=responsibility_code,
                created_at=now,
                content=content,
                requirement_ids=req_ids,
                filename_hint=filename_hint,
                supersedes=sup_ids,
                reviewed_artifact_id=reviewed_artifact_id,
                verdict=verdict,
                concerns=concerns_list,
                metadata=meta,
                session_id=session_id,
            )

            # Pop superseded artifacts from the in-memory index first so the
            # state is consistent before any disk I/O begins.
            retired: list[Artifact] = []
            for sup_id in sup_ids:
                retired.append(self.__index.pop(sup_id))

            # Add the new artifact to the index (content stripped).
            self.__index[artifact_id] = self.__strip_content(artifact)

            # --- Disk I/O (all outside the critical section for index state,
            #     but still inside the lock to serialise file operations) ---

            # Move retired artifact files to .retired/.
            for ret in retired:
                src = self.__artifact_path(ret)
                dst = self.__retired_dir / f"{ret.id}.json"
                await asyncio.to_thread(self.__move_to_retired, src, dst)

            # Persist the new artifact JSON.
            await asyncio.to_thread(self.__write_artifact, artifact)

            # Atomically persist the updated index.
            await asyncio.to_thread(self.__save_index)

            # Append event log entries.
            await asyncio.to_thread(self.__append_event, self.__published_event(artifact, now))
            for ret in retired:
                await asyncio.to_thread(self.__append_event, self.__retired_event(ret))

        return artifact_id

    async def read(
        self,
        artifact_id: str | None = None,
        author: str | None = None,
        project_code: str | None = None,
        responsibility_code: str | None = None,
        requirement_id: str | None = None,
        artifact_type: ArtifactType | None = None,
        verdict: Verdict | None = None,
        concern_kind: str | None = None,
        include_content: bool = True,
        version: str | None = None,
    ) -> list[Artifact]:
        """Query live artifacts from the workspace.

        At least one filter must be supplied. All supplied filters are ANDed.
        Only live (non-retired) artifacts are returned.

        When ``include_content`` is ``False``, returned artifacts have
        ``content=None`` and ``concerns=[]``.  When ``concern_kind`` is
        supplied, artifact files must be read from disk regardless of
        ``include_content`` to inspect concern lists; the cost is bounded by
        the number of live feedback artifacts, which is typically small.

        Args:
            artifact_id (str | None): Return the single artifact with this ID.
            author (str | None): Filter by publishing agent name.
            project_code (str | None): Filter by PROJECTCODE.
            responsibility_code (str | None): Filter by RESPONSIBILITYCODE.
            requirement_id (str | None): Filter to artifacts whose
                ``requirement_ids`` list contains this value.
            artifact_type (ArtifactType | None): Filter by type.
            verdict (Verdict | None): Filter feedback artifacts by verdict.
            concern_kind (str | None): Filter feedback artifacts that contain
                at least one concern of this kind.
            include_content (bool): When ``False``, omit content and concerns.
            version (str | None): Required when ``artifact_id`` is absent.
                ``'in_flight'`` returns the in-progress workspace version;
                ``'stable'`` returns the last accepted (promoted) version.
                Must be ``None`` when ``artifact_id`` is supplied.

        Returns:
            list[Artifact]: Matching live artifacts.

        Raises:
            WorkspaceValidationError: If no filter is supplied, if
                ``version`` is absent on a filter-form call, or if
                ``version`` is supplied alongside ``artifact_id``.
        """
        active_filters: dict[str, object] = {}
        if artifact_id is not None:
            active_filters["artifact_id"] = artifact_id
        if author is not None:
            active_filters["author"] = author
        if project_code is not None:
            active_filters["project_code"] = project_code
        if responsibility_code is not None:
            active_filters["responsibility_code"] = responsibility_code
        if requirement_id is not None:
            active_filters["requirement_id"] = requirement_id
        if artifact_type is not None:
            active_filters["artifact_type"] = artifact_type
        if verdict is not None:
            active_filters["verdict"] = verdict
        if concern_kind is not None:
            active_filters["concern_kind"] = concern_kind

        if not active_filters:
            raise WorkspaceValidationError("At least one filter must be supplied to read().")

        if artifact_id is not None and version is not None:
            raise WorkspaceValidationError(
                "version must not be specified when artifact_id is supplied."
            )
        if artifact_id is None and version is None:
            raise WorkspaceValidationError(
                "version is required for filter-form read() calls; "
                "pass version='in_flight' or version='stable'."
            )

        async with self.__lock:
            await self.__ensure_loaded()
            candidates = [a for a in self.__index.values() if self.__matches(a, active_filters)]

        # concern_kind requires loading full artifact files to inspect concerns.
        if concern_kind is not None:
            result: list[Artifact] = []
            for candidate in candidates:
                path = self.__artifact_path(candidate)
                full = await asyncio.to_thread(self.__read_artifact_file, path)
                if full is None:
                    continue
                if any(c.kind == concern_kind for c in full.concerns):
                    result.append(full if include_content else candidate)
            return result

        if not include_content:
            return list(candidates)

        loaded: list[Artifact] = []
        for candidate in candidates:
            path = self.__artifact_path(candidate)
            full = await asyncio.to_thread(self.__read_artifact_file, path)
            if full is not None:
                loaded.append(full)
        return loaded

    async def rebuild_index(self) -> None:
        """Rebuild the live index from the event log and on-disk artifacts.

        Reads ``events.jsonl``, determines which artifact IDs are live
        (published but not retired), loads each from disk, and rewrites
        ``index.json``.  Should be called on startup when the index is
        missing or corrupt.
        """
        async with self.__lock:
            await asyncio.to_thread(self.__do_rebuild_index)
            self.__loaded = True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def __ensure_loaded(self) -> None:
        if self.__loaded:
            return
        await asyncio.to_thread(self.__sync_init)
        self.__loaded = True

    def __sync_init(self) -> None:
        self.__workspace_dir.mkdir(parents=True, exist_ok=True)
        self.__retired_dir.mkdir(parents=True, exist_ok=True)
        if self.__index_path.exists():
            self.__load_index()
        else:
            self.__do_rebuild_index()

    def __validate_publish(
        self,
        artifact_type: ArtifactType,
        author: str,
        project_code: str,
        responsibility_code: str,
        req_ids: list[str],
        sup_ids: list[str],
        reviewed_artifact_id: str | None,
        verdict: Verdict | None,
        concerns_list: list[Concern],
    ) -> None:
        if not author.strip():
            raise WorkspaceValidationError("'author' must not be empty.")
        if not _PROJECT_CODE_RE.match(project_code):
            raise WorkspaceValidationError(
                f"'project_code' {project_code!r} does not match ^[A-Z][A-Z0-9]{{1,7}}$."
            )
        if not _RESPONSIBILITY_CODE_RE.match(responsibility_code):
            raise WorkspaceValidationError(
                f"'responsibility_code' {responsibility_code!r} does not match "
                "^[A-Z][A-Z0-9]{1,15}$."
            )
        for req_id in req_ids:
            if not _REQUIREMENT_ID_RE.match(req_id):
                raise WorkspaceValidationError(
                    f"Requirement ID {req_id!r} does not match the expected pattern."
                )
        for sup_id in sup_ids:
            if sup_id not in self.__index:
                raise ArtifactNotFoundError(
                    f"Artifact {sup_id!r} listed in 'supersedes' is not live."
                )
        if artifact_type is ArtifactType.FEEDBACK:
            if reviewed_artifact_id is None:
                raise WorkspaceValidationError(
                    "Feedback artifacts must supply 'reviewed_artifact_id'."
                )
            if reviewed_artifact_id not in self.__index:
                raise ArtifactNotFoundError(
                    f"'reviewed_artifact_id' {reviewed_artifact_id!r} is not a live artifact."
                )
            if verdict is None:
                raise WorkspaceValidationError("Feedback artifacts must supply 'verdict'.")
            if verdict is Verdict.REJECTED and not concerns_list:
                raise WorkspaceValidationError(
                    "Feedback artifacts with verdict=REJECTED must supply at least one concern."
                )
        else:
            if reviewed_artifact_id is not None:
                raise WorkspaceValidationError(
                    "'reviewed_artifact_id' is only valid on feedback artifacts."
                )
            if verdict is not None:
                raise WorkspaceValidationError("'verdict' is only valid on feedback artifacts.")
            if concerns_list:
                raise WorkspaceValidationError("'concerns' is only valid on feedback artifacts.")

    def __matches(self, artifact: Artifact, filters: dict[str, object]) -> bool:
        if "artifact_id" in filters and artifact.id != filters["artifact_id"]:
            return False
        if "author" in filters and artifact.author != filters["author"]:
            return False
        if "project_code" in filters and artifact.project_code != filters["project_code"]:
            return False
        if "responsibility_code" in filters and (
            artifact.responsibility_code != filters["responsibility_code"]
        ):
            return False
        if "requirement_id" in filters and (
            filters["requirement_id"] not in artifact.requirement_ids
        ):
            return False
        if "artifact_type" in filters and artifact.type != filters["artifact_type"]:
            return False
        return not ("verdict" in filters and artifact.verdict != filters["verdict"])

    def __artifact_path(self, artifact: Artifact) -> Path:
        return (
            self.__workspace_dir
            / artifact.project_code
            / artifact.responsibility_code
            / f"{artifact.id}.json"
        )

    def __write_artifact(self, artifact: Artifact) -> None:
        path = self.__artifact_path(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.__artifact_to_dict(artifact), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def __read_artifact_file(self, path: Path) -> Artifact | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return self.__artifact_from_dict(data)

    def __save_index(self) -> None:
        entries = {aid: self.__artifact_to_index_entry(a) for aid, a in self.__index.items()}
        tmp = self.__index_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.__index_path)

    def __load_index(self) -> None:
        data: dict[str, object] = json.loads(self.__index_path.read_text(encoding="utf-8"))
        self.__index = {}
        for aid, entry in data.items():
            if isinstance(entry, dict):
                self.__index[aid] = self.__artifact_from_index_entry(entry)

    def __do_rebuild_index(self) -> None:
        self.__index = {}
        if not self.__events_path.exists():
            self.__save_index()
            return

        published: dict[str, dict[str, object]] = {}
        retired: set[str] = set()

        with self.__events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event: dict[str, object] = json.loads(line)
                aid = str(event["artifact_id"])
                if event["event"] == "published":
                    published[aid] = event
                elif event["event"] == "retired":
                    retired.add(aid)

        for artifact_id, pub_event in published.items():
            if artifact_id in retired:
                continue
            project_code = str(pub_event["project_code"])
            responsibility_code = str(pub_event["responsibility_code"])
            path = self.__workspace_dir / project_code / responsibility_code / f"{artifact_id}.json"
            artifact = self.__read_artifact_file(path)
            if artifact is None:
                continue
            self.__index[artifact_id] = self.__strip_content(artifact)

        self.__save_index()

    def __append_event(self, event: dict[str, object]) -> None:
        with self.__events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def __strip_content(artifact: Artifact) -> Artifact:
        return replace(artifact, content=None, concerns=[])

    @staticmethod
    def __move_to_retired(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            src.replace(dst)

    @staticmethod
    def __artifact_to_dict(artifact: Artifact) -> dict[str, object]:
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

    @staticmethod
    def __artifact_from_dict(data: dict[str, object]) -> Artifact:
        concerns: list[Concern] = []
        concerns_raw = data.get("concerns")
        for raw in concerns_raw if isinstance(concerns_raw, list) else []:
            if not isinstance(raw, dict):
                continue
            fl = raw.get("first_line")
            ll = raw.get("last_line")
            ex = raw.get("excerpt")
            concerns.append(
                Concern(
                    kind=str(raw["kind"]),
                    description=str(raw["description"]),
                    first_line=int(fl) if isinstance(fl, (int, float)) else None,
                    last_line=int(ll) if isinstance(ll, (int, float)) else None,
                    excerpt=str(ex) if ex is not None else None,
                )
            )
        verdict_raw = data.get("verdict")
        meta_raw = data.get("metadata")
        req_raw = data.get("requirement_ids")
        sup_raw = data.get("supersedes")
        return Artifact(
            id=str(data["id"]),
            type=ArtifactType(str(data["type"])),
            author=str(data["author"]),
            project_code=str(data["project_code"]),
            responsibility_code=str(data["responsibility_code"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            content=str(data["content"]) if data.get("content") is not None else None,
            requirement_ids=[str(r) for r in req_raw] if isinstance(req_raw, list) else [],
            filename_hint=str(data["filename_hint"]) if data.get("filename_hint") else None,
            supersedes=[str(s) for s in sup_raw] if isinstance(sup_raw, list) else [],
            reviewed_artifact_id=(
                str(data["reviewed_artifact_id"]) if data.get("reviewed_artifact_id") else None
            ),
            verdict=Verdict(str(verdict_raw)) if verdict_raw else None,
            concerns=concerns,
            metadata=(
                {str(k): str(v) for k, v in meta_raw.items()} if isinstance(meta_raw, dict) else {}
            ),
            session_id=str(data["session_id"]) if data.get("session_id") else None,
        )

    @staticmethod
    def __artifact_to_index_entry(artifact: Artifact) -> dict[str, object]:
        return {
            "id": artifact.id,
            "type": artifact.type.value,
            "author": artifact.author,
            "project_code": artifact.project_code,
            "responsibility_code": artifact.responsibility_code,
            "created_at": artifact.created_at.isoformat(),
            "requirement_ids": artifact.requirement_ids,
            "filename_hint": artifact.filename_hint,
            "supersedes": artifact.supersedes,
            "reviewed_artifact_id": artifact.reviewed_artifact_id,
            "verdict": artifact.verdict.value if artifact.verdict else None,
            "session_id": artifact.session_id,
        }

    @staticmethod
    def __artifact_from_index_entry(entry: dict[str, object]) -> Artifact:
        verdict_raw = entry.get("verdict")
        req_raw = entry.get("requirement_ids")
        sup_raw = entry.get("supersedes")
        return Artifact(
            id=str(entry["id"]),
            type=ArtifactType(str(entry["type"])),
            author=str(entry["author"]),
            project_code=str(entry["project_code"]),
            responsibility_code=str(entry["responsibility_code"]),
            created_at=datetime.fromisoformat(str(entry["created_at"])),
            content=None,
            requirement_ids=[str(r) for r in req_raw] if isinstance(req_raw, list) else [],
            filename_hint=str(entry["filename_hint"]) if entry.get("filename_hint") else None,
            supersedes=[str(s) for s in sup_raw] if isinstance(sup_raw, list) else [],
            reviewed_artifact_id=(
                str(entry["reviewed_artifact_id"]) if entry.get("reviewed_artifact_id") else None
            ),
            verdict=Verdict(str(verdict_raw)) if verdict_raw else None,
            concerns=[],
            metadata={},
            session_id=str(entry["session_id"]) if entry.get("session_id") else None,
        )

    @staticmethod
    def __published_event(artifact: Artifact, timestamp: datetime) -> dict[str, object]:
        return {
            "timestamp": timestamp.isoformat(),
            "event": "published",
            "artifact_id": artifact.id,
            "type": artifact.type.value,
            "author": artifact.author,
            "project_code": artifact.project_code,
            "responsibility_code": artifact.responsibility_code,
            "requirement_ids": artifact.requirement_ids,
            "supersedes": artifact.supersedes,
            "reviewed_artifact_id": artifact.reviewed_artifact_id,
            "verdict": artifact.verdict.value if artifact.verdict else None,
            "filename_hint": artifact.filename_hint,
            "session_id": artifact.session_id,
        }

    @staticmethod
    def __retired_event(artifact: Artifact) -> dict[str, object]:
        return {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "event": "retired",
            "artifact_id": artifact.id,
            "type": artifact.type.value,
            "author": artifact.author,
            "project_code": artifact.project_code,
            "responsibility_code": artifact.responsibility_code,
            "requirement_ids": artifact.requirement_ids,
            "supersedes": artifact.supersedes,
            "reviewed_artifact_id": artifact.reviewed_artifact_id,
            "verdict": artifact.verdict.value if artifact.verdict else None,
            "filename_hint": artifact.filename_hint,
        }
