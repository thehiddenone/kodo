"""SessionManager — the server-authoritative owner of every session.

Mints/loads sessions, wires each engine to the shared :class:`LLMGateway`, and
arbitrates single-window ownership (a session is *taken* while a live window
holds it; a short grace window after a disconnect lets the same window reload
and reclaim).  It knows the gateway and the agent registry but **never**
references the connection registry — connections are injected as opaque ``conn``
handles via :meth:`bind_connection` / :meth:`drop_connection`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from kodo.llms import LLMGateway
from kodo.project import SessionWorkspace, WorkspaceLayout
from kodo.runtime import GateOrchestrator, WorkflowEngine
from kodo.state import TransientStore, new_session_id
from kodo.subagents import AgentRegistry
from kodo.transport import Connection, Outbox, SessionChannel

from ._key_broker import KeyBroker
from ._session import Session

__all__ = ["SessionManager"]

_log = logging.getLogger(__name__)

_DEFAULT_GRACE_SECONDS = 5.0


class SessionManager:
    """Creates, loads, and owns all sessions for the singleton server.

    Args:
        registry: Shared subagent registry.
        gateway: Shared LLM gateway (every session's engine schedules through it).
        get_settings: Returns fresh merged settings on each call.
        layout: Global ``~/.kodo`` layout.
        grace_seconds: Seconds a disconnected session stays reserved for its
            window before becoming free for others.
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        gateway: LLMGateway,
        get_settings: Callable[[], dict[str, object]],
        layout: WorkspaceLayout,
        grace_seconds: float = _DEFAULT_GRACE_SECONDS,
    ) -> None:
        self.__registry = registry
        self.__gateway = gateway
        self.__get_settings = get_settings
        self.__layout = layout
        self.__grace = grace_seconds
        self.__sessions: dict[str, Session] = {}
        self.__owner_window: dict[str, str] = {}  # session_id -> owning window id
        self.__live_conn: dict[str, str] = {}  # session_id -> live connection id
        self.__conn_session: dict[str, str] = {}  # connection id -> session_id
        self.__grace_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, session_id: str) -> Session | None:
        """Return the in-memory session, or ``None`` if not loaded."""
        return self.__sessions.get(session_id)

    def live_sessions(self) -> list[Session]:
        """Return every session currently loaded in memory.

        Used to broadcast a window-global settings change (model switch) to each
        running engine so it can react immediately (see
        ``WorkflowEngine.handle_config_changed``).
        """
        return list(self.__sessions.values())

    def session_for_connection(self, conn_id: str) -> Session | None:
        """Return the session a connection is bound to, if any."""
        sid = self.__conn_session.get(conn_id)
        return self.__sessions.get(sid) if sid else None

    # ------------------------------------------------------------------
    # Open / create
    # ------------------------------------------------------------------

    async def create(self, window_id: str) -> Session:
        """Mint a brand-new session owned by *window_id*."""
        session_id = self.__mint_id()
        session = await self.__build(session_id, resumed=False)
        self.__owner_window[session_id] = window_id
        _log.info("Session created: %s (window=%s)", session_id, window_id[:8])
        return session

    async def open(self, session_id: str, window_id: str) -> Session | None:
        """Open an existing session for *window_id*, enforcing single ownership.

        Returns ``None`` (``session_in_use``) if a *different* window currently
        holds it (live, or within its disconnect grace window).  A session id
        that does not exist on disk yields a fresh session under a new id (the
        caller relays the assigned id to the client).

        Args:
            session_id: The session id the window asked to resume.
            window_id: Stable id of the requesting VS Code window.

        Returns:
            Session | None: The opened session, or ``None`` if taken.
        """
        owner = self.__owner_window.get(session_id)
        if owner is not None and owner != window_id and self.__reserved(session_id):
            _log.info(
                "Refusing %s for window %s — held by %s", session_id, window_id[:8], owner[:8]
            )
            return None

        session = self.__sessions.get(session_id)
        if session is None:
            resumed = (self.__layout.sessions_dir / session_id).is_dir()
            if not resumed:
                _log.info("Session %s not found on disk — creating fresh", session_id)
                return await self.create(window_id)
            session = await self.__build(session_id, resumed=True)

        self.__cancel_grace(session_id)
        self.__owner_window[session_id] = window_id
        return session

    # ------------------------------------------------------------------
    # Ownership / connection binding (called by the connection registry)
    # ------------------------------------------------------------------

    async def bind_connection(self, session: Session, conn: Connection) -> None:
        """Attach a live connection to *session* (replays buffered events)."""
        self.__cancel_grace(session.id)
        self.__live_conn[session.id] = conn.id
        self.__conn_session[conn.id] = session.id
        await session.channel.attach(conn)

    def drop_connection(self, conn: Connection) -> None:
        """Handle a dropped connection: detach + start the grace window."""
        session_id = self.__conn_session.pop(conn.id, None)
        if session_id is None:
            return
        if self.__live_conn.get(session_id) == conn.id:
            del self.__live_conn[session_id]
        session = self.__sessions.get(session_id)
        if session is not None:
            session.channel.detach(conn)
        self.__start_grace(session_id)

    def release(self, session_id: str) -> None:
        """Free a session immediately (graceful window close)."""
        self.__cancel_grace(session_id)
        self.__owner_window.pop(session_id, None)
        self.__write_owner(session_id, owner=None)
        _log.info("Session released: %s", session_id)

    async def delete(self, session_id: str) -> None:
        """Permanently delete a session: stop its engine and remove its files.

        Drops all in-memory ownership/liveness tracking, stops the running
        engine (if loaded), then physically removes the session directory under
        ``sessions/`` plus its per-session LLM request logs.  The project the
        session worked on is untouched — only this session's data is deleted.

        Raises:
            OSError: If a directory cannot be removed; the caller surfaces the
                message to the client and keeps the socket open.
        """
        session = self.__sessions.pop(session_id, None)
        if session is not None:
            await session.engine.stop()
        self.__cancel_grace(session_id)
        self.__owner_window.pop(session_id, None)
        conn_id = self.__live_conn.pop(session_id, None)
        if conn_id is not None:
            self.__conn_session.pop(conn_id, None)
        for directory in (
            self.__layout.sessions_dir / session_id,
            self.__layout.llm_requests_dir / session_id,
        ):
            if directory.is_dir():
                shutil.rmtree(directory)
        _log.info("Session deleted: %s", session_id)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict[str, object]]:
        """List every persisted session for the picker.

        Returns:
            list[dict]: ``{id, name, created_at, last_modified, project_root,
            taken}`` per session; ``taken`` is ``True`` while a live window holds
            it.  ``project_root`` is the bound Guided project (``None`` ⇒
            problem-solving-only, openable anywhere).  ``created_at`` /
            ``last_modified`` are ISO-8601 strings (``""`` if unknown).
        """
        out: list[dict[str, object]] = []
        sessions_dir = self.__layout.sessions_dir
        if not sessions_dir.is_dir():
            return out
        for path in sorted(sessions_dir.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            name, created_at, last_modified = _read_meta(path)
            out.append(
                {
                    "id": path.name,
                    "name": name,
                    "created_at": created_at,
                    "last_modified": last_modified,
                    "project_root": _read_project_root(path),
                    "taken": path.name in self.__live_conn,
                }
            )
        return out

    async def shutdown(self) -> None:
        """Stop every running engine (server teardown)."""
        for task in list(self.__grace_tasks.values()):
            task.cancel()
        for session in list(self.__sessions.values()):
            await session.engine.stop()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def __reserved(self, session_id: str) -> bool:
        """Whether a session is live or still inside its disconnect grace."""
        return session_id in self.__live_conn or session_id in self.__grace_tasks

    def __mint_id(self) -> str:
        base = new_session_id()
        candidate = base
        suffix = 1
        while (self.__layout.sessions_dir / candidate).exists() or candidate in self.__sessions:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    async def __build(self, session_id: str, *, resumed: bool) -> Session:
        channel = SessionChannel(Outbox())
        transient = TransientStore(self.__layout.kodo_dir)
        gate = GateOrchestrator(channel, transient)
        key_broker = KeyBroker(channel)
        session_workspace = SessionWorkspace()
        engine = WorkflowEngine(
            sink=channel,
            gate=gate,
            key_provider=key_broker,
            get_settings=self.__get_settings,
            transient=transient,
            workspace_layout=self.__layout,
            registry=self.__registry,
            gateway=self.__gateway,
            session_workspace=session_workspace,
        )
        await engine.start(session_id, resumed)
        session = Session(
            id=session_id,
            channel=channel,
            engine=engine,
            transient=transient,
            session_workspace=session_workspace,
        )
        self.__sessions[session_id] = session
        return session

    def __start_grace(self, session_id: str) -> None:
        self.__cancel_grace(session_id)

        async def _expire() -> None:
            try:
                await asyncio.sleep(self.__grace)
            except asyncio.CancelledError:
                return
            self.__grace_tasks.pop(session_id, None)
            self.__owner_window.pop(session_id, None)
            self.__write_owner(session_id, owner=None)
            _log.info("Session %s grace expired — now free", session_id)

        self.__grace_tasks[session_id] = asyncio.create_task(_expire(), name=f"grace-{session_id}")

    def __cancel_grace(self, session_id: str) -> None:
        task = self.__grace_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    def __write_owner(self, session_id: str, *, owner: str | None) -> None:
        path = self.__layout.sessions_dir / session_id / "owner.json"
        if not path.parent.is_dir():
            return
        import datetime

        try:
            path.write_text(
                json.dumps(
                    {
                        "window_id": owner,
                        "last_seen": datetime.datetime.now(datetime.UTC).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            _log.debug("Could not write owner.json for %s", session_id)


def _read_meta(session_dir: Path) -> tuple[str, str, str]:
    """Return ``(name, created_at, last_modified)`` from a session's meta.json.

    ``name`` falls back to the directory name; the two timestamps fall back to
    ``""``.  ``last_modified`` falls back to ``created_at`` for sessions written
    before the field existed.
    """
    name = session_dir.name
    created_at = ""
    last_modified = ""
    meta = session_dir / "meta.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            name = str(data.get("session_name", session_dir.name))
            created_at = str(data.get("created_at", ""))
            last_modified = str(data.get("last_modified", created_at))
        except (json.JSONDecodeError, OSError):
            pass
    return name, created_at, last_modified


def _read_project_root(session_dir: Path) -> str | None:
    transient = session_dir / "transient.json"
    if not transient.exists():
        return None
    try:
        data = json.loads(transient.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    project = data.get("current_project")
    if isinstance(project, dict) and project.get("root"):
        return str(project["root"])
    return None
