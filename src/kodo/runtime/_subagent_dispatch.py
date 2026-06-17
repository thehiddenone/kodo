"""Leaf sub-agent tool dispatch.

Provides :class:`SubagentDispatcher`, which translates ``publish_artifact``
and ``read_artifact`` tool calls into direct :class:`~kodo.workspace.Workspace`
method calls (no MCP subprocess needed — the same logic in one process).

Report tools (``escalate_blocker``, ``ask_user``,
``request_user_review_artifact``, ``report_artifact_completed``) are
handled here as well, along with the native file I/O tools (``create_file``,
``edit_file``, ``delete_file``, ``copy_file``, ``move_file``) and the native
shell tool (``run_command``) — direct, scoped filesystem/process access with
no MCP subprocess involved.

The specs for all of these tools live in :mod:`kodo.toolspecs`; this module
only contains dispatch logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from kodo.subagents import SubAgent
from kodo.toolspecs import (
    ASK_USER,
    COPY_FILE,
    CREATE_FILE,
    DELETE_FILE,
    EDIT_FILE,
    LEAF_TOOLS_BY_NAME,
    MOVE_FILE,
    PUBLISH_ARTIFACT,
    READ_ARTIFACT,
    RUN_COMMAND,
    ToolSpec,
)
from kodo.workspace import Artifact, ArtifactType, Concern, Verdict, Workspace

from ._gates import GateOrchestrator

__all__ = [
    "LEAF_TOOLS_BY_NAME",
    "SubagentDispatcher",
]

_log = logging.getLogger(__name__)

_FILEIO_TOOL_NAMES: frozenset[str] = frozenset(
    {CREATE_FILE.name, EDIT_FILE.name, DELETE_FILE.name, COPY_FILE.name, MOVE_FILE.name}
)


def tools_for_agent(agent: SubAgent) -> list[ToolSpec]:
    """Return the ToolSpec list for the agent based on its declared tool names.

    Unknown tool names are skipped (forward-compatibility).

    Args:
        agent (SubAgent): The sub-agent whose tool list to resolve.

    Returns:
        list[ToolSpec]: Tool specs the engine should pass to the LLM call.
    """
    return [LEAF_TOOLS_BY_NAME[name] for name in agent.tools if name in LEAF_TOOLS_BY_NAME]


# ---------------------------------------------------------------------------
# SubagentDispatcher
# ---------------------------------------------------------------------------


class SubagentDispatcher:
    """Routes tool calls from a leaf sub-agent to inline handlers.

    Wraps the workspace ``publish`` and ``read`` methods and the report
    tools (escalate, narrative dialog) so the engine can serve all sub-agent
    tool calls in-process without an MCP subprocess.

    Args:
        workspace: The shared :class:`~kodo.workspace.Workspace` instance.
        gate: Gate orchestrator for user-interaction tools.
        agent_name: Name of the running sub-agent (injected as ``author``).
        session_id: Session ID to attach to every published artifact.
        autonomous: Whether the session is in autonomous mode. When ``True``,
            ``request_user_review_artifact`` auto-accepts and ``escalate_blocker``
            hands back to the orchestrator without surfacing to the user.
            (``ask_user`` is withheld from the agent entirely by the registry.)
        complete_fn: Callback invoked on ``report_artifact_completed`` to promote
            the artifact and mark it completed. Defaults to flipping the index
            state via the workspace (no promotion) when not supplied.
    """

    __workspace: Workspace
    __gate: GateOrchestrator
    __agent_name: str
    __session_id: str
    __autonomous: bool
    __complete_fn: Callable[[str], Awaitable[None]]
    __published_ids: list[str]
    __stop_requested: bool

    def __init__(
        self,
        workspace: Workspace,
        gate: GateOrchestrator,
        agent_name: str,
        session_id: str,
        autonomous: bool = False,
        complete_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Initialise the dispatcher.

        Args:
            workspace (Workspace): Shared artifact store.
            gate (GateOrchestrator): Gate/question orchestrator.
            agent_name (str): Sub-agent name (used as artifact author).
            session_id (str): Current session ID.
            autonomous (bool): Whether autonomous mode is active.
            complete_fn: Promotion callback for completed artifacts.
        """
        self.__workspace = workspace
        self.__gate = gate
        self.__agent_name = agent_name
        self.__session_id = session_id
        self.__autonomous = autonomous
        self.__complete_fn = complete_fn if complete_fn is not None else workspace.mark_completed
        self.__published_ids = []
        self.__stop_requested = False

    @property
    def published_ids(self) -> list[str]:
        """Artifact IDs published during this dispatcher's lifetime."""
        return list(self.__published_ids)

    @property
    def stop_requested(self) -> bool:
        """``True`` when the agent called ``escalate_blocker``.

        The engine checks this after each tool-call batch to decide whether to
        exit the agent loop early and hand control back to the orchestrator.
        Completion (``report_artifact_completed``) does not force a stop — a
        solo agent may report several artifacts complete and then end its turn
        naturally.
        """
        return self.__stop_requested

    async def dispatch(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Route one tool call to its handler and return a JSON-encoded result.

        Args:
            tool_name (str): Tool name from :data:`LEAF_TOOLS_BY_NAME`.
            tool_input (dict[str, object]): Parsed JSON input from the LLM.

        Returns:
            str: JSON-encoded result returned to the LLM as a tool result.
        """
        if tool_name == PUBLISH_ARTIFACT.name:
            return await self.__publish(tool_input)
        if tool_name == READ_ARTIFACT.name:
            return await self.__read(tool_input)
        if tool_name == "escalate_blocker":
            return await self.__escalate(tool_input)
        if tool_name == ASK_USER.name:
            return await self.__ask_user(tool_input)
        if tool_name == "request_user_review_artifact":
            return await self.__request_review(tool_input)
        if tool_name == "report_artifact_completed":
            return await self.__report_completed(tool_input)
        if tool_name in _FILEIO_TOOL_NAMES:
            return self.__fileio(tool_name, tool_input)
        if tool_name == RUN_COMMAND.name:
            return await self.__run_command(tool_input)
        _log.warning("SubagentDispatcher: unknown tool %r from %s", tool_name, self.__agent_name)
        return json.dumps({"error": f"Unknown tool: {tool_name!r}"})

    # ------------------------------------------------------------------
    # publish_artifact
    # ------------------------------------------------------------------

    async def __publish(self, tool_input: dict[str, object]) -> str:
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
            artifact_id = await self.__workspace.publish(
                artifact_type=artifact_type,
                author=self.__agent_name,
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
                session_id=self.__session_id,
            )
        except Exception as exc:
            _log.exception("publish_artifact failed for %s: %s", self.__agent_name, exc)
            return json.dumps({"error": str(exc)})

        self.__published_ids.append(artifact_id)
        _log.info(
            "publish_artifact: %s published %s type=%s id=%s",
            self.__agent_name,
            artifact_type.value,
            artifact_type.value,
            artifact_id[:8],
        )
        return json.dumps({"id": artifact_id})

    # ------------------------------------------------------------------
    # read_artifact
    # ------------------------------------------------------------------

    async def __read(self, tool_input: dict[str, object]) -> str:
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
            artifacts = await self.__workspace.read(
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
            _log.exception("read_artifact failed for %s: %s", self.__agent_name, exc)
            return json.dumps({"error": str(exc)})

        return json.dumps([_serialize_artifact(a) for a in artifacts])

    # ------------------------------------------------------------------
    # Report tools
    # ------------------------------------------------------------------

    async def __escalate(self, tool_input: dict[str, object]) -> str:
        reason = str(tool_input.get("reason", ""))
        summary = str(tool_input.get("summary", ""))
        _log.info("escalate_blocker from %s: reason=%s %s", self.__agent_name, reason, summary[:80])
        # Ending the turn hands control back to the orchestrator, which owns
        # triage. In autonomous mode there is no user to adjudicate; the
        # orchestrator decides. In interactive mode the orchestrator may choose
        # to ask the user, but a present user can also answer the surfaced
        # blocker directly here.
        self.__stop_requested = True
        if self.__autonomous:
            return json.dumps({"status": "escalated", "reason": reason})
        response = await self.__gate.fire_question(summary, "free_text")
        return json.dumps(
            {"status": "escalated", "reason": reason, "user_response": response.answer_text}
        )

    async def __ask_user(self, tool_input: dict[str, object]) -> str:
        question = str(tool_input.get("question", ""))
        mode = str(tool_input.get("mode", "free_text"))
        choices_raw = tool_input.get("choices")
        choices: list[dict[str, str]] | None = None
        if isinstance(choices_raw, list):
            choices = [
                {"key": str(c.get("key", "")), "label": str(c.get("label", ""))}
                for c in choices_raw
                if isinstance(c, dict)
            ]
        _log.info("ask_user from %s: %s", self.__agent_name, question[:80])
        response = await self.__gate.fire_question(question, mode, choices)
        if mode == "choice":
            return json.dumps({"choice_key": response.choice_key})
        return json.dumps({"answer_text": response.answer_text})

    async def __request_review(self, tool_input: dict[str, object]) -> str:
        artifact_id = str(tool_input.get("artifact_id", ""))
        summary = str(tool_input.get("summary", "")) or "Please review this artifact."
        _log.info("request_user_review_artifact from %s: id=%s", self.__agent_name, artifact_id[:8])
        # Autonomous mode: the user is away, so the engine auto-accepts.
        if self.__autonomous:
            return json.dumps({"action": "agree", "feedback": ""})
        gate_type = "review"
        try:
            arts = await self.__workspace.read(artifact_id=artifact_id, include_content=False)
            if arts:
                gate_type = arts[0].type.value
        except Exception:  # pragma: no cover - label derivation is best-effort
            pass
        response = await self.__gate.fire_approval(
            gate_type, artifact_id=artifact_id, summary=summary
        )
        return json.dumps({"action": response.action, "feedback": response.feedback})

    async def __report_completed(self, tool_input: dict[str, object]) -> str:
        artifact_id = str(tool_input.get("artifact_id", ""))
        _log.info("report_artifact_completed from %s: id=%s", self.__agent_name, artifact_id[:8])
        # Promote (materialize + mirror commit + move out of staging) and flip
        # the index entry to completed so query_frontier sees it. The default
        # callback only flips state (used by isolated tests with no engine).
        await self.__complete_fn(artifact_id)
        return json.dumps({"status": "completed", "artifact_id": artifact_id})

    # ------------------------------------------------------------------
    # Native file I/O tools
    # ------------------------------------------------------------------

    def __resolve_path(self, path: str) -> Path:
        root = self.__workspace.project_root
        candidate = Path(path)
        resolved = (
            (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        )
        try:
            resolved.relative_to(root)
        except ValueError:
            raise PermissionError(
                f"Path {path!r} is outside the project root {str(root)!r}"
            ) from None
        return resolved

    def __fileio(self, tool_name: str, tool_input: dict[str, object]) -> str:
        try:
            if tool_name == "create_file":
                return self.__create_file(tool_input)
            if tool_name == "edit_file":
                return self.__edit_file(tool_input)
            if tool_name == "delete_file":
                return self.__delete_file(tool_input)
            if tool_name == "copy_file":
                return self.__copy_file(tool_input)
            return self.__move_file(tool_input)
        except OSError as exc:
            _log.info("%s from %s failed: %s", tool_name, self.__agent_name, exc)
            return json.dumps({"error": str(exc)})

    def __create_file(self, tool_input: dict[str, object]) -> str:
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        target = self.__resolve_path(path)
        if target.exists():
            raise FileExistsError(f"File already exists: {path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"status": "created", "path": path})

    def __edit_file(self, tool_input: dict[str, object]) -> str:
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        target = self.__resolve_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        target.write_text(content, encoding="utf-8")
        return json.dumps({"status": "edited", "path": path})

    def __delete_file(self, tool_input: dict[str, object]) -> str:
        path = str(tool_input.get("path", ""))
        target = self.__resolve_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        target.unlink()
        return json.dumps({"status": "deleted", "path": path})

    def __copy_file(self, tool_input: dict[str, object]) -> str:
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        src = self.__resolve_path(source)
        dst = self.__resolve_path(destination)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return json.dumps({"status": "copied", "source": source, "destination": destination})

    def __move_file(self, tool_input: dict[str, object]) -> str:
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        src = self.__resolve_path(source)
        dst = self.__resolve_path(destination)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dst)
        return json.dumps({"status": "moved", "source": source, "destination": destination})

    # ------------------------------------------------------------------
    # Native shell tool
    # ------------------------------------------------------------------

    async def __run_command(self, tool_input: dict[str, object]) -> str:
        command = str(tool_input.get("command", ""))
        working_dir_raw = tool_input.get("working_dir")
        try:
            cwd = (
                self.__resolve_path(str(working_dir_raw))
                if working_dir_raw
                else self.__workspace.project_root
            )
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})
        _log.info("run_command from %s: %s (cwd=%s)", self.__agent_name, command[:120], cwd)
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return json.dumps(
            {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
        )


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _serialize_artifact(artifact: Artifact) -> dict[str, object]:
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
