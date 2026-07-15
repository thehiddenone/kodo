"""Tool implementations for every tool in :mod:`kodo.toolspecs`.

This package is a dedicated import tier **between** ``toolspecs`` (T2) and
``subagents``/``llms`` (T3): it may import only from T0/T1/T2 (``common``,
``project``, ``toolchains``, ``state``, ``security``, ``transport``,
``guided_state``, ``toolspecs``) and is consumed from above by ``runtime``.  It
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
    PermissionLike,
    RootPath,
    SecurityDecisionLike,
    SecurityLike,
    SessionLike,
    ToolContext,
)
from ._create_directory import CreateDirectoryTool
from ._create_file import CreateFileTool
from ._create_new_project import CreateNewProjectTool
from ._disable_autonomous_mode import DisableAutonomousModeTool
from ._dispatch import DISPATCHABLE_TOOLS_BY_NAME, ToolDispatcher, tools_for_agent
from ._document_feedback import DocumentFeedbackTool
from ._edit_file import EditFileTool
from ._escalate_blocker import EscalateBlockerTool
from ._filesystem import FilesystemTool
from ._finalize_project import FinalizeProjectTool
from ._find_files import FindFilesTool
from ._find_text_in_files import FindTextInFilesTool
from ._get_root_paths import GetRootPathsTool
from ._get_web_search_state import GetWebSearchStateTool
from ._guided_dev_status import GuidedDevStatusTool
from ._init_project import InitProjectTool
from ._paths import (
    LogicalPathResolver,
    PathResolver,
    ProjectPathResolver,
    resolve_logical,
    resolve_within,
)
from ._query_search_engine import QuerySearchEngineTool
from ._read_file import ReadFileTool
from ._read_webpage import ReadWebpageTool
from ._remaining_time import RemainingTimeTool
from ._return_result import ReturnResultTool
from ._rollback import RollbackTool
from ._run_author_critic_iteration import RunAuthorCriticIterationTool
from ._run_command import RunCommandTool
from ._run_subagent import RunSubagentTool
from ._tool import Tool
from ._toolchain_build import ToolchainBuildTool
from ._toolchain_deps import ToolchainDepsTool
from ._update_web_search_state import UpdateWebSearchStateTool
from ._wait import WaitTool
from ._web_search import WebSearchTool

__all__ = [
    "DISPATCHABLE_TOOLS_BY_NAME",
    "ApprovalLike",
    "AskUserTool",
    "CreateDirectoryTool",
    "CreateFileTool",
    "CreateNewProjectTool",
    "DisableAutonomousModeTool",
    "DocumentFeedbackTool",
    "EditFileTool",
    "EngineServices",
    "EscalateBlockerTool",
    "FilesystemTool",
    "FinalizeProjectTool",
    "FindFilesTool",
    "FindTextInFilesTool",
    "GateLike",
    "GetRootPathsTool",
    "GetWebSearchStateTool",
    "GuidedDevStatusTool",
    "InitProjectTool",
    "LogicalPathResolver",
    "PathResolver",
    "PermissionLike",
    "ProjectPathResolver",
    "QuerySearchEngineTool",
    "ReadFileTool",
    "ReadWebpageTool",
    "RemainingTimeTool",
    "ReturnResultTool",
    "RollbackTool",
    "RootPath",
    "RunAuthorCriticIterationTool",
    "RunCommandTool",
    "RunSubagentTool",
    "SecurityDecisionLike",
    "SecurityLike",
    "SessionLike",
    "Tool",
    "ToolContext",
    "ToolDispatcher",
    "ToolchainBuildTool",
    "ToolchainDepsTool",
    "UpdateWebSearchStateTool",
    "WaitTool",
    "WebSearchTool",
    "resolve_logical",
    "resolve_within",
    "tools_for_agent",
]
