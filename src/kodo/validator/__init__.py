"""Automated validation harness for kodo's agentic workflows.

Runs real kodo sessions with no VS Code and no human: it starts the actual
server subprocess (``python -m kodo.server``) against an **isolated clone**
of a kodo home, connects over the real WebSocket protocol as a
pseudo-extension, simulates a single- or multi-root workspace, submits
prompts, answers every interactive gate (``ask_user``, document approvals,
security permissions, API keys) from a scripted policy, and records the whole
exchange to a transcript for later evaluation/scoring (phase 2).

Entry point: ``python -m kodo.validator`` (see ``__main__.py``). Programmatic
use starts at :class:`ValidationHarness` or :func:`run_scenario`.
"""

from ._client import ProtocolError, ValidatorClient
from ._harness import Modes, TurnResult, ValidationHarness
from ._home import DEFAULT_SKIP_ENTRIES, DEFAULT_SYMLINK_ENTRIES, clone_kodo_home
from ._scenario import RootSpec, Scenario, ScenarioResult, run_scenario
from ._server import ServerProcess, ServerStartError
from ._transcript import Transcript, TranscriptEntry
from ._user import QuestionAnswer, ScriptedUser, UserSimulator
from ._workspace import SimulatedWorkspace, WorkspaceRoot

__all__ = [
    "DEFAULT_SKIP_ENTRIES",
    "DEFAULT_SYMLINK_ENTRIES",
    "Modes",
    "ProtocolError",
    "QuestionAnswer",
    "RootSpec",
    "Scenario",
    "ScenarioResult",
    "ScriptedUser",
    "ServerProcess",
    "ServerStartError",
    "SimulatedWorkspace",
    "Transcript",
    "TranscriptEntry",
    "TurnResult",
    "UserSimulator",
    "ValidationHarness",
    "ValidatorClient",
    "WorkspaceRoot",
    "clone_kodo_home",
    "run_scenario",
]
