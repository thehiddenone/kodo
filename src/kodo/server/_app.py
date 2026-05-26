"""aiohttp application factory and WebSocket endpoint for the Kōdo server."""

from __future__ import annotations

import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

from aiohttp import web

from kodo.llms.anthropic import ClaudePlugin
from kodo.mirror._checkpoints import CheckpointManager
from kodo.project._layout import ProjectLayout, ProjectLayoutError
from kodo.state._transient import TransientStore, find_unfinished_session
from kodo.subagents._registry import AgentRegistry
from kodo.transport._envelope import Envelope
from kodo.transport._messages import (
    EVT_RESUME_OFFER,
    MSG_APPROVAL_RESPOND,
    MSG_HELLO,
    MSG_MODE_SET,
    MSG_PING,
    MSG_PROMPT_SUBMIT,
    MSG_SESSION_RESUME,
    MSG_STOP,
)
from kodo.transport._outbox import Outbox
from kodo.transport._ws import APP_STATE_KEY, AppState, HandlerFn
from kodo.workflow._engine import WorkflowEngine

from ._config import Config

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


def _make_hello_handler(
    config: Config, engine: WorkflowEngine, unfinished_session_id: str | None
) -> HandlerFn:
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
                "last_session": unfinished_session_id,
            },
        )
        await state.send(resp)

        state_evt = Envelope.make_event("state", engine.session.to_dict())
        await state.send(state_evt)

        if unfinished_session_id:
            await state.send(
                Envelope.make_event(
                    EVT_RESUME_OFFER,
                    {"session_id": unfinished_session_id},
                )
            )

    return _handle_hello


async def _handle_ping(state: AppState, env: Envelope) -> None:
    _log.debug("Ping id=%s", env.id)
    await state.send(Envelope.make_response(env.id, {"type": "pong"}))


def _make_prompt_handler(engine: WorkflowEngine) -> HandlerFn:
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
    async def _handle_mode(state: AppState, env: Envelope) -> None:
        autonomous = bool(env.payload.get("autonomous", False))
        await engine.handle_mode_set(autonomous)
        await state.send(Envelope.make_response(env.id, {"type": "mode.accepted"}))

    return _handle_mode


def _make_approval_handler(engine: WorkflowEngine) -> HandlerFn:
    """Return an ``approval.respond`` handler (FR-WF-05/06)."""

    async def _handle_approval(state: AppState, env: Envelope) -> None:
        gate_id = str(env.payload.get("gate_id", "")).strip()
        action = str(env.payload.get("action", "agree")).strip()
        feedback = str(env.payload.get("feedback", "")).strip()

        if not gate_id:
            await state.send(
                Envelope.make_response(
                    env.id,
                    {
                        "type": "error",
                        "code": "missing_gate_id",
                        "message": "gate_id is required.",
                        "recoverable": True,
                    },
                )
            )
            return

        resolved = engine.gate.resolve(gate_id, action, feedback)
        _log.info("approval.respond: gate_id=%s action=%s resolved=%s", gate_id, action, resolved)
        await state.send(Envelope.make_response(env.id, {"type": "approval.accepted"}))

    return _handle_approval


def _make_stop_handler(engine: WorkflowEngine) -> HandlerFn:
    """Return a ``stop`` handler (FR-WF-07)."""

    async def _handle_stop(state: AppState, env: Envelope) -> None:
        _log.info("Stop requested (id=%s)", env.id)
        await engine.stop()
        await state.send(Envelope.make_response(env.id, {"type": "stop.accepted"}))

    return _handle_stop


def _make_resume_handler(engine: WorkflowEngine) -> HandlerFn:
    """Return a ``session.resume`` handler (FR-STA-02)."""

    async def _handle_resume(state: AppState, env: Envelope) -> None:
        session_id = str(env.payload.get("session_id", "")).strip()
        _log.info("Resume requested: session_id=%s", session_id)
        await engine.handle_resume(session_id)
        await state.send(Envelope.make_response(env.id, {"type": "session.resume.accepted"}))

    return _handle_resume


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

    if not config.anthropic_api_key:
        _log.warning(
            "ANTHROPIC_API_KEY is not set — LLM calls will fail. "
            "Set the key in VS Code SecretStorage."
        )

    unfinished_session = find_unfinished_session(config.project)

    llm = ClaudePlugin(api_key=config.anthropic_api_key)
    transient = TransientStore(config.project)
    registry = AgentRegistry(_AGENTS_DIR)
    mirror = CheckpointManager(layout)

    outbox = Outbox()
    state = AppState(outbox)

    engine = WorkflowEngine(
        app_state=state,
        llm=llm,
        transient=transient,
        layout=layout,
        registry=registry,
        mirror=mirror,
        default_model=config.default_model,
    )

    state.register_handler(MSG_HELLO, _make_hello_handler(config, engine, unfinished_session))
    state.register_handler(MSG_PING, _handle_ping)
    state.register_handler(MSG_PROMPT_SUBMIT, _make_prompt_handler(engine))
    state.register_handler(MSG_MODE_SET, _make_mode_handler(engine))
    state.register_handler(MSG_APPROVAL_RESPOND, _make_approval_handler(engine))
    state.register_handler(MSG_STOP, _make_stop_handler(engine))
    state.register_handler(MSG_SESSION_RESUME, _make_resume_handler(engine))

    app = web.Application()
    app[APP_STATE_KEY] = state
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
