"""Kodo's session log → an ATIF trajectory (Harbor's ``trajectory.json``).

The source is the session directory ``kodo-headless --transcript-dir`` exports
(doc/SESSIONS.md): ``session.jsonl`` for the top-level agent and one
``subsessions/<id>.jsonl`` per sub-agent run. It is exactly the LLM-visible
conversation — every assistant message with its ``thinking`` / ``text`` /
``tool_use`` blocks, every ``tool_result`` as the model received it — plus a
``usage`` marker written just *before* the assistant message it paid for.

Mapping (ATIF-v1.7, Harbor's ``harbor.models.trajectories``):

- a user message with plain text → a ``user`` step (the instruction, a nudge);
- an assistant message → one ``agent`` step: ``text`` → ``message``,
  ``thinking`` → ``reasoning_content``, ``tool_use`` → ``tool_calls``, the
  preceding ``usage`` marker → ``metrics`` and ``model_name``;
- ``tool_result`` blocks → ``observation`` results on the step that made
  the calls, matched by ``tool_call_id``;
- a sub-agent run → an embedded ``subagent_trajectories`` entry
  (``trajectory_id`` = the subsession id), referenced from the observation of
  the ``run_subagent_*`` call that spawned it;
- ``compaction`` and ``error`` markers → ``system`` steps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal, cast

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)

__all__ = ["ATIF_SCHEMA_VERSION", "session_to_trajectory"]

#: The ATIF revision this converter writes.
ATIF_SCHEMA_VERSION: Final = "ATIF-v1.7"

_SESSION_LOG = "session.jsonl"
_SUBSESSIONS = "subsessions"
_SPAWN_PREFIX = "run_subagent"


def session_to_trajectory(
    session_dir: Path,
    *,
    agent_version: str,
    model_name: str,
    top_agent: str,
    session_id: str | None = None,
) -> Trajectory | None:
    """Convert an exported kodo session directory into one ATIF trajectory.

    Args:
        session_dir (Path): The directory holding ``session.jsonl``.
        agent_version (str): The py-kodo version that ran.
        model_name (str): The model the run was configured with.
        top_agent (str): The top-level kodo agent.
        session_id (str | None): Run id for ``Trajectory.session_id``;
            defaults to the directory name (kodo's own session id).

    Returns:
        Trajectory | None: The trajectory, or ``None`` when the directory
        holds no conversation.

    Raises:
        ValueError: The log converts into something ATIF rejects.
    """
    lines = _read_lines(session_dir / _SESSION_LOG)
    if not lines:
        return None
    run_id = session_id or session_dir.name
    builder = _Builder()
    builder.feed(lines)
    subagents: list[Trajectory] = []
    for subsession_id, agent in builder.subsessions:
        sub_builder = _Builder()
        sub_builder.feed(_read_lines(session_dir / _SUBSESSIONS / f"{subsession_id}.jsonl"))
        if not sub_builder.steps:
            continue
        subagents.append(
            Trajectory(
                schema_version=ATIF_SCHEMA_VERSION,
                session_id=run_id,
                trajectory_id=subsession_id,
                agent=Agent(name=f"kodo/{agent}", version=agent_version, model_name=model_name),
                steps=sub_builder.steps,
                final_metrics=sub_builder.final_metrics([]),
            )
        )
    if not builder.steps:
        return None
    return Trajectory(
        schema_version=ATIF_SCHEMA_VERSION,
        session_id=run_id,
        agent=Agent(
            name="kodo",
            version=agent_version,
            model_name=model_name,
            extra={"top_agent": top_agent},
        ),
        steps=builder.steps,
        final_metrics=builder.final_metrics(subagents),
        subagent_trajectories=subagents or None,
        notes=(
            "Converted from kodo's session log. final_metrics totals include every "
            "embedded sub-agent trajectory."
        ),
    )


class _Builder:
    """Walks one log (session or subsession) in order, emitting ATIF steps."""

    __steps: list[Step]
    __subsessions: list[tuple[str, str]]
    __pending_usage: dict[str, object] | None
    __pending_spawns: list[str]
    __spawned: dict[str, str]
    __step_of_call: dict[str, Step]
    __prompt: int
    __completion: int
    __cached: int
    __cost: float

    def __init__(self) -> None:
        self.__steps = []
        self.__subsessions = []
        self.__pending_usage = None
        self.__pending_spawns = []
        self.__spawned = {}
        self.__step_of_call = {}
        self.__prompt = 0
        self.__completion = 0
        self.__cached = 0
        self.__cost = 0.0

    @property
    def steps(self) -> list[Step]:
        return list(self.__steps)

    @property
    def subsessions(self) -> list[tuple[str, str]]:
        """``(subsession_id, agent)`` of every sub-agent run, in start order."""
        return list(self.__subsessions)

    def feed(self, lines: list[dict[str, object]]) -> None:
        for line in lines:
            role = line.get("role")
            if role == "assistant":
                self.__assistant(line)
            elif role == "user":
                self.__user(line)
            else:
                self.__marker(line)

    def final_metrics(self, subagents: list[Trajectory]) -> FinalMetrics:
        prompt, completion, cached, cost = (
            self.__prompt,
            self.__completion,
            self.__cached,
            self.__cost,
        )
        for sub in subagents:
            metrics = sub.final_metrics
            if metrics is None:
                continue
            prompt += metrics.total_prompt_tokens or 0
            completion += metrics.total_completion_tokens or 0
            cached += metrics.total_cached_tokens or 0
            cost += metrics.total_cost_usd or 0.0
        return FinalMetrics(
            total_prompt_tokens=prompt,
            total_completion_tokens=completion,
            total_cached_tokens=cached or None,
            total_cost_usd=round(cost, 6) or None,
            total_steps=len(self.__steps),
        )

    # -- lines ---------------------------------------------------------------

    def __assistant(self, line: dict[str, object]) -> None:
        texts: list[str] = []
        thoughts: list[str] = []
        calls: list[ToolCall] = []
        for block in _blocks(line.get("content")):
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "thinking":
                thoughts.append(str(block.get("thinking", "")))
            elif kind == "tool_use":
                call_id = str(block.get("id", ""))
                name = str(block.get("name", ""))
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        tool_call_id=call_id,
                        function_name=name,
                        arguments=cast(dict[str, object], arguments)
                        if isinstance(arguments, dict)
                        else {},
                    )
                )
                if name.startswith(_SPAWN_PREFIX):
                    self.__pending_spawns.append(call_id)
        usage, self.__pending_usage = self.__pending_usage, None
        step = Step(
            step_id=len(self.__steps) + 1,
            timestamp=_timestamp(line),
            source="agent",
            model_name=str(usage["model"]) if usage and usage.get("model") else None,
            message="\n\n".join(t for t in texts if t),
            reasoning_content="\n\n".join(t for t in thoughts if t) or None,
            tool_calls=calls or None,
            metrics=self.__metrics(usage),
            llm_call_count=1,
        )
        self.__steps.append(step)
        for call in calls:
            self.__step_of_call[call.tool_call_id] = step

    def __user(self, line: dict[str, object]) -> None:
        content = line.get("content")
        if isinstance(content, str):
            self.__append("user", content, line)
            return
        texts: list[str] = []
        for block in _blocks(content):
            kind = block.get("type")
            if kind == "tool_result":
                self.__observe(block)
            elif kind == "text":
                texts.append(str(block.get("text", "")))
        if any(texts):
            self.__append("user", "\n\n".join(t for t in texts if t), line)

    def __marker(self, line: dict[str, object]) -> None:
        kind = line.get("type")
        if kind == "usage":
            self.__pending_usage = line
        elif kind == "subsession_start":
            subsession_id = str(line.get("subsession_id", ""))
            self.__subsessions.append((subsession_id, str(line.get("agent", ""))))
            if self.__pending_spawns:
                self.__spawned[self.__pending_spawns.pop(0)] = subsession_id
        elif kind == "compaction":
            summary = str(line.get("summary", "") or "")
            self.__append("system", f"[kodo compacted the context]\n\n{summary}".strip(), line)
        elif kind == "error":
            self.__append("system", f"[kodo error] {line.get('message', '')}", line)

    # -- helpers ---------------------------------------------------------------

    def __append(
        self, source: Literal["user", "system"], message: str, line: dict[str, object]
    ) -> None:
        self.__steps.append(
            Step(
                step_id=len(self.__steps) + 1,
                timestamp=_timestamp(line),
                source=source,
                message=message,
            )
        )

    def __observe(self, block: dict[str, object]) -> None:
        call_id = str(block.get("tool_use_id", ""))
        step = self.__step_of_call.get(call_id)
        if step is None:
            return  # a result for a call this log never showed (e.g. after compaction)
        raw = block.get("content")
        content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        subsession_id = self.__spawned.get(call_id)
        result = ObservationResult(
            source_call_id=call_id,
            content=content,
            subagent_trajectory_ref=[SubagentTrajectoryRef(trajectory_id=subsession_id)]
            if subsession_id
            else None,
        )
        if step.observation is None:
            step.observation = Observation(results=[result])
        else:
            step.observation.results.append(result)

    def __metrics(self, usage: dict[str, object] | None) -> Metrics | None:
        if usage is None:
            return None
        tokens = usage.get("last_call_tokens")
        if not isinstance(tokens, dict):
            return None
        counts = cast(dict[str, object], tokens)
        cache_read = _int(counts.get("cache_read"))
        prompt = _int(counts.get("input")) + cache_read + _int(counts.get("cache_write"))
        completion = _int(counts.get("output"))
        raw_cost = usage.get("usd_cost")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
        self.__prompt += prompt
        self.__completion += completion
        self.__cached += cache_read
        self.__cost += cost
        return Metrics(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cache_read or None,
            cost_usd=cost or None,
            extra={"stop_reason": usage.get("stop_reason"), "agent": usage.get("agent")},
        )


def _blocks(content: object) -> list[dict[str, object]]:
    if not isinstance(content, list):
        return []
    return [cast(dict[str, object], b) for b in content if isinstance(b, dict)]


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _timestamp(line: dict[str, object]) -> str | None:
    value = line.get("ts")
    return value if isinstance(value, str) and value else None


def _read_lines(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[dict[str, object]] = []
    for raw in text.splitlines():
        try:
            loaded: object = json.loads(raw)
        except ValueError:
            continue  # a torn last line from a killed run
        if isinstance(loaded, dict):
            lines.append(cast(dict[str, object], loaded))
    return lines
