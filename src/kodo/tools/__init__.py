"""Tool implementations for every tool in :mod:`kodo.toolspecs`.

This package is a dedicated import tier **between** ``toolspecs`` (T2) and
``subagents``/``llms`` (T3): it may import only from T0/T1/T2 (``common``,
``project``, ``toolchains``, ``state``, ``security``, ``transport``,
``workspace``, ``toolspecs``) and is consumed from above by ``runtime``.  It
must never import ``subagents``, ``llms``, or ``runtime`` — the collaborators
those would provide are expressed here as structural Protocols
(:class:`GateLike`, :class:`SessionLike`, :class:`EngineServices`) and injected.

There is a single unified tool surface: every agent (guide included)
gets exactly the tools its frontmatter declares, dispatched through one
:class:`ToolDispatcher`.  Each tool is a :class:`Tool` subclass in its own
``_<tool_name>.py`` module, bound to one run's :class:`ToolContext`, mirroring
the ``kodo.toolspecs`` one-file-per-tool convention.
"""

from __future__ import annotations

from ._ask_user import AskUserTool
from ._context import (
    ApprovalLike,
    EngineServices,
    GateLike,
    QuestionLike,
    RootPath,
    SessionLike,
    ToolContext,
)
from ._disable_autonomous_mode import DisableAutonomousModeTool
from ._dispatch import DISPATCHABLE_TOOLS_BY_NAME, ToolDispatcher, tools_for_agent
from ._edit_file import EditFileTool
from ._escalate_blocker import EscalateBlockerTool
from ._filesystem import FilesystemTool
from ._finalize_project import FinalizeProjectTool
from ._find_files import FindFilesTool
from ._find_text_in_files import FindTextInFilesTool
from ._get_root_paths import GetRootPathsTool
from ._list_artifacts import ListArtifactsTool
from ._paths import (
    LogicalPathResolver,
    PathResolver,
    ProjectPathResolver,
    resolve_logical,
    resolve_within,
)
from ._publish_artifact import PublishArtifactTool
from ._query_frontier import QueryFrontierTool
from ._read_artifact import ReadArtifactTool
from ._report_artifact_completed import ReportArtifactCompletedTool
from ._request_user_review_artifact import RequestUserReviewArtifactTool
from ._return_result import ReturnResultTool
from ._rollback import RollbackTool
from ._run_author_critic_iteration import RunAuthorCriticIterationTool
from ._run_command import RunCommandTool
from ._run_subagent import RunSubagentTool
from ._tool import Tool

__all__ = [
    "DISPATCHABLE_TOOLS_BY_NAME",
    "ApprovalLike",
    "AskUserTool",
    "EngineServices",
    "DisableAutonomousModeTool",
    "EditFileTool",
    "EscalateBlockerTool",
    "FilesystemTool",
    "FinalizeProjectTool",
    "FindFilesTool",
    "FindTextInFilesTool",
    "GateLike",
    "GetRootPathsTool",
    "ListArtifactsTool",
    "LogicalPathResolver",
    "PathResolver",
    "ProjectPathResolver",
    "PublishArtifactTool",
    "QueryFrontierTool",
    "QuestionLike",
    "ReadArtifactTool",
    "ReportArtifactCompletedTool",
    "RequestUserReviewArtifactTool",
    "ReturnResultTool",
    "RollbackTool",
    "RootPath",
    "RunAuthorCriticIterationTool",
    "RunCommandTool",
    "RunSubagentTool",
    "SessionLike",
    "Tool",
    "ToolContext",
    "ToolDispatcher",
    "resolve_logical",
    "resolve_within",
    "tools_for_agent",
]
