"""aiohttp application factory and WebSocket endpoint for the Kōdo server.

The server is a machine-wide singleton: many VS Code windows connect to it, each
driving its own session.  Frames are routed by ``payload.session_id`` to the
owning :class:`~kodo.server.SessionManager` session; ``hello`` (the only frame
without a required ``session_id``) creates or resumes one.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from aiohttp import web

from kodo.llms import LLMEntry, LLMGateway, get_llm_registry
from kodo.llms.llamacpp import (
    LlamaServer,
    LlamaServerConfig,
    download_model,
    ensure_llama_running,
    find_installed,
    find_running_server,
    get_model_path,
    install_llamacpp,
)
from kodo.project import WorkspaceLayout, kodo_user_dir
from kodo.subagents import AgentRegistry
from kodo.transport import (
    EVT_LLAMA_STATE,
    EVT_LLAMACPP_INSTALL_PROGRESS,
    EVT_MODEL_INSTALL_PROGRESS,
    MSG_COMPACT_NOW,
    MSG_CONFIG_RELOAD,
    MSG_HELLO,
    MSG_LLAMA_START,
    MSG_LLAMA_STOP,
    MSG_LLAMACPP_INSTALL,
    MSG_MODE_SET,
    MSG_MODEL_INSTALL,
    MSG_PING,
    MSG_PROJECT_SET,
    MSG_PROMPT_SUBMIT,
    MSG_SESSION_DELETE,
    MSG_SESSION_LIST,
    MSG_SESSION_RELEASE,
    MSG_STOP,
    MSG_WORKFLOW_SET,
    MSG_WORKSPACE_FOLDERS,
    Envelope,
)

from ._config import Config
from ._connection_registry import (
    CONNECTION_REGISTRY_KEY,
    ConnectionRegistry,
    HandlerFn,
    Request,
)
from ._session import Session
from ._session_manager import SessionManager

_log = logging.getLogger(__name__)

_SERVER_VERSION: str = "0.2.0b1"
_MANAGER_KEY: web.AppKey[SessionManager] = web.AppKey("session_manager")

# Subagents directory: kodo/subagents/ next to kodo/server/
_AGENTS_DIR = Path(__file__).parent.parent / "subagents"


# ------------------------------------------------------------------
# Startup validation + logging
# ------------------------------------------------------------------


def _check_git_on_path() -> None:
    if shutil.which("git") is None:
        _log.error("'git' is not on PATH.  Kōdo requires git.")
        sys.exit(1)


def _setup_log_file(layout: WorkspaceLayout, log_level: str) -> None:
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        layout.server_log,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(log_level)
    logging.getLogger().addHandler(handler)
    _log.info("Log file: %s", layout.server_log)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _require_session(req: Request) -> Session | None:
    """Resolve the request's session, replying with an error if unknown."""
    if req.session is not None:
        return req.session
    await req.reply(
        {
            "type": "error",
            "code": "unknown_session",
            "message": f"No such session: {req.session_id!r}",
            "recoverable": True,
        }
    )
    return None


# ------------------------------------------------------------------
# hello — create or resume a session, bind the connection
# ------------------------------------------------------------------


async def _handle_hello(req: Request) -> None:
    payload = req.env.payload
    window_id = str(payload.get("window_id") or req.connection.id)
    role = str(payload.get("role") or "session")

    # A control connection (the window's sidebar) drives window-global, session-
    # less frames only (llama / model management, session.list).  It must NOT
    # create or bind a session — it just needs the model/llama snapshot.
    if role == "control":
        _log.info(
            "Hello (control) from client=%s window=%s",
            payload.get("client", "unknown"),
            window_id[:8],
        )
        await req.reply(
            {
                "type": "hello.ack",
                "role": "control",
                "server_version": _SERVER_VERSION,
                **_llama_payload(),
            }
        )
        return

    requested = str(payload.get("session_id") or "")
    _log.info(
        "Hello from client=%s window=%s session=%s",
        payload.get("client", "unknown"),
        window_id[:8],
        requested or "<new>",
    )

    if requested:
        session = await req.manager.open(requested, window_id)
        if session is None:
            await req.reply({"type": "hello.ack", "error": "session_in_use"})
            return
    else:
        session = await req.manager.create(window_id)

    await req.manager.bind_connection(session, req.connection)

    await req.reply(
        {
            "type": "hello.ack",
            "server_version": _SERVER_VERSION,
            "session_id": session.id,
            "current_project": session.engine.current_project,
            "state": session.engine.session.to_dict(),
            **_llama_payload(),
        }
    )

    await session.channel.send(Envelope.make_event("state", session.engine.session.to_dict()))
    await session.channel.send(
        Envelope.make_event(
            "session.name",
            {"session_id": session.id, "name": session.engine.session_name},
        )
    )
    history = session.engine.history_entries()
    if history:
        await session.channel.send(Envelope.make_event("session.history", {"entries": history}))


async def _handle_ping(req: Request) -> None:
    await req.reply({"type": "pong"})


