"""Wire-protocol envelope: ``{kind, id, correlation_id?, payload}``."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Literal, cast

__all__ = ["Envelope", "MessageKind"]

MessageKind = Literal["request", "response", "event", "stream_chunk", "thinking_chunk", "stream_end"]


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Envelope:
    """Single WebSocket frame — the atomic unit of the Kōdo wire protocol.

    Attributes:
        kind: Frame type discriminator.
        payload: Message-specific data.
        id: Unique frame identifier (hex UUID).
        correlation_id: ID of the request this frame responds to (optional).
    """

    kind: MessageKind
    payload: dict[str, object]
    id: str = field(default_factory=_new_id)
    correlation_id: str | None = None

    def to_json(self) -> str:
        """Serialise this envelope to a JSON string.

        Returns:
            str: JSON-encoded envelope.
        """
        d: dict[str, object] = {
            "kind": self.kind,
            "id": self.id,
            "payload": self.payload,
        }
        if self.correlation_id is not None:
            d["correlation_id"] = self.correlation_id
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        """Deserialise an envelope from a JSON string.

        Args:
            raw (str): JSON-encoded envelope.

        Returns:
            Envelope: Parsed envelope.

        Raises:
            KeyError: If required fields are absent.
            ValueError: If JSON is invalid.
        """
        d = cast(dict[str, object], json.loads(raw))
        return cls(
            kind=cast(MessageKind, d["kind"]),
            payload=cast(dict[str, object], d.get("payload", {})),
            id=str(d.get("id", _new_id())),
            correlation_id=str(d["correlation_id"]) if "correlation_id" in d else None,
        )

    @classmethod
    def make_response(cls, correlation_id: str, payload: dict[str, object]) -> Envelope:
        """Create a response envelope for a given request.

        Args:
            correlation_id (str): ID of the originating request.
            payload (dict[str, object]): Response payload.

        Returns:
            Envelope: A ``response`` envelope.
        """
        return cls(kind="response", payload=payload, correlation_id=correlation_id)

    @classmethod
    def make_event(cls, event_type: str, payload: dict[str, object]) -> Envelope:
        """Create an unsolicited server-push event envelope.

        Args:
            event_type (str): Event discriminator (e.g. ``'agent.started'``).
            payload (dict[str, object]): Event-specific fields.

        Returns:
            Envelope: An ``event`` envelope with ``payload.type`` set.
        """
        return cls(kind="event", payload={"type": event_type, **payload})

    @classmethod
    def make_stream_chunk(cls, correlation_id: str, text: str) -> Envelope:
        """Create a streaming-token chunk envelope.

        Args:
            correlation_id (str): Stream identifier.
            text (str): Token text fragment.

        Returns:
            Envelope: A ``stream_chunk`` envelope.
        """
        return cls(
            kind="stream_chunk",
            payload={"type": "agent.tokens", "text": text},
            correlation_id=correlation_id,
        )

    @classmethod
    def make_thinking_chunk(cls, correlation_id: str, text: str) -> Envelope:
        """Create a thinking-token chunk envelope.

        Args:
            correlation_id (str): Stream identifier.
            text (str): Thinking text fragment.

        Returns:
            Envelope: A ``thinking_chunk`` envelope.
        """
        return cls(
            kind="thinking_chunk",
            payload={"type": "agent.thinking", "text": text},
            correlation_id=correlation_id,
        )

    @classmethod
    def make_stream_end(cls, correlation_id: str) -> Envelope:
        """Create a stream terminator envelope.

        Args:
            correlation_id (str): Stream identifier matching the prior chunks.

        Returns:
            Envelope: A ``stream_end`` envelope.
        """
        return cls(kind="stream_end", payload={}, correlation_id=correlation_id)
