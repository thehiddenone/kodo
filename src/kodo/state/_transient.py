"""Per-session state store under ``.kodo/sessions/<session-id>/``.

Each server session gets one directory.  The directory is created on first
use and reused across restarts when the session is resumed.  Layout::

    .kodo/sessions/<posix-timestamp>/
        meta.json        — human-readable metadata (name, creation time)
        transient.json   — mutable runtime state (stage, prompt, autonomous)
        session.jsonl    — append-only orchestrator LLM context (all messages)
        agents/          — per-sub-agent JSONL call logs
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["TransientStore"]

_log = logging.getLogger(__name__)

_UNSET: object = object()


def _new_session_id() -> str:
    """Return a new session ID based on the current POSIX timestamp."""
    return str(int(time.time()))


@dataclass
class _SessionPaths:
    root: Path

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    @property
    def transient(self) -> Path:
        return self.root / "transient.json"

    @property
    def session_log(self) -> Path:
        return self.root / "session.jsonl"

    @property
    def agents(self) -> Path:
        return self.root / "agents"


class TransientStore:
    """Per-session transient state store under ``.kodo/sessions/``.

    Created early (before bootstrap); call :meth:`attach_session` once the
    session ID is known from :class:`~kodo.runtime._bootstrap.ProjectBootstrap`.

    Args:
        kodo_dir (Path): The project's ``.kodo/`` directory.
    """

    __kodo_dir: Path
    __paths: _SessionPaths | None
    __session_id: str
    __stage: str
    __last_prompt: str
    __autonomous: bool
    __pending_prompt: dict[str, object] | None
    __lock: asyncio.Lock

    def __init__(self, kodo_dir: Path) -> None:
        """Initialise without attaching a session.

        Args:
            kodo_dir (Path): The project's ``.kodo/`` directory.
        """
        self.__kodo_dir = kodo_dir
        self.__paths = None
        self.__session_id = ""
        self.__stage = "IDLE"
        self.__last_prompt = ""
        self.__autonomous = False
        self.__pending_prompt = None
        self.__lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        """Identifier for the current session."""
        return self.__session_id

    @property
    def session_dir(self) -> Path:
        """Absolute path to this session's directory."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.root

    @property
    def session_log_path(self) -> Path:
        """Path to the orchestrator JSONL message log."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.session_log

    @property
    def stage(self) -> str:
        """Most recent workflow stage."""
        return self.__stage

    @property
    def last_prompt(self) -> str:
        """Last developer prompt, stored for resume support."""
        return self.__last_prompt

    @property
    def autonomous(self) -> bool:
        """Whether autonomous mode is active."""
        return self.__autonomous

    @property
    def pending_prompt(self) -> dict[str, object] | None:
        """The outstanding ``prompt.question``/``prompt.approval`` request, if any.

        Persisted so that a server restart with an unanswered prompt can
        re-surface it to the user instead of silently dropping it.
        """
        return self.__pending_prompt

    def attach_session(self, session_id: str, resumed: bool) -> None:
        """Attach to an existing session or create a new one.

        Called by the engine immediately after bootstrap completes.

        Args:
            session_id (str): Session identifier from bootstrap.
            resumed (bool): ``True`` if the session already exists on disk.
        """
        paths = _SessionPaths(self.__kodo_dir / "sessions" / session_id)
        self.__paths = paths
        self.__session_id = session_id

        if resumed:
            self.__load_transient(paths)
            _log.info("Transient session resumed: %s", session_id)
        else:
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.agents.mkdir(exist_ok=True)
            self.__write_meta(paths)
            self.__flush(paths)
            _log.info("Transient session created: %s", session_id)

    def update(
        self,
        *,
        stage: str | None = None,
        prompt: str | None = None,
        autonomous: bool | None = None,
        pending_prompt: dict[str, object] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Update mutable fields and flush ``transient.json`` to disk.

        Args:
            stage (str | None): New stage name if changed.
            prompt (str | None): Developer prompt to persist for resume.
            autonomous (bool | None): New autonomous flag if changed.
            pending_prompt (dict[str, object] | None): Outstanding
                ``prompt.question``/``prompt.approval`` request to persist,
                or ``None`` to clear it. Left unchanged if omitted.
        """
        if stage is not None:
            self.__stage = stage
        if prompt is not None:
            self.__last_prompt = prompt
        if autonomous is not None:
            self.__autonomous = autonomous
        if pending_prompt is not _UNSET:
            self.__pending_prompt = pending_prompt
        if self.__paths is not None:
            self.__flush(self.__paths)

    def append_message(self, role: str, content: str | list[dict[str, object]]) -> None:
        """Append one LLM message to ``session.jsonl``.

        Args:
            role (str): ``'user'`` or ``'assistant'``.
            content (str | list): Message content (plain text or content blocks).
        """
        if self.__paths is None:
            return
        line = json.dumps({"role": role, "content": content}) + "\n"
        self.__paths.session_log.open("a", encoding="utf-8").write(line)

    def read_messages(self) -> list[dict[str, object]]:
        """Return all messages from ``session.jsonl`` in order.

        Returns:
            list[dict[str, object]]: Ordered list of ``{role, content}`` dicts.
        """
        if self.__paths is None or not self.__paths.session_log.exists():
            return []
        messages: list[dict[str, object]] = []
        for line in self.__paths.session_log.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                try:
                    messages.append(json.loads(stripped))
                except json.JSONDecodeError:
                    _log.warning("Skipping malformed session.jsonl line")
        return messages

    async def write_agent_record(self, agent_name: str, record: dict[str, object]) -> None:
        """Append one JSON record to a sub-agent's JSONL log.

        Args:
            agent_name (str): Agent identifier (used as the filename stem).
            record (dict[str, object]): Arbitrary JSON-serialisable data.
        """
        if self.__paths is None:
            return
        path = self.__paths.agents / f"{agent_name}.jsonl"
        line = json.dumps(record, default=str) + "\n"
        async with self.__lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: path.open("a", encoding="utf-8").write(line)
            )

    def __load_transient(self, paths: _SessionPaths) -> None:
        if not paths.transient.exists():
            return
        try:
            data = json.loads(paths.transient.read_text(encoding="utf-8"))
            self.__stage = str(data.get("stage", "IDLE"))
            self.__last_prompt = str(data.get("last_prompt", ""))
            self.__autonomous = bool(data.get("autonomous", False))
            pending = data.get("pending_prompt")
            self.__pending_prompt = pending if isinstance(pending, dict) else None
        except Exception:
            _log.warning("Could not parse transient.json — using defaults")

    def __write_meta(self, paths: _SessionPaths) -> None:
        meta = {
            "session_name": "Unnamed Session",
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        paths.meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def __flush(self, paths: _SessionPaths) -> None:
        data = {
            "stage": self.__stage,
            "last_prompt": self.__last_prompt,
            "autonomous": self.__autonomous,
            "pending_prompt": self.__pending_prompt,
        }
        paths.transient.write_text(json.dumps(data, indent=2), encoding="utf-8")
