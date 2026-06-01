"""Per-session runtime metadata."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

__all__ = ["SessionState"]

# Valid phase values per WS_PROTOCOL.md §5.1
Phase = str  # "intake" | "running" | "awaiting_user" | "stopped" | "done" | "error"


@dataclass
class SessionState:
    """Mutable state for one Kodo session.

    The runtime engine owns this object and updates it as work progresses.
    It is intentionally mutable (not frozen) because the engine writes it
    frequently.

    Attributes:
        session_id: Unique session identifier.
        phase: Current wire-protocol phase (WS_PROTOCOL.md §5.1).
        agent: Name of the currently active sub-agent, if any.
        component: Responsibility code currently under work, if any.
        autonomous: Whether autonomous mode is active.
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: Phase = "intake"
    agent: str | None = None
    component: str | None = None
    autonomous: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for wire-protocol events.

        Returns:
            dict[str, object]: JSON-serialisable state snapshot.
        """
        return {
            "phase": self.phase,
            "current_agent": {"name": self.agent, "component": self.component}
            if self.agent
            else None,
            "autonomous": self.autonomous,
        }
