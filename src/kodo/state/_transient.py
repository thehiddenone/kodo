"""Append-only JSONL transient state at ``~/.kodo/transient/``.

Stores per-LLM-call records for crash recovery and post-mortem inspection
(FR-STA-01).  Each session gets its own directory keyed by a hash of the
project root so multiple projects never collide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["TransientStore", "SessionMeta", "find_unfinished_session", "load_session_prompt"]

_TERMINAL_STAGES = frozenset({"IDLE", "STOPPED", "DONE", "ERROR"})

_log = logging.getLogger(__name__)

_KODO_HOME = Path(os.path.expanduser("~")) / ".kodo"
_TRANSIENT_BASE = _KODO_HOME / "transient"


def _project_hash(project_root: Path) -> str:
    """Return a 12-character hex hash of the absolute project path."""
    return hashlib.sha1(str(project_root.resolve()).encode()).hexdigest()[:12]


def _new_session_id() -> str:
    """Return a sortable session identifier based on UTC timestamp."""
    now = datetime.now(tz=UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


class SessionMeta:
    """Metadata for one Kodo session, persisted as ``session.json``.

    Attributes:
        session_id: Identifier derived from the session start time.
        project_hash: 12-char hash of the project root path.
        started_at: ISO-8601 timestamp when the session began.
        last_stage: Most recent workflow stage name.
        last_prompt: Original developer prompt (needed for resume).
        autonomous: Whether autonomous mode was active.
        dev_proxy_rules: Newline-joined Dev Proxy rules from project settings.
    """

    __session_id: str
    __project_hash: str
    __started_at: str
    __last_stage: str
    __last_prompt: str
    __autonomous: bool
    __dev_proxy_rules: str
    __path: Path

    def __init__(self, session_dir: Path, session_id: str, project_hash: str) -> None:
        """Initialise session metadata.

        Args:
            session_dir (Path): Directory for this session's files.
            session_id (str): Unique session identifier.
            project_hash (str): 12-char project hash.
        """
        self.__session_id = session_id
        self.__project_hash = project_hash
        self.__started_at = datetime.now(tz=UTC).isoformat()
        self.__last_stage = "IDLE"
        self.__last_prompt = ""
        self.__autonomous = False
        self.__dev_proxy_rules = ""
        self.__path = session_dir / "session.json"

    @property
    def session_id(self) -> str:
        """Session identifier."""
        return self.__session_id

    @property
    def project_hash(self) -> str:
        """12-character project hash."""
        return self.__project_hash

    @property
    def last_stage(self) -> str:
        """Most recent workflow stage name."""
        return self.__last_stage

    @property
    def last_prompt(self) -> str:
        """Original developer prompt stored for resume support."""
        return self.__last_prompt

    @property
    def autonomous(self) -> bool:
        """Whether autonomous mode is active."""
        return self.__autonomous

    @property
    def dev_proxy_rules(self) -> str:
        """Newline-joined Dev Proxy rule strings from project settings."""
        return self.__dev_proxy_rules

    def update(
        self,
        *,
        stage: str | None = None,
        prompt: str | None = None,
        autonomous: bool | None = None,
        dev_proxy_rules: str | None = None,
    ) -> None:
        """Update mutable fields and flush to disk.

        Args:
            stage (str | None): New stage name if changed.
            prompt (str | None): Developer prompt to persist for resume.
            autonomous (bool | None): New autonomous flag if changed.
            dev_proxy_rules (str | None): Updated Dev Proxy rules string.
        """
        if stage is not None:
            self.__last_stage = stage
        if prompt is not None:
            self.__last_prompt = prompt
        if autonomous is not None:
            self.__autonomous = autonomous
        if dev_proxy_rules is not None:
            self.__dev_proxy_rules = dev_proxy_rules
        self.__flush()

    def __flush(self) -> None:
        data = {
            "session_id": self.__session_id,
            "project_hash": self.__project_hash,
            "started_at": self.__started_at,
            "last_stage": self.__last_stage,
            "last_prompt": self.__last_prompt,
            "autonomous": self.__autonomous,
            "dev_proxy_rules": self.__dev_proxy_rules,
        }
        self.__path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_session_prompt(project_root: Path, session_id: str) -> str:
    """Return the stored developer prompt for a specific session, or empty string.

    Args:
        project_root (Path): Absolute path to the Kodo project root.
        session_id (str): Session ID returned by :func:`find_unfinished_session`.

    Returns:
        str: The original prompt, or ``""`` if not found.
    """
    proj_hash = _project_hash(project_root)
    proj_dir = _TRANSIENT_BASE / proj_hash
    if not proj_dir.exists():
        return ""
    for session_dir in proj_dir.iterdir():
        session_json = session_dir / "session.json"
        if not session_json.exists():
            continue
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("session_id", "")) == session_id:
            return str(data.get("last_prompt", ""))
    return ""


def find_unfinished_session(project_root: Path) -> str | None:
    """Return the most recent unfinished session ID for a project, or ``None``.

    Scans ``~/.kodo/transient/<project-hash>/`` for session directories whose
    ``session.json`` records a non-terminal stage.  The most recently created
    session wins (directory names are sortable ISO timestamps).

    Args:
        project_root (Path): Absolute path to the Kodo project root.

    Returns:
        str | None: Session ID to offer for resume, or ``None`` if nothing pending.
    """
    proj_hash = _project_hash(project_root)
    proj_dir = _TRANSIENT_BASE / proj_hash
    if not proj_dir.exists():
        return None

    candidates = sorted(proj_dir.iterdir(), reverse=True)
    for session_dir in candidates:
        session_json = session_dir / "session.json"
        if not session_json.exists():
            continue
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        last_stage = str(data.get("last_stage", "IDLE"))
        prompt = str(data.get("last_prompt", ""))
        if last_stage not in _TERMINAL_STAGES and prompt:
            return str(data.get("session_id", session_dir.name))
    return None


class TransientStore:
    """Per-session transient state store at ``~/.kodo/transient/``.

    Directory layout::

        ~/.kodo/transient/
            <project-hash>/
                <session-id>/
                    session.json
                    agents/
                        <agent-name>.jsonl
                    mcp/
                        <tool>.jsonl

    Records are append-only (one JSON line per LLM call or MCP invocation).
    Rotation and compaction are post-MVP.
    """

    __session_dir: Path
    __agents_dir: Path
    __meta: SessionMeta
    __lock: asyncio.Lock

    def __init__(self, project_root: Path) -> None:
        """Create a new session under the transient store for ``project_root``.

        Args:
            project_root (Path): Absolute path to the Kodo project root.
        """
        proj_hash = _project_hash(project_root)
        session_id = _new_session_id()
        session_dir = _TRANSIENT_BASE / proj_hash / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        self.__session_dir = session_dir
        self.__agents_dir = session_dir / "agents"
        self.__agents_dir.mkdir(exist_ok=True)
        (session_dir / "mcp").mkdir(exist_ok=True)

        self.__meta = SessionMeta(session_dir, session_id, proj_hash)
        self.__meta.update()  # write initial session.json

        self.__lock = asyncio.Lock()
        _log.info("Transient session: %s", session_dir)

    @property
    def session_id(self) -> str:
        """Identifier for the current session."""
        return self.__meta.session_id

    @property
    def session_dir(self) -> Path:
        """Absolute path to this session's directory."""
        return self.__session_dir

    @property
    def meta(self) -> SessionMeta:
        """Session metadata object."""
        return self.__meta

    async def write_agent_record(self, agent_name: str, record: dict[str, object]) -> None:
        """Append one JSON record to an agent's JSONL log.

        Args:
            agent_name (str): Agent identifier (used as the filename stem).
            record (dict[str, object]): Arbitrary JSON-serialisable data.
        """
        path = self.__agents_dir / f"{agent_name}.jsonl"
        line = json.dumps(record, default=str) + "\n"
        async with self.__lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: path.open("a", encoding="utf-8").write(line)
            )

    async def write_mcp_record(self, tool_name: str, record: dict[str, object]) -> None:
        """Append one JSON record to an MCP tool's JSONL log.

        Tool names may contain ``/`` or ``.`` (e.g. ``tools/fileio.read_file``);
        they are sanitised to a flat filename so no nested directories are created
        under ``mcp/``.

        Args:
            tool_name (str): Tool identifier; slashes and dots become underscores.
            record (dict[str, object]): Arbitrary JSON-serialisable data.
        """
        safe_name = tool_name.replace("/", "_").replace(".", "_")
        path = self.__session_dir / "mcp" / f"{safe_name}.jsonl"
        line = json.dumps(record, default=str) + "\n"
        async with self.__lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: path.open("a", encoding="utf-8").write(line)
            )
