"""Kodo runtime — Guide substrate, index, bootstrap, gates, and tools.

Tool dispatch lives in :mod:`kodo.tools` (a lower import tier); the engine
builds a per-run :class:`~kodo.tools.ToolDispatcher` and resolves each agent's
tools via :func:`agent_tool_specs`, the one place that joins the static tool
catalog with the per-agent schemas only :mod:`kodo.agents` knows (see
:mod:`._agenttools`). Both are re-exported here for convenience.
"""

from kodo.tools import ToolDispatcher, tools_for_agent

from ._agenttools import agent_tool_specs
from ._checkpoints import CheckpointEntry, CheckpointState, MirrorDirtyError
from ._engine import WorkflowEngine
from ._gates import ApprovalResponse, GateOrchestrator, PermissionResponse
from ._llm_selection import LlmSelection, resolve_model_key
from ._security_rules import delete_global_security_rules, list_global_security_rules
from ._session import SessionState
from ._session_log import SessionLog
from ._session_params import ParamsSnapshot, ParamValue, diff_params

__all__ = [
    "ApprovalResponse",
    "CheckpointEntry",
    "CheckpointState",
    "GateOrchestrator",
    "LlmSelection",
    "MirrorDirtyError",
    "ParamValue",
    "ParamsSnapshot",
    "PermissionResponse",
    "SessionLog",
    "SessionState",
    "ToolDispatcher",
    "WorkflowEngine",
    "agent_tool_specs",
    "delete_global_security_rules",
    "diff_params",
    "list_global_security_rules",
    "resolve_model_key",
    "tools_for_agent",
]
