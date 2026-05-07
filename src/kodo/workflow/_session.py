"""Per-session workflow metadata and resume detection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ._stages import Stage

__all__ = ["SessionState"]


@dataclass
class SessionState:
    """Mutable workflow state for one Kodo session.

    The workflow engine owns this object and updates it as stages progress.
    It is intentionally mutable (not frozen) because the engine writes it
    frequently.

    Attributes:
        session_id: Unique session identifier.
        stage: Current workflow stage.
        agent: Name of the currently active agent, if any.
        component: Component currently being processed, if any.
        autonomous: Whether autonomous mode is active.
    """

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stage: Stage = Stage.IDLE
    agent: str | None = None
    component: str | None = None
    autonomous: bool = False

    def to_dict(self) -> dict[str, object]:
        """Serialise to a plain dict for wire-protocol events.

        Returns:
            dict[str, object]: JSON-serialisable state snapshot.
        """
        return {
            "stage": self.stage.value,
            "agent": self.agent,
            "component": self.component,
            "autonomous": self.autonomous,
        }
