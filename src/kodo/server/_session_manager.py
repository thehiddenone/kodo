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

    def any_running(self) -> bool:
        """Whether any loaded session's engine is mid-turn (phase ``running``).

        Consulted by the idle self-reap: a window reload can leave the server
        briefly with zero connections while a turn is still streaming; reaping
        then would kill work the reconnecting window is about to drain.
        """
        return any(s.engine.session.phase == "running" for s in self.__sessions.values())

    # ------------------------------------------------------------------
    # Open / create
    # ------------------------------------------------------------------

    async def create(self, window_id: str, *, thinking_level: str | None = None) -> Session:
        """Mint a brand-new session owned by *window_id*.

        Args:
            window_id: Stable id of the requesting VS Code window.
            thinking_level: Optional seed for the new session's
                ``thinking_level`` (validated by the caller — see
                ``_handle_session_hello``'s ``hello.thinking_level``,
                doc/SESSIONS.md); ``None`` uses the active model's family
                default.
        """
        session_id = self.__mint_id()
        session = await self.__build(session_id, resumed=False, thinking_level=thinking_level)
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
        """Attach a live connection to *session*.

        Does not replay the session's buffered backlog — the caller must send
        the reconnect base layer (hello.ack/state/session.history) first, then
        call :meth:`replay_backlog` (see :meth:`SessionChannel.attach`).
        """
        self.__cancel_grace(session.id)
        self.__live_conn[session.id] = conn.id
        self.__conn_session[conn.id] = session.id
        await session.channel.attach(conn)

    async def replay_backlog(self, session: Session) -> None:
        """Flush *session*'s buffered backlog and re-surface any still-open
        server-initiated prompt, now that the base layer is sent.

        The two are independent: buffered backlog is whatever streamed while
        the window was gone; a re-surfaced prompt (approval/question/
        permission/API key) is one whose future is still unresolved,
        regardless of whether it was ever buffered — see
        :meth:`SessionChannel.replay_pending_requests`.
        """
        await session.channel.replay_backlog()
        await session.channel.replay_pending_requests()

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

    def list_session_security_rules(self, session_id: str) -> list[dict[str, str]]:
        """Every "always allow" rule granted for *session_id*, command- and
        path-shape combined (doc/SECURITY_RULES_PLAN.md §2, §2.7).

        Reads the live :class:`TransientStore` if the session is currently
        loaded in memory (the authoritative copy — a mutation always flushes
        to disk immediately, so this is never stale), else reads
        ``transient.json`` directly, mirroring :func:`_read_workspace`'s
        on-disk fallback for a session nobody has resumed this run.

        Returns:
            list[dict[str, str]]: ``[{"kind": "command"|"path", "executable",
            "value"}, ...]``, sorted for a stable display order.
        """
        session = self.__sessions.get(session_id)
        if session is not None:
            rules = session.transient.security_rules
            path_rules = session.transient.security_path_rules
        else:
            rules, path_rules = _read_security_rules(self.__layout.sessions_dir / session_id)
        out = [{"kind": "command", "executable": e, "value": v} for e, v in sorted(rules)]
        out += [{"kind": "path", "executable": e, "value": v} for e, v in sorted(path_rules)]
        return out

    def delete_session_security_rules(
        self, session_id: str, rules: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Revoke each ``{kind, executable, value}`` entry for *session_id*;
        unknown ones are no-ops.

        Mutates the live :class:`TransientStore` (which flushes itself) when
        the session is loaded in memory, so a currently-open session's next
        autosave can't clobber the deletion with its stale in-memory copy;
        otherwise patches ``transient.json`` directly.

        Returns:
            list[dict[str, str]]: The resulting rule set, same shape as
            :meth:`list_session_security_rules` — lets the caller refresh a
            management UI from the response alone.
        """
        session = self.__sessions.get(session_id)
        if session is not None:
            for entry in rules:
                executable = str(entry.get("executable", ""))
                value = str(entry.get("value", ""))
                if not executable or not value:
                    continue
                if entry.get("kind") == "path":
                    session.transient.remove_security_path_rule(executable, value)
                else:
                    session.transient.remove_security_rule(executable, value)
        else:
            _remove_security_rules_on_disk(self.__layout.sessions_dir / session_id, rules)
        return self.list_session_security_rules(session_id)

    def list_sessions(self) -> list[dict[str, object]]:
        """List every persisted session for the picker.

        Returns:
            list[dict]: ``{id, name, created_at, last_modified, project_root,
            taken, workspace}`` per session; ``taken`` is ``True`` while a
            live window holds it.  ``project_root`` is the bound Guided
            project (``None`` ⇒ problem-solving-only, openable anywhere).
            ``workspace`` is ``{physical_root, folders, code_workspace_file}``
            (the session's remembered VS Code workspace shape) or ``None`` if
            nothing was ever pushed. ``created_at`` / ``last_modified`` are
            ISO-8601 strings (``""`` if unknown).
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
                    "workspace": _read_workspace(path),
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

    async def __build(
        self, session_id: str, *, resumed: bool, thinking_level: str | None = None
    ) -> Session:
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
        await engine.start(session_id, resumed, thinking_level=thinking_level)
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
            _log.info("Session %s grace expired — now free", session_id)

        self.__grace_tasks[session_id] = asyncio.create_task(_expire(), name=f"grace-{session_id}")

    def __cancel_grace(self, session_id: str) -> None:
        task = self.__grace_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()


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


def _read_workspace(session_dir: Path) -> dict[str, object] | None:
    """Read a session's remembered VS Code workspace shape for `session.list`.

    Mirrors :func:`_read_project_root`'s read-only, defensive style. Returns
    ``None`` until at least one folder has earned a checkpoint commit
    (``TransientStore.workspace_locked_paths`` non-empty) — a session that
    merely had ``workspace.folders`` pushed to it (every session open in a
    window gets that, per ``WorkflowEngine.handle_workspace_folders``) but
    never committed anything has no legitimate claim on that workspace, and
    must stay resumable with no reload/reopen at all. Without this gate, an
    empty exploratory session co-resident in a window with a project-owning
    session would get that project's workspace persisted into its own
    ``transient.json`` and silently reopen it after a restart — see the
    ``project_kodo_workspace_session_linkage`` memory (2026-07-22 round) for
    the incident this fixes.

    The returned ``locked`` flag is always ``True`` when the dict is
    returned at all — kodo-vsix additionally uses it to decide whether
    resuming this session into a *different current* workspace needs the
    user's explicit confirmation first.
    """
    transient = session_dir / "transient.json"
    if not transient.exists():
        return None
    try:
        data = json.loads(transient.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    locked_paths = data.get("workspace_locked_paths")
    if not locked_paths:
        return None
    physical_root = str(data.get("workspace_physical_root", ""))
    raw_folders = data.get("workspace_folders")
    folders = (
        {str(k): str(v) for k, v in raw_folders.items()} if isinstance(raw_folders, dict) else {}
    )
    code_file = data.get("workspace_code_file")
    return {
        "physical_root": physical_root,
        "folders": folders,
        "code_workspace_file": code_file if isinstance(code_file, str) and code_file else None,
        "locked": True,
    }


def _read_transient_json(session_dir: Path) -> dict[str, object]:
    """Best-effort read of a session's raw ``transient.json``, ``{}`` if
    absent/unreadable. Shared by the security-rules disk fallback below."""
    transient = session_dir / "transient.json"
    if not transient.exists():
        return {}
    try:
        data = json.loads(transient.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_security_rules(
    session_dir: Path,
) -> tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
    """Read a not-currently-loaded session's granted rules straight off disk.

    Mirrors :func:`_read_workspace`'s read-only, defensive style. Used by
    :meth:`SessionManager.list_session_security_rules` only when the session
    isn't loaded in memory — a live session's :class:`TransientStore` is the
    authoritative copy instead.

    Returns:
        tuple: ``(security_rules, security_path_rules)``, each a frozenset of
        ``(executable, value)`` pairs.
    """
    data = _read_transient_json(session_dir)

    def _parse(key: str) -> frozenset[tuple[str, str]]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(
            (str(pair[0]), str(pair[1]))
            for pair in raw
            if isinstance(pair, list) and len(pair) == 2
        )

    return _parse("security_rules"), _parse("security_path_rules")


def _remove_security_rules_on_disk(session_dir: Path, rules: list[dict[str, str]]) -> None:
    """Patch a not-currently-loaded session's ``transient.json`` in place,
    removing each ``{kind, executable, value}`` entry from the matching
    ``security_rules``/``security_path_rules`` list.

    A no-op if the session has no ``transient.json`` yet (nothing to
    revoke) — mirrors the live-session path's no-op-if-absent semantics
    (:meth:`kodo.state.TransientStore.remove_security_rule`). Patches only
    the two rule keys, leaving every other field in the file untouched.
    """
    transient = session_dir / "transient.json"
    if not transient.exists():
        return
    data = _read_transient_json(session_dir)
    if not data:
        return
    command_rules, path_rules = _read_security_rules(session_dir)
    for entry in rules:
        executable = str(entry.get("executable", ""))
        value = str(entry.get("value", ""))
        if not executable or not value:
            continue
        if entry.get("kind") == "path":
            path_rules = path_rules - {(executable, value)}
        else:
            command_rules = command_rules - {(executable, value)}
    data["security_rules"] = sorted([list(rule) for rule in command_rules])
    data["security_path_rules"] = sorted([list(rule) for rule in path_rules])
    try:
        transient.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        _log.exception("Failed to persist security-rule removal for %s", session_dir.name)
