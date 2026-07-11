"""Scenario: console Tic-Tac-Toe from stdin, built by the 9B LUT.

Selector: ``qwen35-9b.tictactoe_console`` (or ``qwen35-9b`` / ``all``).

The prompt under test is the **fully specified** ``tictactoe/detailed_task`` so
the weak 9B LUT builds directly instead of stalling: it verbalizes clarifying
questions as prose rather than calling ``ask_user``, so a task with open
questions starves the build (see the ``qwen36-27b.tictactoe_upp`` scenario for
the ask/answer path on the ``tictactoe/sparse_task`` variant). The shared
``tictactoe/upp`` and ``tictactoe/rvp`` are reused across both — only the
``_task`` differs. The judge scores the result via the ``submit_evaluation``
tool per the RVP. LUT = 9B, VLLM = 27B.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="tictactoe-console",
    prompts=[PROMPTS.get("tictactoe/detailed_task")],
    user_proxy_prompt=PROMPTS.get("tictactoe/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe/rvp"),
    llm_under_test="unsloth-qwen35-9b-q8-k-xl",
    validation_llm="unsloth-qwen36-27b-q8-k-xl",
    roots=[RootSpec(name="tictactoe")],
    # Interactive + problem-solving, friction-free gates. The PUT is fully
    # specified (the 9B LUT won't reliably call ask_user), so the UPP stays
    # wired but is normally inert here — a capable LUT that asks would exercise
    # it. The judge scores via the submit_evaluation tool.
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=2400.0,
    eval_timeout=1800.0,
)
