"""Simulated user: scripted answers to every interactive server request.

The kodo server treats its client as the user's proxy: agents ask questions
(``prompt.question``), the Guide pipeline raises document-review gates
(``prompt.approval``), the security layer raises permission gates
(``prompt.permission``), and the LLM gateway pulls API keys
(``api_key.request``). During validation all of these must be answered by a
policy instead of a human — and every exchange is logged to the transcript so
the evaluator can score how the agent used them.

:class:`ScriptedUser` is the default policy: optional per-batch scripted
answers consumed in order, falling back to deterministic defaults (first
option / free-text fallback, approve, allow, key from environment).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

__all__ = ["QuestionAnswer", "ScriptedUser", "UserSimulator"]


@dataclass(frozen=True)
class QuestionAnswer:
    """One scripted answer to one question of an ``ask_user`` batch.

    Attributes:
        selected: Option texts to select verbatim (empty for free-text-only).
        free_text: Free-text portion, or None.
    """

    selected: list[str] = field(default_factory=list)
    free_text: str | None = None

    def to_payload(self) -> dict[str, object]:
        """This answer as one ``prompt.question.response`` entry.

        Returns:
            dict[str, object]: ``{"selected": [...], "free_text": ...}``.
        """
        return {"selected": list(self.selected), "free_text": self.free_text}


class UserSimulator(Protocol):
    """Answering policy for every server→client interactive request."""

    async def answer_questions(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a ``prompt.question`` batch.

        Args:
            payload (dict[str, object]): The request payload (``questions`` list).

        Returns:
            dict[str, object]: A ``prompt.question.response`` payload.
        """
        ...

    async def answer_approval(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a ``prompt.approval`` document-review gate.

        Args:
            payload (dict[str, object]): The request payload.

        Returns:
            dict[str, object]: A ``prompt.approval.response`` payload.
        """
        ...

    async def answer_permission(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a ``prompt.permission`` security gate.

        Args:
            payload (dict[str, object]): The request payload.

        Returns:
            dict[str, object]: A ``prompt.permission.response`` payload.
        """
        ...

    async def provide_api_key(self, vendor: str) -> str | None:
        """Resolve a vendor API key for ``api_key.request``.

        Args:
            vendor (str): Vendor id (e.g. ``anthropic``).

        Returns:
            str | None: The key, or None to answer ``{"error": "cancelled"}``.
        """
        ...


class ScriptedUser:
    """Deterministic :class:`UserSimulator` with optional scripted answers.

    Args:
        question_script: Answer batches consumed in order, one list of
            :class:`QuestionAnswer` per expected ``prompt.question`` request.
            When exhausted (or unset), each question falls back to its first
            option, or to *free_text_fallback* when it has no options.
        free_text_fallback: Free-text used for option-less questions outside
            the script.
        approval_action: Default document-review verdict.
        approval_feedback: Feedback text sent when *approval_action* is
            ``feedback``.
        permission_action: Default security-gate verdict.
        permission_feedback: Optional free text attached to permission replies
            (returned to the agent verbatim on a denial).
        api_keys: Vendor → key map consulted first. Missing vendors fall back
            to ``KODO_VALIDATOR_API_KEY_<VENDOR>``, then ``<VENDOR>_API_KEY``.
    """

    def __init__(
        self,
        *,
        question_script: list[list[QuestionAnswer]] | None = None,
        free_text_fallback: str = "Use your best judgment.",
        approval_action: Literal["agree", "feedback"] = "agree",
        approval_feedback: str | None = None,
        permission_action: Literal["allow", "deny"] = "allow",
        permission_feedback: str | None = None,
        api_keys: dict[str, str] | None = None,
    ) -> None:
        self.__question_script = list(question_script or [])
        self.__free_text_fallback = free_text_fallback
        self.__approval_action = approval_action
        self.__approval_feedback = approval_feedback
        self.__permission_action = permission_action
        self.__permission_feedback = permission_feedback
        self.__api_keys = dict(api_keys or {})

    async def answer_questions(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a question batch from the script, else with defaults.

        Args:
            payload (dict[str, object]): The ``prompt.question`` payload.

        Returns:
            dict[str, object]: The ``prompt.question.response`` payload.
        """
        questions = cast(list[dict[str, object]], payload.get("questions") or [])
        if self.__question_script:
            scripted = self.__question_script.pop(0)
            answers = [a.to_payload() for a in scripted]
            # Pad a short scripted batch with defaults rather than failing the
            # tool call — the mismatch itself stays visible in the transcript.
            for question in questions[len(answers) :]:
                answers.append(self.__default_answer(question))
        else:
            answers = [self.__default_answer(q) for q in questions]
        return {"type": "prompt.question.response", "answers": answers}

    async def answer_approval(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a document-review gate with the configured verdict.

        Args:
            payload (dict[str, object]): The ``prompt.approval`` payload.

        Returns:
            dict[str, object]: The ``prompt.approval.response`` payload.
        """
        return {
            "type": "prompt.approval.response",
            "action": self.__approval_action,
            "feedback_text": (
                self.__approval_feedback if self.__approval_action == "feedback" else None
            ),
        }

    async def answer_permission(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a security permission gate with the configured verdict.

        Args:
            payload (dict[str, object]): The ``prompt.permission`` payload.

        Returns:
            dict[str, object]: The ``prompt.permission.response`` payload.
        """
        return {
            "type": "prompt.permission.response",
            "action": self.__permission_action,
            "feedback": self.__permission_feedback,
        }

    async def provide_api_key(self, vendor: str) -> str | None:
        """Resolve a key from the explicit map, then the environment.

        Args:
            vendor (str): Vendor id (e.g. ``anthropic``).

        Returns:
            str | None: The key, or None when nothing is configured.
        """
        if vendor in self.__api_keys:
            return self.__api_keys[vendor]
        slug = vendor.upper().replace("-", "_")
        return os.environ.get(f"KODO_VALIDATOR_API_KEY_{slug}") or os.environ.get(f"{slug}_API_KEY")

    def __default_answer(self, question: dict[str, object]) -> dict[str, object]:
        options = cast(list[object], question.get("options") or [])
        if options:
            return QuestionAnswer(selected=[str(options[0])]).to_payload()
        return QuestionAnswer(free_text=self.__free_text_fallback).to_payload()
