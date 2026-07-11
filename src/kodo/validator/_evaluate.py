"""Phase-2 result evaluation: the VLLM judges the finished run.

After the LUT's turns settle, the harness switches llama-server to the
validation LLM (``llm.select``, synchronous) and opens a **second session**
on the same server — a separate WebSocket, its own ``hello``, the same
simulated-workspace push. The judge turn is therefore a real kodo turn with
real read tools, which is what lets the **Result Validation Prompt** (RVP)
receive *paths* to the generated code instead of inlined copies: the judge
explores the workspace itself.

The judge prompt is the RVP followed by a mechanical context block (workspace
roots, the prompts under test, the full interaction log) and the output
contract. A session turn cannot be grammar-constrained and an agentic turn's
assistant text is polluted with exploration narration, so the judge submits its
verdict through the ``submit_evaluation`` **tool**: the harness reads
``{score, report}`` off the tool-call detail event
(:func:`_verdict_from_tool_calls`) instead of parsing free text. Parsing a JSON
object out of the assistant text (:func:`_parse_score`) remains as a fallback
for a judge that answers in prose, and each retry is a follow-up turn in the
same judge session asking for the verdict again.

Deliberately *not* here: prompt content (phase 3) and any fallback scoring —
an evaluation that cannot produce a parseable score raises
:class:`EvaluationError` and fails the scenario, it never fabricates one.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from kodo.transport import (
    MSG_COMMAND_CONTROL_SET,
    MSG_EDIT_CONTROL_SET,
    MSG_LLM_SELECT,
    MSG_MODE_SET,
    MSG_PROMPT_SUBMIT,
    MSG_WORKFLOW_SET,
    MSG_WORKSPACE_FOLDERS,
)

from ._client import ValidatorClient
from ._transcript import Transcript
from ._user import ScriptedUser
from ._vllm import DEFAULT_SWITCH_TIMEOUT

__all__ = ["EvaluationError", "EvaluationResult", "run_evaluation"]

_log = logging.getLogger(__name__)

DEFAULT_EVAL_TURN_TIMEOUT = 900.0
DEFAULT_EVAL_MAX_ATTEMPTS = 3

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class EvaluationError(RuntimeError):
    """The judge run failed or never produced a parseable ``{score, report}``."""


@dataclass(frozen=True)
class EvaluationResult:
    """What one RVP evaluation produced.

    Attributes:
        score: The judge's 0–100 rating of the run.
        report: The judge's free-form report text (markdown).
        raw_text: The assistant text the score was parsed from.
        attempts: Judge turns it took to get parseable JSON.
        judge_session_id: The second session's id (its transcript is
            ``judge-transcript.jsonl`` in the run directory).
    """

    score: float
    report: str
    raw_text: str
    attempts: int
    judge_session_id: str | None


async def run_evaluation(
    *,
    ws_url: str,
    run_dir: Path,
    main_client: ValidatorClient,
    transcript: Transcript,
    workspace_payload: dict[str, object],
    result_validation_prompt: str,
    validation_llm: str,
    prompts: list[str],
    turn_timeout: float = DEFAULT_EVAL_TURN_TIMEOUT,
    switch_timeout: float = DEFAULT_SWITCH_TIMEOUT,
    max_attempts: int = DEFAULT_EVAL_MAX_ATTEMPTS,
) -> EvaluationResult:
    """Switch to the VLLM and run the judge session over the finished run.

    The active model is left on *validation_llm* afterwards — every scenario
    gets a fresh home + server, and a caller reusing the harness for more LUT
    turns would go through the proxy's own switch anyway.

    Args:
        ws_url (str): The run's server endpoint (second connection target).
        run_dir (Path): Run artifact dir; the judge transcript lands here.
        main_client (ValidatorClient): The main session's client — used only
            for the synchronous ``llm.select``.
        transcript (Transcript): Main transcript; receives the evaluation
            lifecycle notes.
        workspace_payload (dict[str, object]): ``workspace.folders`` payload —
            re-pushed to the judge session and cited (paths) in its prompt.
        result_validation_prompt (str): The RVP text (phase-3 content).
        validation_llm (str): Local registry name of the judge model.
        prompts (list[str]): Every prompt the run submitted, in order.
        turn_timeout (float): Per-judge-turn timeout in seconds.
        switch_timeout (float): ``llm.select`` response timeout.
        max_attempts (int): Judge turns before giving up on parseable JSON.

    Returns:
        EvaluationResult: Score, report, and provenance.

    Raises:
        EvaluationError: If a judge turn ends in ``error`` or no attempt
            yields a parseable score.
    """
    transcript.record(
        "note", "lifecycle", {"event": "llm_selected", "model": validation_llm, "purpose": "judge"}
    )
    await main_client.request(
        MSG_LLM_SELECT, name=validation_llm, session_scoped=False, timeout=switch_timeout
    )

    judge_transcript = Transcript(run_dir / "judge-transcript.jsonl")
    judge = ValidatorClient(
        ws_url, judge_transcript, ScriptedUser(), window_id="kodo-validator-judge"
    )
    try:
        await judge.connect()
        await judge.hello()
        await judge.request(MSG_WORKSPACE_FOLDERS, dict(workspace_payload))
        # Autonomous problem-solving with friction minimized: the judge only
        # reads, gates would just add noise (and SMART security judgements
        # would burn extra VLLM calls) to the run being scored.
        await judge.request(MSG_MODE_SET, autonomous=True)
        await judge.request(MSG_WORKFLOW_SET, mode="problem_solving")
        await judge.request(MSG_EDIT_CONTROL_SET, edit_control="allow_all")
        await judge.request(MSG_COMMAND_CONTROL_SET, command_control="permissive")

        interactions = [e.payload for e in transcript.interactions()]
        prompt = _render_judge_prompt(
            result_validation_prompt, workspace_payload, prompts, interactions
        )
        text = ""
        last_error = "no attempts made"
        for attempt in range(1, max(1, max_attempts) + 1):
            start_seq = len(judge_transcript.entries)
            judge.begin_turn()
            await judge.request(MSG_PROMPT_SUBMIT, text=prompt)
            final_phase = await judge.wait_turn_end(timeout=turn_timeout)
            if final_phase == "error":
                raise EvaluationError(f"Judge turn ended in error (attempt {attempt})")
            text = judge_transcript.assistant_text(start=start_seq)
            # Prefer the structured verdict the judge submitted through the
            # submit_evaluation tool (read off the tool-call detail event, so it
            # survives an agentic turn's narration); fall back to parsing a JSON
            # object out of the assistant text only when no tool call was made.
            verdict = _verdict_from_tool_calls(judge_transcript.tool_calls(start=start_seq))
            source = "tool"
            if verdict is None:
                source = "parsed_text"
                try:
                    verdict = _parse_score(text)
                except ValueError as exc:
                    last_error = str(exc)
                    transcript.record(
                        "note",
                        "lifecycle",
                        {"event": "evaluation_retry", "attempt": attempt, "error": last_error},
                    )
                    _log.warning("Judge attempt %d had no verdict: %s", attempt, last_error)
                    prompt = _RETRY_PROMPT
                    continue
            score, report = verdict
            transcript.record(
                "note",
                "evaluation",
                {"score": score, "report": report, "attempts": attempt, "source": source},
            )
            return EvaluationResult(
                score=score,
                report=report,
                raw_text=text,
                attempts=attempt,
                judge_session_id=judge.session_id,
            )
        raise EvaluationError(f"Judge produced no verdict in {max_attempts} attempts: {last_error}")
    finally:
        await judge.close()
        judge_transcript.close()


# Name of the tool the judge submits its verdict through (kodo.toolspecs
# SUBMIT_EVALUATION). Read off the tool-call detail event rather than imported,
# to keep this package free of engine-internal imports.
_SUBMIT_EVALUATION_TOOL = "submit_evaluation"

# The wire contract the judge follows; behavioural instruction belongs to the
# RVP itself (phase 3). The verdict rides the submit_evaluation tool call; the
# JSON-object form is only a fallback for a judge that will not call the tool.
_CONTRACT = (
    "When you have finished reviewing, submit your verdict by calling the "
    "`submit_evaluation` tool exactly once, with `score` (a number 0-100) and "
    "`report` (your full written assessment). Do not answer in prose. If for any "
    "reason you cannot call the tool, reply instead with a single JSON object — "
    '{"score": <number 0-100>, "report": "<your full assessment>"} — and nothing else.'
)

_RETRY_PROMPT = f"You did not submit a verdict. {_CONTRACT}"


def _verdict_from_tool_calls(tool_calls: list[dict[str, object]]) -> tuple[float, str] | None:
    """Extract ``(score, report)`` from a ``submit_evaluation`` tool call.

    Reads the verdict off the tool-call detail rows (the customer-visible
    input/output projection the engine emits for every non-``ask_user`` call),
    so it survives an agentic turn's surrounding narration. The last
    ``submit_evaluation`` call wins; within a call the output rows (the tool's
    coerced echo) are appended after the input rows, so they take precedence.

    Args:
        tool_calls (list[dict[str, object]]): ``Transcript.tool_calls`` output
            (prep merged with detail) for the judge turn.

    Returns:
        tuple[float, str] | None: The verdict, or None if the judge did not
        submit one this turn.
    """
    for call in reversed(tool_calls):
        if call.get("tool_name") != _SUBMIT_EVALUATION_TOOL:
            continue
        detail = call.get("detail")
        rows = detail.get("rows") if isinstance(detail, dict) else None
        if not isinstance(rows, list):
            continue
        score: float | None = None
        report = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if name == "score":
                coerced = _coerce_row_score(row.get("value"))
                if coerced is not None:
                    score = coerced
            elif name == "report":
                report = str(row.get("value") or "")
        if score is not None:
            return score, report
    return None


def _coerce_row_score(value: object) -> float | None:
    """Parse a detail-row score value (a stringified number) into 0–100.

    Args:
        value (object): The row's ``value`` (``stringify_value`` output).

    Returns:
        float | None: The score if it parses to a number in 0–100, else None.
    """
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if score != score or not 0.0 <= score <= 100.0:  # NaN or out of range
        return None
    return score


def _render_judge_prompt(
    rvp: str,
    workspace_payload: dict[str, object],
    prompts: list[str],
    interactions: list[dict[str, object]],
) -> str:
    """Assemble the judge turn's prompt: RVP + run context + output contract.

    Args:
        rvp (str): The Result Validation Prompt.
        workspace_payload (dict[str, object]): Root name → path map the judge
            can read with its tools.
        prompts (list[str]): The prompts under test, in submission order.
        interactions (list[dict[str, object]]): Every simulated-user exchange
            (questions, permissions, approvals) from the run transcript.

    Returns:
        str: The composed prompt text.
    """
    prompt_lines = "\n\n".join(
        f"### Prompt {i}\n\n{text}" for i, text in enumerate(prompts, start=1)
    )
    return (
        f"{rvp}\n\n"
        "## Workspace under evaluation\n\n"
        "The generated code lives in these workspace folders (read them with "
        "your tools):\n\n"
        f"{json.dumps(workspace_payload, ensure_ascii=False, indent=2)}\n\n"
        "## Task prompts that were under test\n\n"
        f"{prompt_lines or '(none)'}\n\n"
        "## Interaction log\n\n"
        "Every question, permission, and approval the assistant raised during "
        "the run, with the answer it received:\n\n"
        f"{json.dumps(interactions, ensure_ascii=False, indent=2)}\n\n"
        "## Response format\n\n"
        f"{_CONTRACT}"
    )


def _parse_score(text: str) -> tuple[float, str]:
    """Extract ``(score, report)`` from the judge's assistant text.

    Tries, in order: every fenced JSON block, the whole stripped text, and
    the outermost ``{…}`` substring.

    Args:
        text (str): Judge assistant text.

    Returns:
        tuple[float, str]: The validated score and the report text.

    Raises:
        ValueError: If nothing parses into ``{"score": <0-100 number>, ...}``.
    """
    candidates: list[str] = _JSON_FENCE.findall(text)
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    last_error = "no JSON object found"
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"not valid JSON: {exc}"
            continue
        if not isinstance(parsed, dict):
            last_error = "JSON is not an object"
            continue
        score_raw = parsed.get("score")
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float)):
            last_error = '"score" is not a number'
            continue
        score = float(score_raw)
        if not 0.0 <= score <= 100.0:
            last_error = f'"score" out of range: {score}'
            continue
        return score, str(parsed.get("report") or "")
    raise ValueError(last_error)
