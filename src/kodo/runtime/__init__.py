"""Kodo runtime — Guide substrate, index, bootstrap, gates, and tools.

Tool dispatch lives in :mod:`kodo.tools` (a lower import tier); the engine
builds a per-run :class:`~kodo.tools.ToolDispatcher` and resolves each agent's
tools via :func:`~kodo.tools.tools_for_agent`. Both are re-exported here for
convenience.
"""

from kodo.tools import ToolDispatcher, tools_for_agent

from ._bootstrap import BootstrapResult, ProjectBootstrap, locate_guide_session
from ._checkpoints import CheckpointEntry, CheckpointState, MirrorDirtyError
from ._engine import WorkflowEngine
from ._gates import ApprovalResponse, GateOrchestrator, QuestionResponse
from ._guide import GuideMarker
from ._rollback import Rollback
from ._session import SessionState
from ._session_log import SessionLog

__all__ = [
    "ApprovalResponse",
    "BootstrapResult",
    "CheckpointEntry",
    "CheckpointState",
    "GateOrchestrator",
    "GuideMarker",
    "MirrorDirtyError",
    "ProjectBootstrap",
    "QuestionResponse",
    "Rollback",
    "SessionLog",
    "SessionState",
    "ToolDispatcher",
    "WorkflowEngine",
    "locate_guide_session",
    "tools_for_agent",
]
