"""KeyBroker — requests API keys from the connected VSIX client.

Keys are never stored; each resolved key lives only inside the
:class:`~kodo.llms.anthropic.ClaudePlugin` instance for that session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from kodo.common import ApiKey, Envelope, ResponseChannel
from kodo.transport import SREQ_API_KEY_REQUEST

__all__ = ["KeyBroker"]

_log = logging.getLogger(__name__)


class KeyBroker:
    """Requests API keys from one session's VSIX window over the WebSocket.

    Sends a ``kind=request`` frame with ``type=api_key.request`` and blocks
    until the client responds with a ``kind=response``.  If the WebSocket
    disconnects while waiting, the pending future is cancelled by the session's
    channel and this method raises :class:`asyncio.CancelledError`, which the
    caller surfaces as a key-request failure.  Keys are per session — each
    session asks its own window.

    Args:
        channel: The session's response channel.
    """

    __channel: ResponseChannel

    def __init__(self, channel: ResponseChannel) -> None:
        """Initialise the broker with the session's response channel.

        Args:
            channel (ResponseChannel): The session's response channel.
        """
        self.__channel = channel

    async def get_key(self, vendor: str) -> ApiKey:
        """Request the API key for *vendor* from the VSIX client.

        Blocks indefinitely until the client responds or the connection drops.
        If the user cancels the key-entry dialog, the client sends a response
        with ``error`` set and this method returns an :class:`ApiKey` with
        ``error`` populated.

        Args:
            vendor (str): Vendor identifier (e.g. ``'anthropic'``).

        Returns:
            ApiKey: Key result; check ``error`` before using ``api_key``.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.__channel.register_response_future(req_id, future)

        await self.__channel.send(
            Envelope(
                kind="request",
                id=req_id,
                payload={"type": SREQ_API_KEY_REQUEST, "vendor": vendor},
            )
        )
        _log.info("API key requested for vendor=%r (req_id=%s)", vendor, req_id[:8])

        try:
            payload = await future
        except asyncio.CancelledError:
            _log.warning("API key request cancelled (WebSocket disconnected) for vendor=%r", vendor)
            return ApiKey(vendor=vendor, api_key="", error="connection_lost")

        error = payload.get("error")
        if error:
            _log.info("API key rejected by client for vendor=%r: %s", vendor, error)
            return ApiKey(vendor=vendor, api_key="", error=str(error))

        api_key = str(payload.get("api_key", ""))
        _log.info("API key received for vendor=%r", vendor)
        return ApiKey(vendor=vendor, api_key=api_key)
