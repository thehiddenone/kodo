"""Tool implementations for every tool in :mod:`kodo.toolspecs`.

This package is a dedicated import tier **between** ``toolspecs`` (T2) and
``subagents``/``llms`` (T3): it may import only from T0/T1/T2 (``common``,
``project``, ``toolchains``, ``state``, ``security``, ``transport``,
``workspace``, ``toolspecs``) and is consumed from above by ``runtime``.  It
must never import ``subagents``, ``llms``, or ``runtime`` — the collaborators
those would provide are expressed here as structural Protocols
(:class:`GateLike`, :class:`SessionLike`, :class:`SubagentRunner`) and injected.

There is a single unified tool surface: every agent (orchestrator included)
gets exactly the tools its frontmatter declares, dispatched through one
:class:`ToolDispatcher`.  Each tool is a :class:`Tool` subclass in its own
``_<tool_name>.py`` module, bound to one run's :class:`ToolContext`, mirroring
the ``kodo.toolspecs`` one-file-per-tool convention.
"""

from __future__ import annotations

from ._ask_user import AskUserTool
from ._context import (
    ApprovalLike,
    GateLike,
    QuestionLike,
    SessionLike,
    SubagentRunner,
    ToolContext,
)
from ._copy_file import CopyFileTool
from ._create_file import CreateFileTool
from ._delete_file import DeleteFileTool
from ._dispatch import DISPATCHABLE_TOOLS_BY_NAME, ToolDispatcher, tools_for_agent
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

__all__ = [
    "DISPATCHABLE_TOOLS_BY_NAME",
    "ApprovalLike",
    "AskUserTool",
    "CopyFileTool",
    "CreateFileTool",
    "DeleteFileTool",
    "EditFileTool",
    "EscalateBlockerTool",
    "FinalizeProjectTool",
    "GateLike",
    "ListArtifactsTool",
    "MoveFileTool",
    "PublishArtifactTool",
    "QueryFrontierTool",
    "QuestionLike",
    "ReadArtifactTool",
    "ReportArtifactCompletedTool",
    "RequestUserReviewArtifactTool",
    "RollbackTool",
    "RunAuthorCriticIterationTool",
    "RunCommandTool",
    "RunSubagentTool",
    "SessionLike",
    "SubagentRunner",
    "Tool",
    "ToolContext",
    "ToolDispatcher",
    "tools_for_agent",
]
