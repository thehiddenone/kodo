"""The validation-LLM user proxy: ``ask_user`` answered by the VLLM.

Phase 2 of the harness (doc/VALIDATOR.md §9) replaces scripted question
answers with real ones produced by the **validation LLM** (VLLM). When the
LLM-under-test (LUT) raises a ``prompt.question`` batch, its ``ask_user``
tool call dangles server-side with no timeout, which makes this swap safe:

1. ``llm.select`` the VLLM — synchronous; the reply confirms llama-server is
   actually serving it;
2. ``llm.complete`` with the **User Proxy Prompt** (UPP) as system prompt and
   the task prompt + question batch as the user message, grammar-constrained
   to :func:`answers_json_schema` so the reply is parseable by construction;
3. ``llm.select`` the LUT back;
4. answer the dangling request with the parsed batch.

Permissions and document-review approvals are *not* proxied (they keep the
wrapped base simulator's behaviour — allow / agree — and stay fully logged);
only ``prompt.question`` reaches the VLLM.

Failures are deliberately fatal: a run whose questions silently fell back to
scripted defaults would report a score that lies about how the LUT was
steered. The first error is kept in :attr:`VLLMUserProxy.failure` and the
harness aborts the scenario after the turn settles.
"""

from __future__ import annotations

import json
import logging
from typing import cast

from kodo.transport import MSG_LLM_COMPLETE, MSG_LLM_SELECT

from ._client import ValidatorClient
from ._transcript import Transcript
from ._user import ScriptedUser, UserSimulator

__all__ = ["VLLMProxyError", "VLLMUserProxy", "answers_json_schema"]

_log = logging.getLogger(__name__)

# Model loads take minutes on big GGUFs; completions on a batch of questions
# can too. Both are per-request WS response timeouts, overridable per harness.
DEFAULT_SWITCH_TIMEOUT = 600.0
DEFAULT_COMPLETE_TIMEOUT = 900.0
DEFAULT_MAX_ATTEMPTS = 3


class VLLMProxyError(RuntimeError):
    """A VLLM-proxied question answer could not be produced.

    Raised for model-switch failures, ``llm.complete`` failures, and answers
    that stayed unparseable after every retry. Always fatal to the scenario.
    """


def answers_json_schema(question_count: int) -> dict[str, object]:
    """The JSON schema an UPP answer must match, for *question_count* questions.

    Passed to ``llm.complete``'s ``json_schema`` so llama-server grammar-
    enforces the shape. ``free_text`` is a plain string (empty = unused)
    rather than ``string|null`` — nullable unions convert less reliably to
    GBNF grammars than concrete types.

    Args:
        question_count (int): Exact number of answer entries required.

    Returns:
        dict[str, object]: A JSON-schema object for
        ``{"answers": [{"selected": [...], "free_text": "..."}]}``.
    """
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "minItems": question_count,
                "maxItems": question_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "selected": {"type": "array", "items": {"type": "string"}},
                        "free_text": {"type": "string"},
                    },
                    "required": ["selected", "free_text"],
                },
            }
        },
        "required": ["answers"],
    }


