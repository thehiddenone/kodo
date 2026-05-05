"""Disconnect-tolerant outbound message queue."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ._envelope import Envelope

_log = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES: int = 50 * 1024 * 1024  # 50 MB


class Outbox:
    """Buffers outbound envelopes while the WebSocket client is disconnected.

    When the client reconnects, :meth:`drain_to` replays all buffered frames
    in arrival order.  If the buffer exceeds ``max_bytes``, overflow frames
    are silently dropped and an error is logged.

    All methods are safe to call from a single asyncio event loop.
    """

    __queue: list[str]
    __total_bytes: int
    __max_bytes: int
    __lock: asyncio.Lock

    def __init__(self, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        """Initialise the outbox.

        Args:
            max_bytes (int): Maximum buffer size in bytes. Defaults to 50 MB.
        """
        self.__queue = []
        self.__total_bytes = 0
        self.__max_bytes = max_bytes
        self.__lock = asyncio.Lock()

    @property
    def pending(self) -> int:
        """Number of envelopes currently buffered."""
        return len(self.__queue)

    async def enqueue(self, env: Envelope) -> None:
        """Buffer an envelope for later delivery.

        Args:
            env (Envelope): The envelope to buffer.
        """
        frame = env.to_json()
        size = len(frame.encode())
        async with self.__lock:
            if self.__total_bytes + size > self.__max_bytes:
                _log.error(
                    "Outbox overflow (%d bytes); dropping frame id=%s",
                    self.__total_bytes,
                    env.id,
                )
                return
            self.__queue.append(frame)
            self.__total_bytes += size

    async def drain_to(self, ws: web.WebSocketResponse) -> None:
        """Replay all buffered frames to a newly-connected WebSocket.

        Clears the buffer after sending.  If the send fails the frame is lost
        (best-effort semantics).

        Args:
            ws (web.WebSocketResponse): The active WebSocket connection.
        """
        async with self.__lock:
            frames = list(self.__queue)
            self.__queue.clear()
            self.__total_bytes = 0

        for frame in frames:
            await ws.send_str(frame)

        if frames:
            _log.info("Outbox: replayed %d frame(s) on reconnect", len(frames))

    async def send_or_buffer(
        self,
        env: Envelope,
        ws: web.WebSocketResponse | None,
    ) -> None:
        """Send immediately if connected, else buffer for later replay.

        Args:
            env (Envelope): The envelope to send.
            ws (web.WebSocketResponse | None): Active connection, or ``None``
                when the client is disconnected.
        """
        if ws is not None and not ws.closed:
            await ws.send_str(env.to_json())
        else:
            await self.enqueue(env)
