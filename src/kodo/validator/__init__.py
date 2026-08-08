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

A :class:`Scenario` is content-only — it names no LLM. Which LLM(s) (each an
:class:`LLMUnderTest`, a registry name + knob selection) it runs against is supplied
by the caller of :func:`run_scenario`, or by a :class:`ValidationSuite`
(:func:`run_suite`) for batches that validate several LLMs and end with one
judge-produced comparative summary across all of them. See doc/VALIDATOR.md
§10.

Entry point: ``python -m kodo.validator`` (single scenario, see
``__main__.py``) or ``python -m kodo.validator.suites`` (a suite). Programmatic
use starts at :class:`ValidationHarness`, :func:`run_scenario`, or
:func:`run_suite`.
"""

from ._client import ProtocolError, ValidatorClient
from ._evaluate import EvaluationError, EvaluationResult, run_evaluation
from ._harness import Modes, TurnResult, ValidationHarness
from ._home import DEFAULT_SKIP_ENTRIES, DEFAULT_SYMLINK_ENTRIES, clone_kodo_home
from ._models import (
    LocalModelUnavailableError,
    ensure_local_llms_installed,
    missing_local_llms,
)
from ._scenario import RootSpec, Scenario, ScenarioResult, run_scenario
from ._server import ServerProcess, ServerStartError
from ._suite import (
    LLMUnderTest,
    SuiteEntry,
    SuiteEntryResult,
    SuiteResult,
    ValidationSuite,
    run_suite,
)
from ._transcript import Transcript, TranscriptEntry
from ._user import QuestionAnswer, ScriptedUser, UserSimulator
from ._vllm import VLLMProxyError, VLLMUserProxy, answers_json_schema
from ._workspace import SimulatedWorkspace, WorkspaceRoot

__all__ = [
    "DEFAULT_SKIP_ENTRIES",
    "DEFAULT_SYMLINK_ENTRIES",
    "EvaluationError",
    "EvaluationResult",
    "LLMUnderTest",
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
    "SuiteEntry",
    "SuiteEntryResult",
    "SuiteResult",
    "Transcript",
    "TranscriptEntry",
    "TurnResult",
    "UserSimulator",
    "VLLMProxyError",
    "VLLMUserProxy",
    "ValidationHarness",
    "ValidationSuite",
    "ValidatorClient",
    "WorkspaceRoot",
    "answers_json_schema",
    "clone_kodo_home",
    "ensure_local_llms_installed",
    "missing_local_llms",
    "run_evaluation",
    "run_scenario",
    "run_suite",
]
