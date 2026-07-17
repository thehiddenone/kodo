"""Approval-gate and user-question orchestration (WS_PROTOCOL.md §6).

Server-initiated user interactions use ``kind=request`` frames so the
client's reply is a ``kind=response`` correlated by ``id``.

- :meth:`GateOrchestrator.fire_approval` — surfaces an approval gate
  (``prompt.approval``, FR-WF-05) and blocks until the user responds.
- :meth:`GateOrchestrator.fire_questions` — surfaces one ``ask_user``
  question batch (``prompt.question``, WS_PROTOCOL.md §6.1) and blocks
  until the user confirms answers to all of them.
- :meth:`GateOrchestrator.fire_permission` — surfaces a security-layer
  permission prompt (``prompt.permission``, WS_PROTOCOL.md §6.5) and blocks
  until the user allows or denies the tool call.

All three register a :class:`asyncio.Future` via
:meth:`~kodo.transport.SessionChannel.register_response_future` and await it.
The connection registry resolves the future when the matching
``kind=response`` arrives. The future (and the request envelope that fired
it) is session-scoped, not tied to whichever socket was live at the time —
see :mod:`kodo.transport._connection` — so a client disconnect/reconnect
never loses one of these waits; only genuine session teardown does.

``fire()`` is an alias for ``fire_approval()`` kept for call-site
compatibility. :class:`GateOrchestrator` satisfies the ``GateLike`` protocol
in :mod:`kodo.tools`, through which the tool handlers reach it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from kodo.common import Envelope, ResponseChannel
from kodo.state import TransientStore
from kodo.tools import PermissionPartLike
from kodo.transport import SREQ_PROMPT_APPROVAL, SREQ_PROMPT_PERMISSION, SREQ_PROMPT_QUESTION

__all__ = ["ApprovalResponse", "GateOrchestrator", "PermissionResponse"]

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
class PermissionResponse:
    """Developer response to a security permission prompt.

    Attributes:
        action: ``'allow'`` or ``'deny'``.
        feedback: Optional free-text the user attached to the decision
            (returned to the agent verbatim on a denial).
        remember: One entry per part the server offered (parallel to the
            ``parts`` passed into :meth:`fire_permission`): ``'session'`` /
            ``'global'`` where the user chose to permanently allow that
            part's offered rule shape, else ``None``
            (doc/SECURITY_RULES_PLAN.md §2.6). Only acted on by
            ``ToolDispatcher`` for a part that actually carried a
            ``rule_offer`` — never trusted from the wire alone.
    """

    action: str
    feedback: str
    remember: tuple[str | None, ...] = ()


class GateOrchestrator:
    """Manages server-initiated ``kind=request`` prompts.

    Both :meth:`fire_approval` and :meth:`fire_questions` emit a
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

    def __init__(self, app_state: ResponseChannel, transient: TransientStore) -> None:
        """Initialise with the application state.

        Args:
            app_state (ResponseChannel): The session's response channel
                (:class:`~kodo.transport.SessionChannel` in production).
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
            self.__transient.update(pending_prompt=None)
            return ApprovalResponse(action=action, feedback=feedback)
        except asyncio.CancelledError:
            # Leave pending_prompt persisted — the worker is being cancelled
            # (e.g. server shutdown) with the prompt still unanswered, so it
            # can be re-surfaced on resume.
            raise
        except Exception:
            self.__transient.update(pending_prompt=None)
            raise

    async def fire_questions(
        self,
        questions: list[dict[str, object]],
        tool_call_id: str = "",
    ) -> list[dict[str, object]]:
        """Emit a ``prompt.question`` ``kind=request`` and block until the
        user confirms answers to every question in the batch.

        No ``pending_prompt`` is persisted: the ``ask_user`` ``tool_use`` that
        drives this is flushed to ``session.jsonl`` before dispatch, so a
        server restart re-drives the whole batch from scratch (never partial
        answers) via the engine's dangling-tool-use resume path.

        Args:
            questions: Normalized ``ask_user`` batch — one
                ``{'question': str, 'kind': str, 'options': [str, ...]}`` per
                question, in display order.
            tool_call_id: The calling ``tool_use`` block's id, forwarded so
                the client can correlate the interactive panel with the
                persisted feed entry.

        Returns:
            list[dict[str, object]]: One
            ``{'selected': [str, ...], 'free_text': str | None}`` per
            question, in the same order.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.__app_state.register_response_future(req_id, future)

        payload: dict[str, object] = {
            "type": SREQ_PROMPT_QUESTION,
            "tool_call_id": tool_call_id,
            "questions": questions,
        }
        await self.__app_state.send(Envelope(kind="request", id=req_id, payload=payload))
        _log.info("Question batch fired: n=%d req_id=%s", len(questions), req_id[:8])

        response_payload = await future
        answers = self.__normalize_answers(response_payload.get("answers"), len(questions))
        _log.info("Question batch resolved: req_id=%s", req_id[:8])
        return answers

    async def fire_permission(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        external_name: str,
        risk: str,
        intent: str,
        reason: str,
        params: list[dict[str, str]],
        recovered: bool = False,
        parts: tuple[PermissionPartLike, ...] = (),
    ) -> PermissionResponse:
        """Emit a ``prompt.permission`` ``kind=request`` and block until the
        user allows or denies the gated tool call.

        Unlike :meth:`fire_questions`, this *does* persist one piece of state:
        ``pending_security_alert`` on the transient store is set to
        *tool_call_id* for the duration of the wait (mirroring how
        :meth:`fire_approval` persists ``pending_prompt``), and cleared the
        instant the wait resolves — normally, or via a non-cancellation
        exception. It is deliberately left set if the wait is cancelled
        (server restart, or a live disconnect — see
        ``Connection``/``SessionChannel`` in :mod:`kodo.transport`), because
        that is exactly the signal cold-restart resume needs: this specific
        ``tool_call_id`` is provably still at the gate, never dispatched, so
        it is safe to re-judge and — if still "ask" — re-fire this same
        prompt, instead of falling back to the generic interrupted-stand-in
        given to a dangling call that might have started executing (see
        doc/SECURITY.md §7, :meth:`~kodo.runtime._engine._resume.ResumeMixin._resume_main_turn`).

        Args:
            tool_call_id: The gated ``tool_use`` block's id (feed correlation).
            tool_name: Internal tool name (``run_command``).
            external_name: User-facing tool name (``Run Command``).
            risk: The tool's ``SecurityImpact`` label (``High``, …).
            intent: The agent's declared intent ("" when the tool has none).
            reason: The security layer's one-sentence reason for asking.
            params: Customer-visible ``{"name", "value"}`` parameter rows.
            recovered: ``True`` when this prompt is for a salvaged malformed
                tool call — carried on the wire so the client can render a
                distinct "recovered" banner.
            parts: Every elementary command within the call that still needs
                the user's attention — one for an ordinary single-command
                ask, several for a compound pipeline/``&&``/``;`` chain
                (doc/SECURITY_RULES_PLAN.md §2.6). The client shows one
                "always allow — this session / all sessions" checkbox pair
                per part whose ``rule_offer`` is set.

        Returns:
            PermissionResponse: The user's decision, optional feedback, and
            a ``remember`` tuple parallel to *parts*.
        """
        req_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self.__app_state.register_response_future(req_id, future)

        payload: dict[str, object] = {
            "type": SREQ_PROMPT_PERMISSION,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "external_name": external_name,
            "risk": risk,
            "intent": intent,
            "reason": reason,
            "params": params,
            "recovered": recovered,
            "parts": [
                {
                    "reason": part.reason,
                    "rule_offer": (
                        {"executable": part.rule_offer[0], "subcommand": part.rule_offer[1]}
                        if part.rule_offer is not None
                        else None
                    ),
                }
                for part in parts
            ],
        }
        self.__transient.update(pending_security_alert=tool_call_id)
        try:
            await self.__app_state.send(Envelope(kind="request", id=req_id, payload=payload))
            _log.info(
                "Permission prompt fired: tool=%s risk=%s req_id=%s", tool_name, risk, req_id[:8]
            )

            response_payload = await future
            action = str(response_payload.get("action", "deny"))
            if action not in ("allow", "deny"):
                action = "deny"
            feedback = str(response_payload.get("feedback") or "")
            remember_raw = response_payload.get("remember")
            remember: tuple[str | None, ...] = ()
            if isinstance(remember_raw, list):
                remember = tuple(
                    r if isinstance(r, str) and r in ("session", "global") else None
                    for r in remember_raw
                )
            _log.info("Permission prompt resolved: req_id=%s action=%s", req_id[:8], action)
            self.__transient.update(pending_security_alert=None)
            return PermissionResponse(action=action, feedback=feedback, remember=remember)
        except asyncio.CancelledError:
            # Leave pending_security_alert persisted — the wait is being cut
            # short (server restart or a live disconnect) with the call still
            # un-dispatched, so resume can safely re-judge and re-ask it.
            raise
        except Exception:
            self.__transient.update(pending_security_alert=None)
            raise

    @staticmethod
    def __normalize_answers(raw: object, count: int) -> list[dict[str, object]]:
        """Coerce the client's ``answers`` payload to exactly *count* entries.

        Each entry becomes ``{'selected': [str, ...], 'free_text': str | None}``;
        malformed or missing entries collapse to an empty selection so the
        tool result always matches the output schema.
        """
        entries = raw if isinstance(raw, list) else []
        answers: list[dict[str, object]] = []
        for i in range(count):
            entry = entries[i] if i < len(entries) and isinstance(entries[i], dict) else {}
            selected_raw = entry.get("selected")
            selected = [str(s) for s in selected_raw] if isinstance(selected_raw, list) else []
            free_raw = entry.get("free_text")
            free_text = str(free_raw) if isinstance(free_raw, str) and free_raw.strip() else None
            answers.append({"selected": selected, "free_text": free_text})
        return answers

    # Alias so existing call sites use fire() unchanged.
    fire = fire_approval
