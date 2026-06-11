"""Orchestrator tool surface — the 8 tools the Orchestrator may call (FR-ORCH-03).

Each tool is defined as a :class:`~kodo.llms._interface.ToolSpec` whose JSON
schema is the contract the Orchestrator LLM sees.  The :class:`ToolSurface`
class holds the async handlers that the engine dispatches to when the
Orchestrator calls one of these tools.

Tools that spawn sub-agents (``start_subagent``, ``run_author_critic_iteration``)
are stubs in this step; full implementation arrives in Step 5.  All other tools
are fully implemented.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from kodo.llms._interface import ToolSpec
from kodo.workspace._models import ArtifactType

from ._gates import ApprovalResponse, GateOrchestrator
from ._index import ProjectIndex
from ._session import SessionState

__all__ = [
    "ORCHESTRATOR_TOOLS",
    "ORCHESTRATOR_TOOLS_BY_NAME",
    "RunAuthorCriticFn",
    "RunSubagentFn",
    "ToolSurface",
]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical per-responsibility artifact execution order (§2.2)
# ---------------------------------------------------------------------------

_PER_RESPONSIBILITY_ORDER: tuple[ArtifactType, ...] = (
    ArtifactType.FUNCTIONAL_DESIGN,
    ArtifactType.TEST_PLAN,
    ArtifactType.TEST,
    ArtifactType.CODE,
)

# ---------------------------------------------------------------------------
# ToolSpec definitions — one per FR-ORCH-03 tool
# ---------------------------------------------------------------------------

COMPUTE_FRONTIER = ToolSpec(
    name="compute_frontier",
    description=(
        "Return the per-responsibility frontier: for each responsibility_code, "
        "the earliest artifact type in the canonical execution order "
        "(functional-design → test-plan → test → code) that has zero completed "
        "entries.  A responsibility absent from the result has all four types completed."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)

LIST_ARTIFACTS = ToolSpec(
    name="list_artifacts",
    description=(
        "Query the workspace index.  All supplied filters are combined with AND. "
        "At least one filter is required."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "Exact artifact UUID."},
            "type": {
                "type": "string",
                "enum": [t.value for t in ArtifactType],
                "description": "Artifact type filter.",
            },
            "responsibility_code": {"type": "string", "description": "Responsibility codename."},
            "requirement_id": {
                "type": "string",
                "description": "Requirement ID that must be in requirement_ids.",
            },
            "author": {
                "type": "string",
                "description": "Sub-agent name that published the artifact.",
            },
            "state": {
                "type": "string",
                "enum": ["completed", "in_flight"],
                "description": "Lifecycle state filter.",
            },
        },
        "required": [],
        "minProperties": 1,
    },
)

START_SUBAGENT = ToolSpec(
    name="start_subagent",
    description=(
        "Invoke a leaf sub-agent by name.  Blocks until the sub-agent session "
        "completes.  Returns the artifact IDs the sub-agent published."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Sub-agent name from the registry (e.g. 'narrative_author').",
            },
            "task_message": {
                "type": "string",
                "description": "Task message injected as the initial uncached user turn.",
            },
            "input_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Artifact IDs the sub-agent may read via read_artifact.",
            },
        },
        "required": ["name", "task_message"],
    },
)

RUN_AUTHOR_CRITIC_ITERATION = ToolSpec(
    name="run_author_critic_iteration",
    description=(
        "Execute one round of the Author/Critic loop.  "
        "Spawns the Author (with previous_artifact_id as feedback context when provided), "
        "then spawns the Critic against the Author's output.  "
        "Returns the artifact ID, verdict, and concerns.  "
        "Call again to iterate; the Orchestrator decides when to stop."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_name": {"type": "string", "description": "Author sub-agent name."},
            "critic_name": {"type": "string", "description": "Critic sub-agent name."},
            "input_artifact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Input artifact IDs passed to the Author.",
            },
            "previous_artifact_id": {
                "type": "string",
                "description": (
                    "Artifact ID of the prior Author output.  When set, the Author "
                    "receives it as revision context alongside the Critic's concerns."
                ),
            },
        },
        "required": ["author_name", "critic_name", "input_artifact_ids"],
    },
)

REQUEST_USER_APPROVAL = ToolSpec(
    name="request_user_approval",
    description=(
        "Surface an approval gate to the user.  "
        "Blocks until the user responds with agree or feedback.  "
        "In autonomous mode the engine auto-resolves to agree."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "gate_type": {
                "type": "string",
                "enum": [
                    "narrative",
                    "architecture",
                    "requirements",
                    "plan",
                    "design",
                    "test_plan",
                    "implementation",
                    "final",
                ],
                "description": "Gate type matching the canonical sequence moment.",
            },
            "artifact_id": {
                "type": "string",
                "description": "ID of the artifact the user should review.",
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary shown to the user.",
            },
        },
        "required": ["gate_type", "summary"],
    },
)

ASK_USER = ToolSpec(
    name="ask_user",
    description=(
        "Surface a free-form or choice question to the user. "
        "Blocks until the user responds. "
        "Use for clarification, confirmation before destructive operations, and intake."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to display."},
            "mode": {
                "type": "string",
                "enum": ["free_text", "choice"],
                "description": "free_text: user types a reply; choice: user picks from choices.",
            },
            "choices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["key", "label"],
                },
                "description": "Required when mode='choice'.",
            },
        },
        "required": ["question", "mode"],
    },
)

ROLLBACK = ToolSpec(
    name="rollback",
    description=(
        "Invoke the rollback procedure.  "
        "Restores src/ and gen/ from the target mirror commit, clears the workspace, "
        "and starts a fresh Orchestrator session.  "
        "The Orchestrator MUST confirm with the user via ask_user before calling this."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target_sha": {
                "type": "string",
                "description": "Mirror commit SHA to roll back to.",
            },
        },
        "required": ["target_sha"],
    },
)

FINALIZE_PROJECT = ToolSpec(
    name="finalize_project",
    description=(
        "Terminal call: the project is complete.  "
        "Transitions state.phase to 'done' and ends the Orchestrator session."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)

ORCHESTRATOR_TOOLS: list[ToolSpec] = [
    COMPUTE_FRONTIER,
    LIST_ARTIFACTS,
    START_SUBAGENT,
    RUN_AUTHOR_CRITIC_ITERATION,
    REQUEST_USER_APPROVAL,
    ASK_USER,
    ROLLBACK,
    FINALIZE_PROJECT,
]

ORCHESTRATOR_TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ORCHESTRATOR_TOOLS}


# ---------------------------------------------------------------------------
# ToolSurface — async handlers
# ---------------------------------------------------------------------------

# Callback types for the engine implementations injected at startup.
RunSubagentFn = Callable[[str, str, list[str]], Awaitable[list[str]]]
RunAuthorCriticFn = Callable[
    [str, str, list[str], "str | None"],
    Awaitable[dict[str, object]],
]
RollbackFn = Callable[[str], Awaitable[None]]


class ToolSurface:
    """Async handlers for the 8 Orchestrator tools.

    Each public method corresponds to one tool in :data:`ORCHESTRATOR_TOOLS`
    and is called by the engine's tool dispatcher when the Orchestrator LLM
    emits a matching tool-use block.

    Args:
        index: Live in-memory artifact index.
        gate: Approval-gate orchestrator (handles request_user_approval blocking).
        session: Mutable session state (finalize_project writes phase here).
        run_subagent_fn: Callback to spawn a leaf sub-agent.
        run_author_critic_fn: Callback to run one Author/Critic iteration.
        rollback_fn: Callback to invoke the rollback procedure.
    """

    __index: ProjectIndex
    __gate: GateOrchestrator
    __session: SessionState
    __run_subagent_fn: RunSubagentFn
    __run_author_critic_fn: RunAuthorCriticFn
    __rollback_fn: RollbackFn

    def __init__(
        self,
        index: ProjectIndex,
        gate: GateOrchestrator,
        session: SessionState,
        run_subagent_fn: RunSubagentFn,
        run_author_critic_fn: RunAuthorCriticFn,
        rollback_fn: RollbackFn,
    ) -> None:
        """Initialise the tool surface.

        Args:
            index: Live artifact index.
            gate: Approval-gate orchestrator.
            session: Current session state.
            run_subagent_fn: Async callback that spawns a solo sub-agent.
            run_author_critic_fn: Async callback that runs one Author/Critic round.
            rollback_fn: Async callback that executes rollback.
        """
        self.__index = index
        self.__gate = gate
        self.__session = session
        self.__run_subagent_fn = run_subagent_fn
        self.__run_author_critic_fn = run_author_critic_fn
        self.__rollback_fn = rollback_fn

    async def dispatch(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Route a tool call from the Orchestrator to the matching handler.

        Args:
            tool_name: Name from :data:`ORCHESTRATOR_TOOLS_BY_NAME`.
            tool_input: Parsed JSON input from the LLM tool-use block.

        Returns:
            str: JSON-encoded tool result the engine returns to the LLM.
        """
        if tool_name == COMPUTE_FRONTIER.name:
            return await self.__compute_frontier()
        if tool_name == LIST_ARTIFACTS.name:
            return await self.__list_artifacts(tool_input)
        if tool_name == START_SUBAGENT.name:
            return await self.__start_subagent(tool_input)
        if tool_name == RUN_AUTHOR_CRITIC_ITERATION.name:
            return await self.__run_author_critic_iteration(tool_input)
        if tool_name == REQUEST_USER_APPROVAL.name:
            return await self.__request_user_approval(tool_input)
        if tool_name == ASK_USER.name:
            return await self.__ask_user(tool_input)
        if tool_name == ROLLBACK.name:
            return await self.__rollback(tool_input)
        if tool_name == FINALIZE_PROJECT.name:
            return await self.__finalize_project()
        return json.dumps({"error": f"Unknown orchestrator tool: {tool_name!r}"})

    # ------------------------------------------------------------------
    # compute_frontier
    # ------------------------------------------------------------------

    async def __compute_frontier(self) -> str:
        frontier: list[dict[str, str]] = []
        completed = self.__index.completed_entries()

        # Collect all responsibility codes that have at least one artifact
        resp_codes: set[str] = {e.responsibility_code for e in completed}

        for resp_code in sorted(resp_codes):
            completed_types = {e.type for e in completed if e.responsibility_code == resp_code}
            for artifact_type in _PER_RESPONSIBILITY_ORDER:
                if artifact_type not in completed_types:
                    frontier.append(
                        {
                            "responsibility_code": resp_code,
                            "next_type": artifact_type.value,
                        }
                    )
                    break  # only report the earliest missing type per responsibility

        return json.dumps({"frontier": frontier})

    # ------------------------------------------------------------------
    # list_artifacts
    # ------------------------------------------------------------------

    async def __list_artifacts(self, tool_input: dict[str, object]) -> str:
        artifact_id = tool_input.get("artifact_id")
        type_filter = tool_input.get("type")
        resp_code = tool_input.get("responsibility_code")
        req_id = tool_input.get("requirement_id")
        author = tool_input.get("author")
        state = tool_input.get("state")

        if not any([artifact_id, type_filter, resp_code, req_id, author, state]):
            return json.dumps({"error": "At least one filter is required."})

        entries = self.__index.all_entries()

        if artifact_id:
            entries = [e for e in entries if e.artifact_id == artifact_id]
        if type_filter:
            try:
                t = ArtifactType(str(type_filter))
                entries = [e for e in entries if e.type == t]
            except ValueError:
                return json.dumps({"error": f"Unknown artifact type: {type_filter!r}"})
        if resp_code:
            entries = [e for e in entries if e.responsibility_code == resp_code]
        if req_id:
            entries = [e for e in entries if str(req_id) in e.requirement_ids]
        if author:
            entries = [e for e in entries if e.author == author]
        if state:
            entries = [e for e in entries if e.state == state]

        result = [
            {
                "artifact_id": e.artifact_id,
                "type": e.type.value,
                "responsibility_code": e.responsibility_code,
                "filename_hint": e.filename_hint,
                "state": e.state,
                "author": e.author,
            }
            for e in entries
        ]
        return json.dumps({"artifacts": result})

    # ------------------------------------------------------------------
    # start_subagent (stub — Step 5 wires the real invocation)
    # ------------------------------------------------------------------

    async def __start_subagent(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", ""))
        task_message = str(tool_input.get("task_message", ""))
        input_ids_raw = tool_input.get("input_artifact_ids", [])
        input_ids = [str(i) for i in input_ids_raw] if isinstance(input_ids_raw, list) else []

        _log.info("start_subagent: name=%s input_ids=%s", name, input_ids)
        artifact_ids = await self.__run_subagent_fn(name, task_message, input_ids)
        return json.dumps({"artifact_ids": artifact_ids})

    # ------------------------------------------------------------------
    # run_author_critic_iteration
    # ------------------------------------------------------------------

    async def __run_author_critic_iteration(self, tool_input: dict[str, object]) -> str:
        author_name = str(tool_input.get("author_name", ""))
        critic_name = str(tool_input.get("critic_name", ""))
        input_ids_raw = tool_input.get("input_artifact_ids", [])
        input_ids = [str(i) for i in input_ids_raw] if isinstance(input_ids_raw, list) else []
        previous_id = tool_input.get("previous_artifact_id")

        _log.info(
            "run_author_critic_iteration: author=%s critic=%s previous=%s",
            author_name,
            critic_name,
            previous_id,
        )
        result = await self.__run_author_critic_fn(
            author_name,
            critic_name,
            input_ids,
            str(previous_id) if previous_id is not None else None,
        )
        return json.dumps(result)

    # ------------------------------------------------------------------
    # request_user_approval
    # ------------------------------------------------------------------

    async def __request_user_approval(self, tool_input: dict[str, object]) -> str:
        gate_type = str(tool_input.get("gate_type", ""))
        artifact_id = tool_input.get("artifact_id")
        summary = str(tool_input.get("summary", ""))

        _log.info("request_user_approval: gate_type=%s", gate_type)

        # Autonomous mode: auto-agree without surfacing to the user (FR-AUT-02)
        if self.__session.autonomous:
            _log.info("Autonomous mode: auto-agreeing gate %s", gate_type)
            return json.dumps({"action": "agree", "feedback_text": None})

        response: ApprovalResponse = await self.__gate.fire(
            gate_type,
            summary=summary,
            component=str(artifact_id) if artifact_id else None,
        )
        result: dict[str, object] = {"action": response.action}
        if response.feedback:
            result["feedback_text"] = response.feedback
        return json.dumps(result)

    # ------------------------------------------------------------------
    # ask_user (stub — Step 3 wires the real prompt.question machinery)
    # ------------------------------------------------------------------

    async def __ask_user(self, tool_input: dict[str, object]) -> str:
        question = str(tool_input.get("question", ""))
        mode = str(tool_input.get("mode", "free_text"))
        choices_raw = tool_input.get("choices")
        choices: list[dict[str, str]] | None = None
        if isinstance(choices_raw, list):
            choices = [
                {"key": str(c.get("key", "")), "label": str(c.get("label", ""))}
                for c in choices_raw
                if isinstance(c, dict)
            ]

        _log.info("ask_user: question=%r mode=%s", question[:80], mode)
        response = await self.__gate.fire_question(question, mode, choices)

        if mode == "choice":
            return json.dumps({"choice_key": response.choice_key})
        return json.dumps({"answer_text": response.answer_text})

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------

    async def __rollback(self, tool_input: dict[str, object]) -> str:
        target_sha = str(tool_input.get("target_sha", "")).strip()
        if not target_sha:
            return json.dumps({"error": "target_sha is required"})
        _log.info("rollback: target_sha=%s", target_sha[:12])
        await self.__rollback_fn(target_sha)
        return json.dumps({"status": "completed"})

    # ------------------------------------------------------------------
    # finalize_project
    # ------------------------------------------------------------------

    async def __finalize_project(self) -> str:
        self.__session.phase = "done"
        _log.info("finalize_project: session marked done")
        return json.dumps({"status": "done"})
