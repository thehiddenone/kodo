"""Approval-gate and user-question orchestration (WS_PROTOCOL.md §6).

Server-initiated user interactions use ``kind=request`` frames so the
client's reply is a ``kind=response`` correlated by ``id``.

- :meth:`GateOrchestrator.fire_approval` — surfaces an approval gate
  (``prompt.approval``, FR-WF-05) and blocks until the user responds.
- :meth:`GateOrchestrator.fire_question` — surfaces a free-form or choice
  question (``prompt.question``, WS_PROTOCOL.md §6.1) and blocks until
  the user responds.

Both methods register a :class:`asyncio.Future` via
:meth:`~kodo.transport.AppState.register_response_future` and await it.
The WS dispatcher resolves the future when the matching ``kind=response``
arrives.

``fire()`` is an alias for ``fire_approval()`` kept for call-site
compatibility inside :class:`~kodo.runtime._tool_surface.ToolSurface`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from kodo.common import Envelope
from kodo.state import TransientStore
from kodo.transport import SREQ_PROMPT_APPROVAL, SREQ_PROMPT_QUESTION, WebSocketDispatcher

__all__ = ["ApprovalResponse", "GateOrchestrator", "QuestionResponse"]

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


@dataclass(frozen=True)
class QuestionResponse:
    """Developer response to a user question.

    Attributes:
        answer_text: Free-text answer (mode='free_text').
        choice_key: Selected choice key (mode='choice').
    """

    answer_text: str
    choice_key: str


class GateOrchestrator:
    """Manages server-initiated ``kind=request`` prompts.

    Both :meth:`fire_approval` and :meth:`fire_question` emit a
    ``kind=request`` envelope, register a :class:`asyncio.Future` for the
    response, and ``await`` it.  Resolution happens in the WS dispatcher
    when the client sends the matching ``kind=response``.

    Multiple prompts MAY be outstanding simultaneously; each is tracked by
    its own ``request_id``.

    Args:
        app_state: WebSocket application state used to send frames and
            register response futures.
        transient: Session store used to persist the outstanding prompt so
            it can be re-surfaced if the server restarts before the user
            responds.
    """

    def __init__(self, app_state: WebSocketDispatcher, transient: TransientStore) -> None:
        """Initialise with the application state.

        Args:
            app_state (AppState): WebSocket application state.
            transient (TransientStore): Session store for pending-prompt persistence.
        """
        self.__app_state = app_state
        self.__transient = transient

    async def fire_approval(
        self,
        gate_type: str,
        *,
        artifact_id: str | None = None,
        summary: str = "",
        component: str | None = None,
    ) -> ApprovalResponse:
        """Emit a ``prompt.approval`` ``kind=request`` and block until the
        user responds.

        Args:
            gate_type: Gate type label (e.g. ``'narrative'``).
            artifact_id: ID of the artifact the user should review.
            summary: One-paragraph summary shown to the user.
            component: Unused; kept for call-site compatibility.

        Returns:
            ApprovalResponse: The user's action and optional feedback.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.__app_state.register_response_future(req_id, future)

        self.__transient.update(
            pending_prompt={
                "kind": "approval",
                "gate_type": gate_type,
                "artifact_id": artifact_id,
                "summary": summary,
            }
        )
        try:
            await self.__app_state.send(
                Envelope(
                    kind="request",
                    id=req_id,
                    payload={
                        "type": SREQ_PROMPT_APPROVAL,
                        "gate_type": gate_type,
                        "artifact_id": artifact_id,
                        "summary": summary,
                    },
                )
            )
            _log.info("Approval gate fired: type=%s req_id=%s", gate_type, req_id[:8])

            response_payload = await future
            action = str(response_payload.get("action", "agree"))
            feedback = str(response_payload.get("feedback_text") or "")
            _log.info("Approval gate resolved: req_id=%s action=%s", req_id[:8], action)
            return ApprovalResponse(action=action, feedback=feedback)
        finally:
            self.__transient.update(pending_prompt=None)

    async def fire_question(
        self,
        question: str,
        mode: str,
        choices: list[dict[str, str]] | None = None,
    ) -> QuestionResponse:
        """Emit a ``prompt.question`` ``kind=request`` and block until the
        user responds.

        Args:
            question: The question text to display.
            mode: ``'free_text'`` or ``'choice'``.
            choices: Required when ``mode='choice'``; list of
                ``{'key': str, 'label': str}`` dicts.

        Returns:
            QuestionResponse: The user's answer or choice key.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.__app_state.register_response_future(req_id, future)

        payload: dict[str, object] = {
            "type": SREQ_PROMPT_QUESTION,
            "question": question,
            "mode": mode,
        }
        if choices:
            payload["choices"] = choices

        self.__transient.update(
            pending_prompt={
                "kind": "question",
                "question": question,
                "mode": mode,
                "choices": choices,
            }
        )
        try:
            await self.__app_state.send(Envelope(kind="request", id=req_id, payload=payload))
            _log.info("Question fired: mode=%s req_id=%s", mode, req_id[:8])

            response_payload = await future
            answer_text = str(response_payload.get("answer_text") or "")
            choice_key = str(response_payload.get("choice_key") or "")
            _log.info("Question resolved: req_id=%s", req_id[:8])
            return QuestionResponse(answer_text=answer_text, choice_key=choice_key)
        finally:
            self.__transient.update(pending_prompt=None)

    # Alias so ToolSurface and existing call sites use fire() unchanged.
    fire = fire_approval
