"""aiohttp application factory and WebSocket endpoint for the Kōdo server."""

from __future__ import annotations

import asyncio
import logging
import uuid

from typing import cast

from aiohttp import web

from kodo.transport._envelope import Envelope
from kodo.transport._messages import EVT_STATE, MSG_HELLO, MSG_PING
from kodo.transport._outbox import Outbox
from kodo.transport._ws import AppState, HandlerFn

from ._config import Config

_log = logging.getLogger(__name__)

_SERVER_VERSION: str = "0.1.0b1"

# M1 demo fake-stream parameters
_FAKE_CHUNKS: int = 200
_FAKE_CHUNK_DELAY: float = 0.03  # seconds between chunks
_FAKE_WORDS: str = (
    "Kōdo is ready. This is a demo token stream verifying that the "
    "WebSocket connection and streaming pipeline work end-to-end. "
)


def _make_hello_handler(config: Config) -> HandlerFn:
    """Return a ``hello`` message handler closed over ``config``.

    Args:
        config (Config): Resolved server configuration.

    Returns:
        HandlerFn: Async handler for ``hello`` messages.
    """

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

        state_evt = Envelope.make_event(
            EVT_STATE,
            {
                "stage": "IDLE",
                "agent": None,
                "component": None,
                "autonomous": False,
            },
        )
        await state.send(state_evt)

        asyncio.create_task(_fake_stream(state))

    return _handle_hello


async def _handle_ping(state: AppState, env: Envelope) -> None:
    _log.debug("Ping id=%s", env.id)
    await state.send(Envelope.make_response(env.id, {"type": "pong"}))


async def _fake_stream(state: AppState) -> None:
    """Emit a 200-chunk demo token stream for the M1 observable demo."""
    stream_id = uuid.uuid4().hex
    await asyncio.sleep(0.5)

    word_list = _FAKE_WORDS.split()
    chunks: list[str] = []
    while len(chunks) < _FAKE_CHUNKS:
        chunks.extend(w + " " for w in word_list)
    chunks = chunks[:_FAKE_CHUNKS]

    for chunk in chunks:
        await state.send(Envelope.make_stream_chunk(stream_id, chunk))
        await asyncio.sleep(_FAKE_CHUNK_DELAY)

    await state.send(Envelope.make_stream_end(stream_id))
    _log.info("Fake stream complete (%d chunks)", _FAKE_CHUNKS)


async def _ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    state = cast(AppState, request.app["state"])
    return await state.run_ws(request)


def create_app(config: Config) -> web.Application:
    """Build and configure the aiohttp application.

    Args:
        config (Config): Resolved server configuration.

    Returns:
        web.Application: Ready-to-serve aiohttp application.
    """
    outbox = Outbox()
    state = AppState(outbox)

    state.register_handler(MSG_HELLO, _make_hello_handler(config))
    state.register_handler(MSG_PING, _handle_ping)

    app = web.Application()
    app["state"] = state
    app.router.add_get("/ws", _ws_endpoint)

    _log.info(
        "Kōdo server %s — project=%s port=%d",
        _SERVER_VERSION,
        config.project,
        config.port,
    )
    return app
