"""Tool implementations for every tool in :mod:`kodo.toolspecs`.

This package is a dedicated import tier **between** ``toolspecs`` (T2) and
``subagents``/``llms`` (T3): it may import only from T0/T1/T2 (``common``,
``project``, ``toolchains``, ``state``, ``security``, ``transport``,
``guided_state``, ``skills``, ``toolspecs``) and is consumed from above by
``runtime``.  It
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

from ._add_local_llm_quant import AddLocalLlmQuantTool
from ._ask_user import AskUserTool
from ._context import (
    AgentReview,
    AgentReviewConcern,
    AgentWorkshopLike,
    ApprovalLike,
    EditReviewFeedbackLike,
    EditReviewLike,
    EngineServices,
    GateLike,
    LocalCatalogLike,
    PermissionLike,
    PermissionPartLike,
    RootPath,
    SecurityDecisionLike,
    SecurityLike,
    SessionLike,
    ToolContext,
)
from ._copy_builtin_agent import CopyBuiltinAgentTool
from ._create_directory import CreateDirectoryTool
from ._create_file import CreateFileTool
from ._disable_autonomous_mode import DisableAutonomousModeTool
from ._dispatch import (
    DISPATCHABLE_TOOLS_BY_NAME,
    ToolDispatcher,
    canonical_tool_call,
    tools_for_agent,
)
from ._document_status import document_status, status_from_state
from ._edit_agent import EditAgentTool
from ._edit_file import EditFileTool, compute_new_content
from ._edit_review import should_review_edit
from ._filesystem import FilesystemTool
from ._finalize_project import FinalizeProjectTool
from ._find_files import FindFilesTool
from ._find_text_in_files import FindTextInFilesTool
from ._get_findings import GetFindingsTool
from ._get_plan import GetPlanTool
from ._get_root_paths import GetRootPathsTool
from ._get_web_search_state import GetWebSearchStateTool
from ._guided_dev_status import GuidedDevStatusTool
from ._lint_agent import LintAgentTool
from ._list_agents import ListAgentsTool
from ._list_local_llms import ListLocalLlmsTool
from ._list_tools import ListToolsTool
from ._paths import (
    LogicalPathResolver,
    NoWorkspaceError,
    PathResolver,
    resolve_logical,
    resolve_within,
    root_for,
)
from ._plan_step_forward import PlanStepForwardTool
from ._query_search_engine import QuerySearchEngineTool
from ._read_agent import ReadAgentTool
from ._read_attachment import ReadAttachmentTool
from ._read_file import ReadFileTool
from ._read_gguf_header import ReadGgufHeaderTool
from ._read_hf_model import ReadHfModelTool
from ._read_webpage import ReadWebpageTool
from ._remaining_time import RemainingTimeTool
from ._report_agent_review import ReportAgentReviewTool
from ._return_result import ReturnResultTool
from ._rollback import RollbackTool
from ._run_command import RunCommandTool
from ._run_subagent import RunSubagentTool
from ._scaffold_new_project import ScaffoldNewProjectTool
from ._set_mtp_heads import SetMtpHeadsTool
from ._tool import Tool
from ._toolchain_build import ToolchainBuildTool
from ._toolchain_deps import ToolchainDepsTool
from ._update_web_search_state import UpdateWebSearchStateTool
from ._use_skill import UseSkillTool
from ._validate_agent import ValidateAgentTool
from ._wait import WaitTool
from ._web_search import WebSearchTool
from ._write_agent import WriteAgentTool

__all__ = [
    "AgentReview",
    "AgentReviewConcern",
    "AgentWorkshopLike",
    "CopyBuiltinAgentTool",
    "DISPATCHABLE_TOOLS_BY_NAME",
    "ApprovalLike",
    "AddLocalLlmQuantTool",
    "AskUserTool",
    "CreateDirectoryTool",
    "CreateFileTool",
    "DisableAutonomousModeTool",
    "EditAgentTool",
    "EditFileTool",
    "EditReviewFeedbackLike",
    "EditReviewLike",
    "EngineServices",
    "FilesystemTool",
    "FinalizeProjectTool",
    "FindFilesTool",
    "FindTextInFilesTool",
    "GateLike",
    "LintAgentTool",
    "ListAgentsTool",
    "ListToolsTool",
    "LocalCatalogLike",
    "GetFindingsTool",
    "GetPlanTool",
    "PlanStepForwardTool",
    "GetRootPathsTool",
    "GetWebSearchStateTool",
    "GuidedDevStatusTool",
    "ListLocalLlmsTool",
    "LogicalPathResolver",
    "NoWorkspaceError",
    "PathResolver",
    "PermissionLike",
    "PermissionPartLike",
    "QuerySearchEngineTool",
    "ReadAgentTool",
    "ReadAttachmentTool",
    "ReadFileTool",
    "ReadGgufHeaderTool",
    "ReadHfModelTool",
    "ReadWebpageTool",
    "RemainingTimeTool",
    "ReportAgentReviewTool",
    "ReturnResultTool",
    "RollbackTool",
    "RootPath",
    "RunCommandTool",
    "RunSubagentTool",
    "ScaffoldNewProjectTool",
    "SetMtpHeadsTool",
    "SecurityDecisionLike",
    "SecurityLike",
    "SessionLike",
    "Tool",
    "ToolContext",
    "ToolDispatcher",
    "ToolchainBuildTool",
    "ToolchainDepsTool",
    "UpdateWebSearchStateTool",
    "UseSkillTool",
    "ValidateAgentTool",
    "WaitTool",
    "WebSearchTool",
    "WriteAgentTool",
    "compute_new_content",
    "document_status",
    "resolve_logical",
    "resolve_within",
    "root_for",
    "should_review_edit",
    "status_from_state",
    "canonical_tool_call",
    "tools_for_agent",
]