def _llama_payload() -> dict[str, object]:
    llm_registry = get_llm_registry()
    models_payload = [
        {
            "name": e.name,
            "residence": e.residence,
            "description": e.description,
            "model_id": e.model_id,
            "repo_id": e.repo_id,
            "filename": e.filename,
        }
        for e in llm_registry.values()
    ]
    llama = find_installed(kodo_user_dir())
    active = LlamaServer.get_active_llama_server()
    llama_is_running = active is not None and active.is_running
    return {
        "models": models_payload,
        "llama_installed": llama is not None,
        "llama_version": f"b{llama.build}" if llama is not None else None,
        "llama_running": llama_is_running,
        "llama_model": active.model_name if llama_is_running and active is not None else None,
    }


# ------------------------------------------------------------------
# Session list / release
# ------------------------------------------------------------------


async def _handle_session_list(req: Request) -> None:
    await req.reply({"type": "session.list.ack", "sessions": req.manager.list_sessions()})


async def _handle_session_release(req: Request) -> None:
    if req.session_id:
        req.manager.release(req.session_id)
    await req.reply({"type": "session.release.ack"})


async def _handle_session_delete(req: Request) -> None:
    """Delete the session's files; on success close the socket, else reply error.

    The client reads a clean socket closure as confirmation (and closes the tab);
    on error it keeps the socket open and surfaces ``message``.
    """
    session = await _require_session(req)
    if session is None:
        return
    try:
        await req.manager.delete(req.session_id)
    except Exception as exc:  # noqa: BLE001 — any failure is reported to the client
        _log.exception("Failed to delete session %s", req.session_id)
        await req.reply({"type": "session.delete.error", "message": str(exc)})
        return
    # The session is gone: close the socket so the client treats the closure as
    # success. (drop_connection is a no-op now — delete() already detached it.)
    await req.connection.ws.close()


# ------------------------------------------------------------------
# Session-scoped engine handlers
# ------------------------------------------------------------------


