"""Scenario: console Tic-Tac-Toe from stdin, from a fully-specified task.

Selector: ``tictactoe_detailed_task`` (or ``all``).

The prompt under test is the **fully specified** ``tictactoe/detailed_task``,
written so a weak LLM under test builds directly instead of stalling: it
verbalizes clarifying questions as prose rather than calling ``ask_user``, so
a task with open questions starves the build (see the
``tictactoe_sparse_task`` scenario for the ask/answer path on the
``tictactoe/sparse_task`` variant). The shared ``tictactoe/upp`` and
``tictactoe/rvp`` are reused across both — only the ``_task`` differs. The
judge scores the result via the ``submit_evaluation`` tool per the RVP.

This file is content-only (see :mod:`kodo.validator._scenario`) — the LLM
under test and judge are pinned by whatever runs it (a suite, or
``--llm-under-test``/``--validation-llm``).
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="tictactoe-detailed-task",
    prompts=[PROMPTS.get("tictactoe/detailed_task")],
    user_proxy_prompt=PROMPTS.get("tictactoe/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe/rvp"),
    roots=[RootSpec(name="tictactoe")],
    # Interactive + problem-solving, friction-free gates. The PUT is fully
    # specified (a weak LLM under test won't reliably call ask_user), so the
    # UPP stays wired but is normally inert here — a capable LUT that asks
    # would exercise it. The judge scores via the submit_evaluation tool.
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=2400.0,
    eval_timeout=1800.0,
)
