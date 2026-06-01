"""aiohttp application factory and WebSocket endpoint for the Kōdo server."""

from __future__ import annotations

import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from aiohttp import web

from kodo.common import Envelope
from kodo.mirror._checkpoints import CheckpointManager
from kodo.project._layout import ProjectLayout, ProjectLayoutError
from kodo.runtime._engine import WorkflowEngine
from kodo.runtime._gates import GateOrchestrator
from kodo.state._transient import TransientStore
from kodo.subagents._registry import AgentRegistry
from kodo.transport import (
    MSG_CONFIG_RELOAD,
    MSG_HELLO,
    MSG_MODE_SET,
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

        resp = Envelope.make_response(
            env.id,
            {
                "type": "hello.ack",
                "server_version": _SERVER_VERSION,
                "project_root": str(config.project),
                "state": engine.session.to_dict(),
            },
        )
        await state.send(resp)

        state_evt = Envelope.make_event("state", engine.session.to_dict())
        await state.send(state_evt)

    return _handle_hello


async def _handle_ping(state: WebSocketDispatcher, env: Envelope) -> None:
    _log.debug("Ping id=%s", env.id)
    await state.send(Envelope.make_response(env.id, {"type": "pong"}))


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


async def _stop_background(app: web.Application) -> None:
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
    gate = GateOrchestrator(dispatcher)

    transient = TransientStore(config.project)
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