class VLLMUserProxy:
    """A :class:`UserSimulator` whose question answers come from the VLLM.

    Wraps a base simulator: approvals, permissions, and API keys delegate to
    it unchanged (the always-allow logging behaviour the spec asks for);
    ``prompt.question`` batches run the switch → complete → switch-back
    sequence described in the module docstring.

    Args:
        user_proxy_prompt: The UPP — instructions for answering the LUT's
            questions (content is a phase-3 concern; carried verbatim).
        llm_under_test: Local registry name to switch back to after answering.
        validation_llm: Local registry name of the answering model.
        base: Simulator for everything that is not a question batch (a
            default :class:`ScriptedUser` when omitted).
        switch_timeout: WS response timeout for each ``llm.select``.
        complete_timeout: WS response timeout for each ``llm.complete``.
        max_attempts: Completion attempts per batch before giving up.
        thinking_level: When set, rides ``llm.complete``'s ``thinking_level``
            field (doc/WS_PROTOCOL.md §7.6b) on every answering call — a
            valid tier slug for *validation_llm*'s thinking family (e.g.
            ``"minimal"`` to keep ``ask_user`` answers from burning time
            thinking). Scoped to just these calls; never touches settings.json.
    """

    def __init__(
        self,
        *,
        user_proxy_prompt: str,
        llm_under_test: str,
        validation_llm: str,
        base: UserSimulator | None = None,
        switch_timeout: float = DEFAULT_SWITCH_TIMEOUT,
        complete_timeout: float = DEFAULT_COMPLETE_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        thinking_level: str | None = None,
    ) -> None:
        self.__upp = user_proxy_prompt
        self.__llm_under_test = llm_under_test
        self.__validation_llm = validation_llm
        self.__base: UserSimulator = base if base is not None else ScriptedUser()
        self.__switch_timeout = switch_timeout
        self.__complete_timeout = complete_timeout
        self.__max_attempts = max(1, max_attempts)
        self.__thinking_level = thinking_level

        self.__client: ValidatorClient | None = None
        self.__transcript: Transcript | None = None
        self.__task_prompt = ""
        self.__failure: str | None = None

    @property
    def failure(self) -> str | None:
        """First fatal proxy error, if any — the harness aborts on it."""
        return self.__failure

    def bind(self, client: ValidatorClient, transcript: Transcript) -> None:
        """Attach the live protocol client (harness calls this after connect).

        Args:
            client (ValidatorClient): The main session's client — the proxy
                sends its ``llm.select``/``llm.complete`` requests on it.
            transcript (Transcript): Recorder for proxy lifecycle notes.
        """
        self.__client = client
        self.__transcript = transcript

    def set_task_prompt(self, text: str) -> None:
        """Record the prompt under test, forwarded to the VLLM as context.

        Args:
            text (str): The prompt the harness is about to submit.
        """
        self.__task_prompt = text

    # ------------------------------------------------------------------
    # UserSimulator
    # ------------------------------------------------------------------

    async def answer_questions(self, payload: dict[str, object]) -> dict[str, object]:
        """Answer a ``prompt.question`` batch with the VLLM.

        Args:
            payload (dict[str, object]): The request payload (``questions``).

        Returns:
            dict[str, object]: A ``prompt.question.response`` payload.

        Raises:
            VLLMProxyError: On any switch/completion/parse failure (after
                retries). The client answers ``simulator_failure`` on the
                wire; the harness re-raises after the turn settles.
        """
        questions = cast(list[dict[str, object]], payload.get("questions") or [])
        if not questions:
            return {"type": "prompt.question.response", "answers": []}
        try:
            answers = await self.__answer_via_vllm(questions)
        except Exception as exc:
            if self.__failure is None:
                self.__failure = f"{type(exc).__name__}: {exc}"
            raise
        return {"type": "prompt.question.response", "answers": answers}

    async def answer_approval(self, payload: dict[str, object]) -> dict[str, object]:
        """Delegate a document-review gate to the base simulator.

        Args:
            payload (dict[str, object]): The ``prompt.approval`` payload.

        Returns:
            dict[str, object]: The base simulator's reply.
        """
        return await self.__base.answer_approval(payload)

    async def answer_permission(self, payload: dict[str, object]) -> dict[str, object]:
        """Delegate a security gate to the base simulator (allow-all + log).

        Args:
            payload (dict[str, object]): The ``prompt.permission`` payload.

        Returns:
            dict[str, object]: The base simulator's reply.
        """
        return await self.__base.answer_permission(payload)

    async def provide_api_key(self, vendor: str) -> str | None:
        """Delegate an API-key request to the base simulator.

        Args:
            vendor (str): Vendor id.

        Returns:
            str | None: The base simulator's key, if any.
        """
        return await self.__base.provide_api_key(vendor)

    # ------------------------------------------------------------------
    # The switch → complete → switch-back sequence
    # ------------------------------------------------------------------

    async def __answer_via_vllm(
        self, questions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        client = self.__client
        if client is None:
            raise VLLMProxyError("Proxy not bound to a client (harness must call bind())")
        self.__note("llm_selected", model=self.__validation_llm, purpose="user_proxy")
        await client.request(
            MSG_LLM_SELECT,
            name=self.__validation_llm,
            session_scoped=False,
            timeout=self.__switch_timeout,
        )
        try:
            return await self.__complete_answers(client, questions)
        finally:
            # Always restore the LUT — the dangling ask_user resumes the turn
            # on whatever settings name next, and that must be the LUT even
            # when answering failed. A restore failure aborts the run anyway.
            self.__note("llm_selected", model=self.__llm_under_test, purpose="restore_under_test")
            await client.request(
                MSG_LLM_SELECT,
                name=self.__llm_under_test,
                session_scoped=False,
                timeout=self.__switch_timeout,
            )

    async def __complete_answers(
        self, client: ValidatorClient, questions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        prompt = self.__render_request(questions)
        schema = answers_json_schema(len(questions))
        last_error = "no attempts made"
        for attempt in range(1, self.__max_attempts + 1):
            response = await client.request(
                MSG_LLM_COMPLETE,
                prompt=prompt,
                system=self.__upp,
                json_schema=schema,
                session_scoped=False,
                timeout=self.__complete_timeout,
                thinking_level=self.__thinking_level,
            )
            text = str(response.get("text", ""))
            try:
                return self.__parse_answers(text, questions)
            except ValueError as exc:
                last_error = str(exc)
                self.__note(
                    "vllm_answer_retry", attempt=attempt, error=last_error, text=text[:2000]
                )
                _log.warning("VLLM answer attempt %d unparseable: %s", attempt, last_error)
        raise VLLMProxyError(
            f"VLLM produced no parseable answer in {self.__max_attempts} attempts: {last_error}"
        )

    def __render_request(self, questions: list[dict[str, object]]) -> str:
        """The user-message scaffold around the UPP (which rides as ``system``).

        Pure mechanics — the UPP owns all behavioural instruction; this only
        carries the data (task prompt + question batch) and the wire contract
        the parser needs.
        """
        return (
            "## Task prompt under test\n\n"
            f"{self.__task_prompt or '(no prompt recorded)'}\n\n"
            "## Questions the assistant asked\n\n"
            f"{json.dumps({'questions': questions}, ensure_ascii=False, indent=2)}\n\n"
            "## Response format\n\n"
            'Reply with a single JSON object: {"answers": [{"selected": [...], '
            '"free_text": "..."}]} — exactly one entry per question, in order. '
            '"selected" may only contain option texts quoted verbatim from that '
            'question; put anything else in "free_text" (empty string when unused).'
        )

    def __parse_answers(
        self, text: str, questions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Parse + normalize a completion into ``prompt.question.response`` answers.

        Raises:
            ValueError: If *text* is not the required JSON shape.
        """
        try:
            parsed = json.loads(text.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), list):
            raise ValueError('missing "answers" array')
        raw_answers = cast(list[object], parsed["answers"])
        if len(raw_answers) != len(questions):
            raise ValueError(f"expected {len(questions)} answers, got {len(raw_answers)}")

        answers: list[dict[str, object]] = []
        for raw, question in zip(raw_answers, questions, strict=True):
            entry = raw if isinstance(raw, dict) else {}
            options = {str(o) for o in cast(list[object], question.get("options") or [])}
            selected_raw = entry.get("selected")
            selected_all = (
                [str(s) for s in cast(list[object], selected_raw)]
                if isinstance(selected_raw, list)
                else []
            )
            selected = [s for s in selected_all if s in options]
            # A "selected" string that is not verbatim an option is still an
            # answer — the VLLM chose to say something of its own. Fold it
            # into free_text rather than dropping it.
            stray = [s for s in selected_all if s not in options]
            free_text = str(entry.get("free_text") or "").strip()
            merged_free = " ".join(part for part in (free_text, *stray) if part).strip()
            if not selected and not merged_free:
                raise ValueError("an answer selected nothing and carried no free text")
            answers.append(
                {"selected": selected, "free_text": merged_free if merged_free else None}
            )
        return answers

    def __note(self, event: str, **fields: object) -> None:
        if self.__transcript is not None:
            self.__transcript.record("note", "lifecycle", {"event": event, **fields})
