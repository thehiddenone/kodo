"""Approval gate orchestration for the Kōdo workflow engine.

A gate is a suspension point in the workflow where the developer must
either approve an artifact (``agree``) or request a revision
(``feedback``).  The engine ``await``s :meth:`GateOrchestrator.fire` and
resumes only when the extension sends ``approval.respond``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kodo.transport._envelope import Envelope
from kodo.transport._messages import EVT_APPROVAL_REQUEST

if TYPE_CHECKING:
    from kodo.transport._ws import AppState

__all__ = ["ApprovalResponse", "GateOrchestrator"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalResponse:
    """Developer response to an approval gate.

    Attributes:
        action: ``'agree'`` or ``'feedback'``.
        feedback: Free-form feedback text; empty when ``action == 'agree'``.
    """

    action: str
    feedback: str


def _gate_id(gate_type: str, component: str | None) -> str:
    raw = f"{gate_type}|{component or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class GateOrchestrator:
    """Manages the pending approval gate for the workflow engine.

    Only one gate may be pending at a time.  :meth:`fire` suspends the
    calling coroutine until :meth:`resolve` is called from the WebSocket
    handler with the developer's response.

    Args:
        app_state: WebSocket application state used to send the
            ``approval.request`` event.
    """

    def __init__(self, app_state: AppState) -> None:
        self.__app_state = app_state
        self.__pending_id: str | None = None
        self.__pending_future: asyncio.Future[ApprovalResponse] | None = None

    async def fire(
        self,
        gate_type: str,
        *,
        artifact_path: Path | None = None,
        summary: str = "",
        component: str | None = None,
    ) -> ApprovalResponse:
        """Emit an ``approval.request`` event and wait for the developer.

        Args:
            gate_type: Logical gate identifier (e.g. ``'narrative'``).
            artifact_path: Path to the artifact for reference in the UI.
            summary: One-paragraph summary shown to the developer.
            component: Component name for per-component gates.

        Returns:
            ApprovalResponse: The developer's action and optional feedback.
        """
        if self.__pending_id is not None:
            _log.warning("Gate already pending (%s) — overwriting", self.__pending_id)

        gate_id = _gate_id(gate_type, component)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[ApprovalResponse] = loop.create_future()
        self.__pending_id = gate_id
        self.__pending_future = future

        await self.__app_state.send(
            Envelope.make_event(
                EVT_APPROVAL_REQUEST,
                {
                    "gate_id": gate_id,
                    "gate_type": gate_type,
                    "artifact_path": artifact_path.as_posix() if artifact_path else None,
                    "summary": summary,
                    "component": component,
                },
            )
        )
        _log.info("Gate fired: type=%s id=%s", gate_type, gate_id)

        try:
            return await future
        finally:
            self.__pending_id = None
            self.__pending_future = None

    def resolve(self, gate_id: str, action: str, feedback: str) -> bool:
        """Resolve a pending gate with the developer's response.

        Args:
            gate_id: Must match the pending gate's ID.
            action: ``'agree'`` or ``'feedback'``.
            feedback: Feedback text; empty when ``action == 'agree'``.

        Returns:
            bool: ``True`` if the gate was resolved, ``False`` if the
            ``gate_id`` did not match any pending gate.
        """
        if self.__pending_id != gate_id or self.__pending_future is None:
            _log.warning(
                "Stale gate_id %r (pending: %r)", gate_id, self.__pending_id
            )
            return False
        if not self.__pending_future.done():
            self.__pending_future.set_result(
                ApprovalResponse(action=action, feedback=feedback)
            )
            _log.info("Gate resolved: id=%s action=%s", gate_id, action)
        return True
