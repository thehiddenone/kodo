"""aiohttp application factory and WebSocket endpoint for the Kōdo server."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from aiohttp import web

from kodo.common import Envelope
from kodo.llm_utils import (
    LlamaServer,
    LlamaServerConfig,
    download_model,
    ensure_llama_running,
    find_installed,
    find_running_server,
    get_model_path,
    install_llamacpp,
)
from kodo.llms import LLMEntry, get_llm_registry
from kodo.mirror._checkpoints import CheckpointManager
from kodo.project import ProjectLayout, ProjectLayoutError, kodo_user_dir
from kodo.runtime._engine import WorkflowEngine
from kodo.runtime._gates import GateOrchestrator
from kodo.state._transient import TransientStore
from kodo.subagents._registry import AgentRegistry
from kodo.transport import (
    EVT_LLAMA_STATE,
    EVT_LLAMACPP_INSTALL_PROGRESS,
    EVT_MODEL_INSTALL_PROGRESS,
    MSG_CONFIG_RELOAD,
    MSG_HELLO,
    MSG_LLAMA_START,
    MSG_LLAMA_STOP,
    MSG_LLAMACPP_INSTALL,
    MSG_MODE_SET,
    MSG_MODEL_INSTALL,
    MSG_PING,
    MSG_PROMPT_SUBMIT,
    MSG_STOP,
)
from kodo.transport._outbox import Outbox
from kodo.transport._ws import APP_STATE_KEY, HandlerFn, WebSocketDispatcher

from ._config import Config
from ._key_broker import KeyBroker

_log = logging.getLogger(__name__)

_SERVER_VERSION: str = "0.1.0b1"
_ENGINE_KEY: web.AppKey[WorkflowEngine] = web.AppKey("engine")

# Subagents directory: kodo/subagents/ next to kodo/server/
_AGENTS_DIR = Path(__file__).parent.parent / "subagents"


# ------------------------------------------------------------------
# Startup validation (FR-SRV-05)
# ------------------------------------------------------------------


def _check_git_on_path() -> None:
    if shutil.which("git") is None:
        _log.error("'git' is not on PATH.  Kōdo requires git.")
        sys.exit(1)


# ------------------------------------------------------------------
# Logging setup (NFR-05)
# ------------------------------------------------------------------


def _setup_log_file(layout: ProjectLayout, log_level: str) -> None:
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
# Message handlers
# ------------------------------------------------------------------


def _make_hello_handler(config: Config, engine: WorkflowEngine) -> HandlerFn:
    async def _handle_hello(state: WebSocketDispatcher, env: Envelope) -> None:
        payload = env.payload
        client = str(payload.get("client", "unknown"))
        version = str(payload.get("version", "unknown"))
        _log.info("Hello from client=%s version=%s", client, version)

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
        resp = Envelope.make_response(
            env.id,
            {
                "type": "hello.ack",
                "server_version": _SERVER_VERSION,
                "project_root": str(config.project),
                "state": engine.session.to_dict(),
                "models": models_payload,
                "llama_installed": llama is not None,
                "llama_version": f"b{llama.build}" if llama is not None else None,
                "llama_running": llama_is_running,
                "llama_model": active.model_name
                if llama_is_running and active is not None
                else None,
            },
        )
        await state.send(resp)

        state_evt = Envelope.make_event("state", engine.session.to_dict())
        await state.send(state_evt)

        history = engine.history_entries()
        if history:
            await state.send(Envelope.make_event("session.history", {"entries": history}))

    return _handle_hello


async def _handle_ping(state: WebSocketDispatcher, env: Envelope) -> None:
    _log.debug("Ping id=%s", env.id)
    await state.send(Envelope.make_response(env.id, {"type": "pong"}))


async def _handle_llamacpp_install(state: WebSocketDispatcher, _env: Envelope) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()

    def progress_cb(pct: int, msg: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, msg))

    async def run() -> None:
        try:
            await asyncio.to_thread(install_llamacpp, kodo_user_dir(), progress_cb=progress_cb)
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(run())

    while True:
        item = await queue.get()
        if item is None:
            break
        pct, msg = item
        await state.send(
            Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": pct, "message": msg})
        )


async def _handle_model_install(state: WebSocketDispatcher, env: Envelope) -> None:
    name = str(env.payload.get("name", "")).strip()
    if not name:
        return

    registry = get_llm_registry()
    entry: LLMEntry | None = registry.get(name)
    if entry is None or entry.residence != "local":
        await state.send(
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
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(run())

    while True:
        item = await queue.get()
        if item is None:
            break
        pct, msg = item
        await state.send(
            Envelope.make_event(
                EVT_MODEL_INSTALL_PROGRESS, {"name": name, "percent": pct, "message": msg}
            )
        )


def _make_llama_start_handler(config: Config) -> HandlerFn:
    async def _handle_llama_start(state: WebSocketDispatcher, _env: Envelope) -> None:
        user_dir = kodo_user_dir()

        settings = config.reload_settings()
        models_map = settings.get("models", {})
        model_name = str(models_map.get("local", "") if isinstance(models_map, dict) else "")
        if not model_name:
            await state.send(
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
        except Exception as exc:
            await state.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": str(exc)}
                )
            )
            return

        await state.send(
            Envelope.make_event(
                EVT_LLAMA_STATE,
                {"running": True, "model": server.model_name, "port": server.port},
            )
        )

    return _handle_llama_start


async def _handle_llama_stop(state: WebSocketDispatcher, _env: Envelope) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None:
        await server.stop()
    await state.send(Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None}))


def _make_prompt_handler(engine: WorkflowEngine) -> HandlerFn:
    async def _handle_prompt(state: WebSocketDispatcher, env: Envelope) -> None:
        text = str(env.payload.get("text", "")).strip()
        if not text:
            await state.send(
                Envelope.make_response(
                    env.id,
                    {
                        "type": "error",
                        "code": "empty_prompt",
                        "message": "Prompt text is required.",
                        "recoverable": True,
                    },
                )
            )
            return

        _log.info("Prompt submitted: %r (id=%s)", text[:80], env.id)
        await state.send(Envelope.make_response(env.id, {"type": "prompt.accepted"}))
        await engine.handle_prompt_submit(text, env.id)

    return _handle_prompt


def _make_mode_handler(engine: WorkflowEngine) -> HandlerFn:
    async def _handle_mode(state: WebSocketDispatcher, env: Envelope) -> None:
        autonomous = bool(env.payload.get("autonomous", False))
        await engine.handle_mode_set(autonomous)
        await state.send(Envelope.make_response(env.id, {"type": "mode.accepted"}))

    return _handle_mode


def _make_config_reload_handler(config: Config) -> HandlerFn:
    async def _handle_config_reload(state: WebSocketDispatcher, env: Envelope) -> None:
        # Validate the settings file is still parseable; the engine reads
        # fresh settings on each dispatch so no further action is needed.
        try:
            config.reload_settings()
            _log.info("Config reload acknowledged — new settings apply to next dispatch")
            await state.send(Envelope.make_response(env.id, {"type": "config.reload.ack"}))
        except Exception as exc:
            _log.warning("Config reload failed: %s", exc)
            await state.send(
                Envelope.make_response(
                    env.id,
                    {
                        "type": "error",
                        "code": "config_reload_failed",
                        "message": str(exc),
                        "recoverable": True,
                    },
                )
            )

    return _handle_config_reload


def _make_stop_handler(engine: WorkflowEngine) -> HandlerFn:
    async def _handle_stop(state: WebSocketDispatcher, env: Envelope) -> None:
        _log.info("Stop requested (id=%s)", env.id)
        await engine.stop()
        await state.send(Envelope.make_response(env.id, {"type": "stop.accepted"}))

    return _handle_stop


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------


async def _start_background(app: web.Application) -> None:
    await app[_ENGINE_KEY].start()

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
            server = LlamaServer(cfg)
            server.adopt(running)
        else:
            _log.warning(
                "Detected running llama-server pid=%d but cannot reconstruct config "
                "(install=%s model_path=%s) — treating as unmanaged",
                running.pid,
                llama_install,
                model_path,
            )


async def _stop_background(app: web.Application) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        await server.stop()
    await app[_ENGINE_KEY].stop()


async def _ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    return await request.app[APP_STATE_KEY].run_ws(request)


def create_app(config: Config) -> web.Application:
    """Build and configure the aiohttp application.

    Args:
        config: Resolved server configuration.

    Returns:
        web.Application: Ready-to-serve aiohttp application.

    Raises:
        SystemExit: If git is absent from PATH or the project layout is invalid.
    """
    _check_git_on_path()

    layout = ProjectLayout(config.project)
    _setup_log_file(layout, config.log_level)

    try:
        layout.validate()
    except ProjectLayoutError as exc:
        _log.warning("Project layout warning: %s", exc)

    outbox = Outbox()
    dispatcher = WebSocketDispatcher(outbox)
    key_broker = KeyBroker(dispatcher)
    transient = TransientStore(layout.kodo_dir)
    gate = GateOrchestrator(dispatcher, transient)

    registry = AgentRegistry(_AGENTS_DIR)
    mirror = CheckpointManager(layout)

    engine = WorkflowEngine(
        sink=dispatcher,
        gate=gate,
        key_provider=key_broker,
        get_settings=config.reload_settings,
        transient=transient,
        layout=layout,
        registry=registry,
        mirror=mirror,
    )

    dispatcher.register_handler(MSG_HELLO, _make_hello_handler(config, engine))
    dispatcher.register_handler(MSG_PING, _handle_ping)
    dispatcher.register_handler(MSG_LLAMACPP_INSTALL, _handle_llamacpp_install)
    dispatcher.register_handler(MSG_MODEL_INSTALL, _handle_model_install)
    dispatcher.register_handler(MSG_LLAMA_START, _make_llama_start_handler(config))
    dispatcher.register_handler(MSG_LLAMA_STOP, _handle_llama_stop)
    dispatcher.register_handler(MSG_PROMPT_SUBMIT, _make_prompt_handler(engine))
    dispatcher.register_handler(MSG_MODE_SET, _make_mode_handler(engine))
    dispatcher.register_handler(MSG_STOP, _make_stop_handler(engine))
    dispatcher.register_handler(MSG_CONFIG_RELOAD, _make_config_reload_handler(config))

    app = web.Application()
    app[APP_STATE_KEY] = dispatcher
    app[_ENGINE_KEY] = engine
    app.router.add_get("/ws", _ws_endpoint)
    app.on_startup.append(_start_background)
    app.on_shutdown.append(_stop_background)

    _log.info(
        "Kōdo server %s — project=%s port=%d session=%s",
        _SERVER_VERSION,
        config.project,
        config.port,
        transient.session_id,
    )
    return app
