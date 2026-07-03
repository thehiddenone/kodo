"""The SMART-mode intent judge: prompt construction and verdict parsing.

One deliberately small, single-shot prompt: the judge model receives the tool
name, the agent's declared intent, the call's (truncated) parameters, the
workspace roots, and any static-analysis notes, and must answer with exactly
one JSON object — ``{"verdict": "allow" | "ask", "reason": "..."}``.

``allow`` means the parameters do what the intent says and the intent is a
plausible, benign development step; anything else — mismatch, vagueness,
possible harm, or doubt — is ``ask``, which surfaces the call to the user.
The judge can therefore never *deny* on its own; the user is always the
authority for anything it does not clear.

The LLM call itself is **not** made here — the layer receives an injected
async callable (see :class:`~kodo.security.JudgeLike`), keeping this package
free of any LLM/transport dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["JudgeVerdict", "build_judge_messages", "parse_judge_verdict"]

# Cap per-parameter value length in the prompt; full content is unnecessary
# for an intent match and would bloat every judged call.
_MAX_VALUE_CHARS = 400
# Overall cap on the rendered parameter block.
_MAX_PARAMS_CHARS = 2_000

JUDGE_SYSTEM_PROMPT = (
    "You are the security gate of Kodo, an autonomous coding agent operating "
    "inside a user-approved workspace. You receive ONE tool call: the tool's "
    "name, the agent's declared intent, the call's parameters, the workspace "
    "roots, and static-analysis notes. Judge it:\n"
    '- "allow": the parameters do exactly what the intent states, the intent '
    "is a specific and plausible step of benign software development, and the "
    "action stays within the workspace / ordinary development-tool behavior.\n"
    '- "ask": anything else — the parameters do more than or differ from the '
    "intent, the intent is vague, misleading, or harmful, the action could "
    "damage the system, exfiltrate or destroy data, alter global/system "
    "state, or you are unsure.\n"
    '"ask" merely refers the call to the human user; when in doubt, "ask".\n'
    "Respond with EXACTLY one JSON object and nothing else: "
    '{"verdict": "allow" | "ask", "reason": "<one short sentence>"}'
)


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's parsed answer.

    Attributes:
        allow: ``True`` for a clean ``allow`` verdict; ``False`` otherwise
            (including unreadable output — fail closed).
        reason: The judge's one-sentence justification (or the parse-failure
            explanation).
    """

    allow: bool
    reason: str


def build_judge_messages(
    *,
    tool_name: str,
    external_name: str,
    intent: str,
    params: dict[str, object],
    roots: tuple[str, ...],
    notes: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Build the ``(system, user)`` message pair for one judged call.

    Args:
        tool_name: Internal tool name (``run_command``).
        external_name: User-facing tool name (``Run Command``).
        intent: The agent's declared ``intent`` ("" when the tool carries none).
        params: The call's input parameters (``intent`` excluded by the caller);
            values are stringified and truncated.
        roots: Absolute workspace root paths.
        notes: Static-analysis findings worth the judge's attention (e.g.
            unresolvable substitutions).

    Returns:
        tuple[str, str]: ``(system_prompt, user_message)``.
    """
    lines = [f"Tool: {tool_name} ({external_name})"]
    lines.append(f"Declared intent: {intent.strip() or '(none declared)'}")
    lines.append("Parameters:")
    used = 0
    for name, value in params.items():
        text = _stringify(value)
        if len(text) > _MAX_VALUE_CHARS:
            text = text[:_MAX_VALUE_CHARS] + f"… [{len(text)} chars total]"
        row = f"  {name} = {text}"
        if used + len(row) > _MAX_PARAMS_CHARS:
            lines.append("  … (remaining parameters truncated)")
            break
        lines.append(row)
        used += len(row)
    lines.append("Workspace roots:")
    lines.extend(f"  - {root}" for root in roots)
    if notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in notes)
    return JUDGE_SYSTEM_PROMPT, "\n".join(lines)


def parse_judge_verdict(raw: str) -> JudgeVerdict:
    """Parse the judge model's raw text into a :class:`JudgeVerdict`.

    Scans for the first decodable JSON object (models occasionally wrap the
    answer in prose or a code fence despite instructions). Anything that is
    not a clean ``{"verdict": "allow"}`` — including unreadable output —
    yields ``allow=False``: the gate fails closed to a user prompt, never
    open.

    Args:
        raw: The model's full text output.

    Returns:
        JudgeVerdict: The parsed verdict.
    """
    decoder = json.JSONDecoder()
    index = raw.find("{")
    while index != -1:
        try:
            obj, _ = decoder.raw_decode(raw, index)
        except ValueError:
            index = raw.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            verdict = str(obj.get("verdict", "")).strip().lower()
            reason = str(obj.get("reason", "")).strip()
            if verdict == "allow":
                return JudgeVerdict(allow=True, reason=reason or "Intent matches the call.")
            return JudgeVerdict(
                allow=False, reason=reason or "The security judge referred this call to you."
            )
        index = raw.find("{", index + 1)
    return JudgeVerdict(allow=False, reason="The security judge returned an unreadable verdict.")


def _stringify(value: object) -> str:
    """Compact, deterministic string form of a parameter value."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)
