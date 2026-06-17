"""Orchestrator tool surface — the tools the Orchestrator may call (FR-ORCH-03).

Each tool is defined as a :class:`~kodo.toolspecs.ToolSpec` whose JSON schema
is the contract the Orchestrator LLM sees; the specs themselves live in
:mod:`kodo.toolspecs`.  The :class:`ToolSurface` class holds the async
handlers that the engine dispatches to when the Orchestrator calls one of
these tools.

Tools that spawn sub-agents (``run_subagent``, ``run_author_critic_iteration``)
are stubs in this step; full implementation arrives in Step 5.  All other tools
are fully implemented.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from kodo.toolspecs import (
    FINALIZE_PROJECT,
    LIST_ARTIFACTS,
    QUERY_FRONTIER,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_SUBAGENT,
    ToolSpec,
)
from kodo.toolspecs import (
    ORCHESTRATOR_ASK_USER as ASK_USER,
)
from kodo.workspace import ArtifactType, ProjectIndex

from ._gates import GateOrchestrator
from ._session import SessionState

__all__ = [
    "ORCHESTRATOR_TOOLS",
    "ORCHESTRATOR_TOOLS_BY_NAME",
    "RunAuthorCriticFn",
    "RunSubagentFn",
    "ToolSurface",
    "orchestrator_tools",
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
# Orchestrator tool catalog — specs live in kodo.toolspecs
# ---------------------------------------------------------------------------

ORCHESTRATOR_TOOLS: list[ToolSpec] = [
    QUERY_FRONTIER,
    LIST_ARTIFACTS,
    RUN_SUBAGENT,
    RUN_AUTHOR_CRITIC_ITERATION,
    ASK_USER,
    ROLLBACK,
    FINALIZE_PROJECT,
]

ORCHESTRATOR_TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ORCHESTRATOR_TOOLS}

# Orchestrator tools withheld in autonomous mode — kept in sync with
# ToolSpec.autonomous_mode == "unavailable" (see kodo.subagents._registry).
_AUTONOMOUS_DISABLED: frozenset[str] = frozenset({ASK_USER.name})


def orchestrator_tools(autonomous: bool) -> list[ToolSpec]:
    """Return the Orchestrator's tool list for the current mode.

    Args:
        autonomous: When ``True``, tools the user must answer (``ask_user``)
            are withheld, mirroring the leaf-agent registry's autonomous filter.

    Returns:
        list[ToolSpec]: The tools the Orchestrator may call in this mode.
    """
    if autonomous:
        return [t for t in ORCHESTRATOR_TOOLS if t.name not in _AUTONOMOUS_DISABLED]
    return list(ORCHESTRATOR_TOOLS)


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
        gate: Gate orchestrator (handles ask_user blocking).
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
        if tool_name == QUERY_FRONTIER.name:
            return await self.__query_frontier()
        if tool_name == LIST_ARTIFACTS.name:
            return await self.__list_artifacts(tool_input)
        if tool_name == RUN_SUBAGENT.name:
            return await self.__run_subagent(tool_input)
        if tool_name == RUN_AUTHOR_CRITIC_ITERATION.name:
            return await self.__run_author_critic_iteration(tool_input)
        if tool_name == ASK_USER.name:
            return await self.__ask_user(tool_input)
        if tool_name == ROLLBACK.name:
            return await self.__rollback(tool_input)
        if tool_name == FINALIZE_PROJECT.name:
            return await self.__finalize_project()
        return json.dumps({"error": f"Unknown orchestrator tool: {tool_name!r}"})

    # ------------------------------------------------------------------
    # query_frontier
    # ------------------------------------------------------------------

    async def __query_frontier(self) -> str:
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
    # run_subagent (stub — Step 5 wires the real invocation)
    # ------------------------------------------------------------------

    async def __run_subagent(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", ""))
        task_message = str(tool_input.get("task_message", ""))
        input_ids_raw = tool_input.get("input_artifact_ids", [])
        input_ids = [str(i) for i in input_ids_raw] if isinstance(input_ids_raw, list) else []

        _log.info("run_subagent: name=%s input_ids=%s", name, input_ids)
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
    # ask_user
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
