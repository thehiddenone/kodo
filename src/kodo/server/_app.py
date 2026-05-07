"""aiohttp application factory and WebSocket endpoint for the Kōdo server."""

from __future__ import annotations

import logging
import logging.handlers
import shutil
import sys
from typing import cast

from aiohttp import web

from kodo.llms.anthropic import ClaudePlugin
from kodo.project._layout import ProjectLayout, ProjectLayoutError
from kodo.state._transient import TransientStore
from kodo.transport._envelope import Envelope
from kodo.transport._messages import MSG_HELLO, MSG_MODE_SET, MSG_PING, MSG_PROMPT_SUBMIT
from kodo.transport._outbox import Outbox
from kodo.transport._ws import AppState, HandlerFn
from kodo.workflow._engine import WorkflowEngine

from ._config import Config

_log = logging.getLogger(__name__)

_SERVER_VERSION: str = "0.1.0b1"


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
    """Return a ``hello`` handler closed over the runtime objects."""

    async def _handle_hello(state: AppState, env: Envelope) -> None:
        payload = env.payload
        client = str(payload.get("client", "unknown"))
        version = str(payload.get("version", "unknown"))
        _log.info("Hello from client=%s version=%s", client, version)

        resp = Envelope.make_response(
            env.id,
            {
                "type": "hello",
                "server_version": _SERVER_VERSION,
                "project_root": str(config.project),
                "last_session": None,
            },
        )
        await state.send(resp)

        # Push current workflow state immediately after handshake
        state_evt = Envelope.make_event("state", engine.session.to_dict())
        await state.send(state_evt)

    return _handle_hello


async def _handle_ping(state: AppState, env: Envelope) -> None:
    _log.debug("Ping id=%s", env.id)
    await state.send(Envelope.make_response(env.id, {"type": "pong"}))


def _make_prompt_handler(engine: WorkflowEngine) -> HandlerFn:
    """Return a ``prompt.submit`` handler."""

    async def _handle_prompt(state: AppState, env: Envelope) -> None:
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
    """Return a ``mode.set`` handler."""

    async def _handle_mode(state: AppState, env: Envelope) -> None:
        autonomous = bool(env.payload.get("autonomous", False))
        await engine.handle_mode_set(autonomous)
        await state.send(Envelope.make_response(env.id, {"type": "mode.accepted"}))

    return _handle_mode


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------

async def _start_background(app: web.Application) -> None:
    engine: WorkflowEngine = cast(WorkflowEngine, app["engine"])
    await engine.start()


async def _stop_background(app: web.Application) -> None:
    engine: WorkflowEngine = cast(WorkflowEngine, app["engine"])
    await engine.stop()


async def _ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    state = cast(AppState, request.app["state"])
    return await state.run_ws(request)


def create_app(config: Config) -> web.Application:
    """Build and configure the aiohttp application.

    Validates the project layout, checks git is on PATH, creates the LLM
    plugin, transient store, and workflow engine, then registers all message
    handlers.

    Args:
        config (Config): Resolved server configuration.

    Returns:
        web.Application: Ready-to-serve aiohttp application.

    Raises:
        SystemExit: If git is absent from PATH or the project layout is
            invalid.
    """
    _check_git_on_path()

    # Validate or soft-warn about project layout
    layout = ProjectLayout(config.project)
    _setup_log_file(layout, config.log_level)

    try:
        layout.validate()
    except ProjectLayoutError as exc:
        # Not fatal at server start — the Dev may run Init Project next
        _log.warning("Project layout warning: %s", exc)

    # Build the plugin stack
    if not config.anthropic_api_key:
        _log.warning(
            "ANTHROPIC_API_KEY is not set — LLM calls will fail. "
            "Set the key in VS Code SecretStorage."
        )

    llm = ClaudePlugin(api_key=config.anthropic_api_key)
    transient = TransientStore(config.project)

    outbox = Outbox()
    state = AppState(outbox)

    engine = WorkflowEngine(
        app_state=state,
        llm=llm,
        transient=transient,
    )

    state.register_handler(MSG_HELLO, _make_hello_handler(config, engine))
    state.register_handler(MSG_PING, _handle_ping)
    state.register_handler(MSG_PROMPT_SUBMIT, _make_prompt_handler(engine))
    state.register_handler(MSG_MODE_SET, _make_mode_handler(engine))

    app = web.Application()
    app["state"] = state
    app["engine"] = engine
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
