"""Unified tool dispatch — one surface for every agent.

There is no orchestrator-vs-leaf split: every agent (the orchestrator
included) is granted exactly the tools its frontmatter declares, and every
tool call — whoever makes it — is routed through a single
:class:`ToolDispatcher` to the matching :class:`~kodo.tools.Tool` subclass.

:func:`tools_for_agent` resolves an agent's declared tool *names* to the
``ToolSpec`` objects passed to the LLM, skipping any name that has no tool
class here (forward-compatibility with spec-only placeholders).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from kodo.toolspecs import (
    ASK_USER,
    COPY_FILE,
    CREATE_FILE,
    DELETE_FILE,
    EDIT_FILE,
    ESCALATE_BLOCKER,
    FINALIZE_PROJECT,
    LIST_ARTIFACTS,
    MOVE_FILE,
    PUBLISH_ARTIFACT,
    QUERY_FRONTIER,
    READ_ARTIFACT,
    REPORT_ARTIFACT_COMPLETED,
    REQUEST_USER_REVIEW_ARTIFACT,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_COMMAND,
    RUN_SUBAGENT,
    ToolSpec,
)
from kodo.workspace import ProjectIndex, Workspace

from ._ask_user import AskUserTool
from ._context import GateLike, SessionLike, SubagentRunner, ToolContext
from ._copy_file import CopyFileTool
from ._create_file import CreateFileTool
from ._delete_file import DeleteFileTool
from ._edit_file import EditFileTool
from ._escalate_blocker import EscalateBlockerTool
from ._finalize_project import FinalizeProjectTool
from ._list_artifacts import ListArtifactsTool
from ._move_file import MoveFileTool
from ._publish_artifact import PublishArtifactTool
from ._query_frontier import QueryFrontierTool
from ._read_artifact import ReadArtifactTool
from ._report_artifact_completed import ReportArtifactCompletedTool
from ._request_user_review_artifact import RequestUserReviewArtifactTool
from ._rollback import RollbackTool
from ._run_author_critic_iteration import RunAuthorCriticIterationTool
from ._run_command import RunCommandTool
from ._run_subagent import RunSubagentTool
from ._tool import Tool

__all__ = ["DISPATCHABLE_TOOLS_BY_NAME", "ToolDispatcher", "tools_for_agent"]

_log = logging.getLogger(__name__)

# Single source of truth pairing each dispatchable ToolSpec with its Tool class.
# Adding a tool means adding one (spec, Tool-subclass) row here.
_TOOL_CLASSES: tuple[tuple[ToolSpec, type[Tool]], ...] = (
    (PUBLISH_ARTIFACT, PublishArtifactTool),
    (READ_ARTIFACT, ReadArtifactTool),
    (ESCALATE_BLOCKER, EscalateBlockerTool),
    (ASK_USER, AskUserTool),
    (REQUEST_USER_REVIEW_ARTIFACT, RequestUserReviewArtifactTool),
    (REPORT_ARTIFACT_COMPLETED, ReportArtifactCompletedTool),
    (CREATE_FILE, CreateFileTool),
    (EDIT_FILE, EditFileTool),
    (DELETE_FILE, DeleteFileTool),
    (COPY_FILE, CopyFileTool),
    (MOVE_FILE, MoveFileTool),
    (RUN_COMMAND, RunCommandTool),
    (QUERY_FRONTIER, QueryFrontierTool),
    (LIST_ARTIFACTS, ListArtifactsTool),
    (RUN_SUBAGENT, RunSubagentTool),
    (RUN_AUTHOR_CRITIC_ITERATION, RunAuthorCriticIterationTool),
    (ROLLBACK, RollbackTool),
    (FINALIZE_PROJECT, FinalizeProjectTool),
)

_CLASSES_BY_NAME: dict[str, type[Tool]] = {spec.name: cls for spec, cls in _TOOL_CLASSES}

# Every tool with an implementation, keyed by name. Consumed by tools_for_agent
# to build each agent's LLM-facing tool list.
DISPATCHABLE_TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec, _ in _TOOL_CLASSES}


def tools_for_agent(tool_names: frozenset[str]) -> list[ToolSpec]:
    """Resolve an agent's declared tool names to dispatchable ``ToolSpec``s.

    Names with no tool class (spec-only placeholders such as ``post_update``)
    are silently skipped, matching the prompt/surface contract.

    Args:
        tool_names: The agent's frontmatter ``tools:`` set (e.g. ``agent.tools``).

    Returns:
        list[ToolSpec]: Specs to pass to the LLM, in catalog order.
    """
    return [
        DISPATCHABLE_TOOLS_BY_NAME[name]
        for name in tool_names
        if name in DISPATCHABLE_TOOLS_BY_NAME
    ]


class ToolDispatcher:
    """Routes one agent run's tool calls to their :class:`Tool` instances.

    One instance is created per agent run (orchestrator or leaf). It owns a
    :class:`~kodo.tools.ToolContext` carrying the injected collaborators plus
    the run's mutable state, and exposes that state (``published_ids``,
    ``stop_requested``) back to the engine after the run.

    Args:
        workspace: Shared artifact store.
        index: Live artifact index.
        gate: Approval/question gate.
        session: Mutable session state.
        runner: Sub-agent launcher.
        rollback_fn: Rollback callback.
        complete_fn: Artifact-completion (promotion) callback.
        agent_name: Name of the running agent.
        session_id: Session ID attached to published artifacts.
        autonomous: Whether autonomous mode is active.
    """

    __ctx: ToolContext

    def __init__(
        self,
        *,
        workspace: Workspace,
        index: ProjectIndex,
        gate: GateLike,
        session: SessionLike,
        runner: SubagentRunner,
        rollback_fn: Callable[[str], Awaitable[None]],
        complete_fn: Callable[[str], Awaitable[None]],
        agent_name: str,
        session_id: str,
        autonomous: bool = False,
    ) -> None:
        self.__ctx = ToolContext(
            workspace=workspace,
            index=index,
            gate=gate,
            session=session,
            runner=runner,
            rollback_fn=rollback_fn,
            complete_fn=complete_fn,
            agent_name=agent_name,
            session_id=session_id,
            autonomous=autonomous,
        )

    @property
    def published_ids(self) -> list[str]:
        """Artifact IDs published during this run."""
        return list(self.__ctx.published_ids)

    @property
    def stop_requested(self) -> bool:
        """``True`` once the agent called ``escalate_blocker``."""
        return self.__ctx.stop_requested

    async def dispatch(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Route one tool call to its handler and return a JSON-encoded result.

        Instantiates the matching :class:`Tool` subclass bound to this run's
        context and invokes its :meth:`Tool.handle`.

        Args:
            tool_name: Tool name from :data:`DISPATCHABLE_TOOLS_BY_NAME`.
            tool_input: Parsed JSON input from the LLM tool-use block.

        Returns:
            str: JSON-encoded result returned to the LLM as a tool result.
        """
        tool_cls = _CLASSES_BY_NAME.get(tool_name)
        if tool_cls is None:
            _log.warning(
                "ToolDispatcher: unknown tool %r from %s", tool_name, self.__ctx.agent_name
            )
            return json.dumps({"error": f"Unknown tool: {tool_name!r}"})
        return await tool_cls(self.__ctx).handle(tool_input)
