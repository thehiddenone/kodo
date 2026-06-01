"""Shared protocols and data types for decoupling consumers from implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._envelope import Envelope

__all__ = ["ApiKey", "ApiKeyProvider", "MessageSink"]


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
