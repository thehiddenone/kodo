"""Automated validation harness for kodo's agentic workflows.

Runs real kodo sessions with no VS Code and no human: it starts the actual
server subprocess (``python -m kodo.server``) against an **isolated clone**
of a kodo home, connects over the real WebSocket protocol as a
pseudo-extension, simulates a single- or multi-root workspace, submits
prompts, answers every interactive gate (``ask_user``, document approvals,
security permissions, API keys), and records the whole exchange to a
transcript.

Phase 2 adds the validation LLM to the loop: with a **User Proxy Prompt**
(``Scenario.user_proxy_prompt``) the LUT's questions are answered by the
validation LLM via the synchronous ``llm.select``/``llm.complete`` protocol
commands; with a **Result Validation Prompt** (``result_validation_prompt``)
a judge session scores the finished run 0–100 into
:attr:`ScenarioResult.score` and ``<run_dir>/report.md``. See
doc/VALIDATOR.md §9.

Entry point: ``python -m kodo.validator`` (see ``__main__.py``). Programmatic
use starts at :class:`ValidationHarness` or :func:`run_scenario`.
"""

from ._client import ProtocolError, ValidatorClient
from ._evaluate import EvaluationError, EvaluationResult, run_evaluation
from ._harness import Modes, TurnResult, ValidationHarness
from ._home import DEFAULT_SKIP_ENTRIES, DEFAULT_SYMLINK_ENTRIES, clone_kodo_home
from ._models import LocalModelUnavailableError, ensure_local_llms_installed
from ._scenario import RootSpec, Scenario, ScenarioResult, run_scenario
from ._server import ServerProcess, ServerStartError
from ._transcript import Transcript, TranscriptEntry
from ._user import QuestionAnswer, ScriptedUser, UserSimulator
from ._vllm import VLLMProxyError, VLLMUserProxy, answers_json_schema
from ._workspace import SimulatedWorkspace, WorkspaceRoot

__all__ = [
    "DEFAULT_SKIP_ENTRIES",
    "DEFAULT_SYMLINK_ENTRIES",
    "EvaluationError",
    "EvaluationResult",
    "LocalModelUnavailableError",
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
    "VLLMProxyError",
    "VLLMUserProxy",
    "ValidationHarness",
    "ValidatorClient",
    "WorkspaceRoot",
    "answers_json_schema",
    "clone_kodo_home",
    "ensure_local_llms_installed",
    "run_evaluation",
    "run_scenario",
]
