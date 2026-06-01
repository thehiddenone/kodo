"""Common shared types and protocols for Kōdo — no intra-kodo dependencies."""

from ._envelope import Envelope, MessageKind
from ._protocols import ApiKey, ApiKeyProvider, MessageSink

__all__ = [
    "Envelope",
    "MessageKind",
    "ApiKey",
    "ApiKeyProvider",
    "MessageSink",
]