async def _handle_prompt(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    text = str(req.env.payload.get("text", "")).strip()
    if not text:
        await req.reply(
            {
                "type": "error",
                "code": "empty_prompt",
                "message": "Prompt text is required.",
                "recoverable": True,
            }
        )
        return
    _log.info("Prompt submitted (session=%s): %r", session.id, text[:80])
    await req.reply({"type": "prompt.accepted"})
    await session.engine.handle_prompt_submit(text, req.env.id)


async def _handle_mode(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_mode_set(bool(req.env.payload.get("autonomous", False)))
    await req.reply({"type": "mode.accepted"})


async def _handle_workflow(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_workflow_set(str(req.env.payload.get("mode", "guided")))
    await req.reply({"type": "workflow.accepted"})


async def _handle_workspace_folders(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    physical_root = str(req.env.payload.get("physical_root", ""))
    raw = req.env.payload.get("folders", {})
    folders = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    await session.engine.handle_workspace_folders(physical_root, folders)
    await req.reply({"type": "workspace.folders.ack"})


async def _handle_project_set(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    root = str(req.env.payload.get("root", "")).strip()
    name = str(req.env.payload.get("name", "")).strip()
    if not root:
        await req.reply(
            {
                "type": "error",
                "code": "missing_project_root",
                "message": "project.set requires a 'root'.",
                "recoverable": True,
            }
        )
        return
    await session.engine.bind_project(root, name or root)
    await req.reply({"type": "project.accepted"})


async def _handle_stop(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.stop()
    await req.reply({"type": "stop.accepted"})


async def _handle_compact(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_compact_now()
    await req.reply({"type": "compact.accepted"})


def _make_config_reload_handler(config: Config) -> HandlerFn:
    async def _handle_config_reload(req: Request) -> None:
        try:
            config.reload_settings()
            # The model selection is window-global; notify every live session so
            # a switch to a smaller-context model can compact right away.
            for session in req.manager.live_sessions():
                await session.engine.handle_config_changed()
            await req.reply({"type": "config.reload.ack"})
        except Exception as exc:  # noqa: BLE001
            await req.reply(
                {
                    "type": "error",
                    "code": "config_reload_failed",
                    "message": str(exc),
                    "recoverable": True,
                }
            )

    return _handle_config_reload


# ------------------------------------------------------------------
# llama / model management (process-global; reply on the connection)
# ------------------------------------------------------------------


async def _handle_llamacpp_install(req: Request) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()

    def progress_cb(pct: int, msg: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, msg))

    async def run() -> None:
        try:
            await asyncio.to_thread(install_llamacpp, kodo_user_dir(), progress_cb=progress_cb)
        except Exception:  # noqa: BLE001
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(run())
    while True:
        item = await queue.get()
        if item is None:
            break
        pct, msg = item
        await req.connection.send(
            Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": pct, "message": msg})
        )


async def _handle_model_install(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        return
    registry = get_llm_registry()
    entry: LLMEntry | None = registry.get(name)
    if entry is None or entry.residence != "local":
        await req.connection.send(
            Envelope.make_event(
                EVT_MODEL_INSTALL_PROGRESS,
                {"name": name, "percent": -1, "message": f"Unknown local model: {name!r}"},
            )
        )
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()

    def progress_cb(pct: int, msg: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, msg))

    async def run() -> None:
        try:
            await asyncio.to_thread(download_model, entry, kodo_user_dir(), progress_cb=progress_cb)
        except Exception:  # noqa: BLE001
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(run())
    while True:
        item = await queue.get()
        if item is None:
            break
        pct, msg = item
        await req.connection.send(
            Envelope.make_event(
                EVT_MODEL_INSTALL_PROGRESS, {"name": name, "percent": pct, "message": msg}
            )
        )


def _make_llama_start_handler(config: Config) -> HandlerFn:
    async def _handle_llama_start(req: Request) -> None:
        user_dir = kodo_user_dir()
        settings = config.reload_settings()
        models_map = settings.get("models", {})
        model_name = str(models_map.get("local", "") if isinstance(models_map, dict) else "")
        if not model_name:
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE,
                    {"running": False, "model": None, "error": "No local model selected"},
                )
            )
            return
        registry = get_llm_registry()
        entry: LLMEntry | None = registry.get(model_name)
        llama_args = entry.llama_args if entry is not None else {}
        try:
            server = await ensure_llama_running(model_name, user_dir, llama_args=llama_args)
        except Exception as exc:  # noqa: BLE001
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": str(exc)}
                )
            )
            return
        await req.connection.send(
            Envelope.make_event(
                EVT_LLAMA_STATE, {"running": True, "model": server.model_name, "port": server.port}
            )
        )

    return _handle_llama_start


async def _handle_llama_stop(req: Request) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None:
        await server.stop()
    await req.connection.send(
        Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
    )


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------


async def _start_background(app: web.Application) -> None:
    user_dir = kodo_user_dir()
    running = find_running_server(user_dir)
    if running is not None:
        llama_install = find_installed(user_dir)
        model_path = get_model_path(running.model, user_dir) if running.model else None
        if llama_install is not None and model_path is not None:
            cfg = LlamaServerConfig(
                executable=llama_install.executable,
                model_path=model_path,
                kodo_dir=user_dir,
                model_name=running.model,
                host=running.host,
                port=running.port,
            )
            LlamaServer(cfg).adopt(running)


async def _stop_background(app: web.Application) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        await server.stop()
    await app[_MANAGER_KEY].shutdown()


async def _ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    return await request.app[CONNECTION_REGISTRY_KEY].run_ws(request)


def create_app(config: Config) -> web.Application:
    """Build and configure the singleton-server aiohttp application.

    Args:
        config: Resolved server configuration.

    Returns:
        web.Application: Ready-to-serve aiohttp application.
    """
    _check_git_on_path()

    layout = WorkspaceLayout()
    layout.init()
    _setup_log_file(layout, config.log_level)

    registry = AgentRegistry(_AGENTS_DIR)
    gateway = LLMGateway(
        cloud_concurrency=lambda: _cloud_concurrency(config),
    )
    manager = SessionManager(
        registry=registry,
        gateway=gateway,
        get_settings=config.reload_settings,
        layout=layout,
    )
    conn_registry = ConnectionRegistry(manager)

    conn_registry.register_handler(MSG_HELLO, _handle_hello)
    conn_registry.register_handler(MSG_PING, _handle_ping)
    conn_registry.register_handler(MSG_SESSION_LIST, _handle_session_list)
    conn_registry.register_handler(MSG_SESSION_RELEASE, _handle_session_release)
    conn_registry.register_handler(MSG_SESSION_DELETE, _handle_session_delete)
    conn_registry.register_handler(MSG_PROMPT_SUBMIT, _handle_prompt)
    conn_registry.register_handler(MSG_MODE_SET, _handle_mode)
    conn_registry.register_handler(MSG_WORKFLOW_SET, _handle_workflow)
    conn_registry.register_handler(MSG_WORKSPACE_FOLDERS, _handle_workspace_folders)
    conn_registry.register_handler(MSG_PROJECT_SET, _handle_project_set)
    conn_registry.register_handler(MSG_STOP, _handle_stop)
    conn_registry.register_handler(MSG_COMPACT_NOW, _handle_compact)
    conn_registry.register_handler(MSG_CONFIG_RELOAD, _make_config_reload_handler(config))
    conn_registry.register_handler(MSG_LLAMACPP_INSTALL, _handle_llamacpp_install)
    conn_registry.register_handler(MSG_MODEL_INSTALL, _handle_model_install)
    conn_registry.register_handler(MSG_LLAMA_START, _make_llama_start_handler(config))
    conn_registry.register_handler(MSG_LLAMA_STOP, _handle_llama_stop)

    app = web.Application()
    app[CONNECTION_REGISTRY_KEY] = conn_registry
    app[_MANAGER_KEY] = manager
    app.router.add_get("/ws", _ws_endpoint)
    app.on_startup.append(_start_background)
    app.on_shutdown.append(_stop_background)

    _log.info("Kōdo server %s — home=%s port=%d", _SERVER_VERSION, layout.kodo_dir, config.port)
    return app


def _cloud_concurrency(config: Config) -> int:
    raw = config.reload_settings().get("cloud_concurrency", 2)
    try:
        return max(1, int(str(raw)))
    except (TypeError, ValueError):
        return 2
