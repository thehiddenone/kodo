"""Scenario: CLI Tic-Tac-Toe vs. computer, with tests + toolchain, in C++.

Selector: ``toolchain_cpp`` (or ``all``).

Part of the ``tictactoe_toolchain`` family (see
``prompts/tictactoe_toolchain/``): a fully-specified CLI Tic-Tac-Toe-vs-computer
task that explicitly asks for tests and a build toolchain, so Problem Solver's
toolchain triggers fire and it spawns ``toolchain_builder`` for C++. The
computer opponent's strength/algorithm is deliberately left open (see the RVP)
— only its legality is graded. This file is content-only (see
:mod:`kodo.validator._scenario`) — the LLM under test and judge are pinned by
whatever runs it (a suite, or ``--llm-under-test``/``--validation-llm``). A
judge with ``toolchain_build`` access can actually run the generated toolchain
rather than only reading it — see ``agent_judge.md``.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="toolchain-cpp",
    prompts=[PROMPTS.get("tictactoe_toolchain/task").format(language="C++")],
    user_proxy_prompt=PROMPTS.get("tictactoe_toolchain/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe_toolchain/rvp"),
    roots=[RootSpec(name="tictactoe-cpp")],
    # Interactive + problem-solving, friction-free gates: the task is fully
    # specified (no ask_user expected), and the explicit "write tests" +
    # "set up the toolchain" asks are what trigger Problem Solver to spawn
    # toolchain_builder for C++.
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=3600.0,
    eval_timeout=3000.0,
)
