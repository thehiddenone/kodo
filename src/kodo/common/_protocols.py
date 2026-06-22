"""Shared protocols and data types for decoupling consumers from implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from ._envelope import Envelope

__all__ = ["ApiKey", "ApiKeyProvider", "MessageSink", "ResponseChannel"]


@dataclass(frozen=True)
class ApiKey:
    """Result of an API key request.

    Attributes:
        vendor: Vendor identifier (e.g. ``'anthropic'``).
        api_key: The key value; empty when ``error`` is set.
        error: Non-``None`` when the request was rejected or cancelled.
    """

    vendor: str
    api_key: str
    error: str | None = None


class MessageSink(Protocol):
    """Accepts outbound envelopes for delivery to the connected client."""

    async def send(self, env: Envelope) -> None:
        """Send an envelope to the client.

        Args:
            env (Envelope): The envelope to deliver.
        """
        ...


class ResponseChannel(Protocol):
    """A per-session channel for server-initiated request/response round-trips.

    Used by the key broker and gate orchestrator: send a ``kind=request`` frame
    to the session's window and register a future to be resolved when the
    matching ``kind=response`` arrives.  Satisfied by the per-session transport
    channel.
    """

    async def send(self, env: Envelope) -> None: ...

    def register_response_future(
        self, request_id: str, future: asyncio.Future[dict[str, object]]
    ) -> None: ...


class ApiKeyProvider(Protocol):
    """Requests API keys from the connected client (e.g. VSIX SecretStorage)."""

    async def get_key(self, vendor: str) -> ApiKey:
        """Request the API key for *vendor* from the client.

        Blocks until the client responds.  Returns an :class:`ApiKey` with
        ``error`` set if the user cancelled or the connection dropped.

        Args:
            vendor (str): Vendor identifier (e.g. ``'anthropic'``).

        Returns:
            ApiKey: Key result; check ``error`` before using ``api_key``.
        """
        ...
