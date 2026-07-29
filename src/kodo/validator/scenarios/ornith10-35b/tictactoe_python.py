"""Scenario: CLI Tic-Tac-Toe vs. computer, with tests + toolchain, in Python.

Selector: ``ornith10-35b.tictactoe_python`` (or ``ornith10-35b`` / ``all``).

Part of the ``tictactoe_toolchain`` family (see
``prompts/tictactoe_toolchain/``): a fully-specified CLI Tic-Tac-Toe-vs-computer
task that explicitly asks for tests and a build toolchain, so Problem Solver's
toolchain triggers fire and it spawns ``toolchain_builder`` for Python. The
computer opponent's strength/algorithm is deliberately left open (see the RVP)
— only its legality is graded. LUT = DeepReinforce Ornith10-35B, VLLM =
Unsloth Qwen3.6-27B (also the judge, which additionally gets ``toolchain_build``
to actually run the generated toolchain rather than only reading it — see
``agent_judge.md``).
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="tictactoe-toolchain-python",
    prompts=[PROMPTS.get("tictactoe_toolchain/task").format(language="Python")],
    user_proxy_prompt=PROMPTS.get("tictactoe_toolchain/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe_toolchain/rvp"),
    llm_under_test="deepreinforce-ornith10-35b-bf16",
    validation_llm="unsloth-qwen36-27b-q8-k-xl",
    roots=[RootSpec(name="tictactoe-python")],
    # Interactive + problem-solving, friction-free gates: the task is fully
    # specified (no ask_user expected), and the explicit "write tests" +
    # "set up the toolchain" asks are what trigger Problem Solver to spawn
    # toolchain_builder for Python.
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=3600.0,
    eval_timeout=3000.0,
)
