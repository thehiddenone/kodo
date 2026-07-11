"""Scenario: exercise the User Proxy — underspecified task forces ``ask_user``.

Selector: ``qwen36-27b.tictactoe_upp`` (or ``qwen36-27b`` / ``all``).

Companion to ``qwen35-9b.tictactoe_console``. The prompt under test is the
**deliberately underspecified** ``tictactoe/sparse_task``, which tells the
assistant to call ``ask_user`` before coding; to make that reliable the LUT is
the **same capable 27B model as the VLLM**, which actually uses the structured
``ask_user`` tool. With LUT == VLLM the ``llm.select`` swaps around each answer
are same-model no-ops, so the proxy round-trip is just the grammar-constrained
``llm.complete``. The shared ``tictactoe/upp`` and ``tictactoe/rvp`` are the
same files the detailed variant uses — only the ``_task`` differs. Flow
demonstrated: LUT asks via ``ask_user`` → proxy answers per the UPP → LUT builds
→ judge scores via the ``submit_evaluation`` tool per the RVP.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="tictactoe-upp",
    prompts=[PROMPTS.get("tictactoe/sparse_task")],
    user_proxy_prompt=PROMPTS.get("tictactoe/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe/rvp"),
    # LUT == VLLM (the capable 27B): ask_user is reliably used and the proxy's
    # model swaps are no-ops.
    llm_under_test="unsloth-qwen36-27b-q8-k-xl",
    validation_llm="unsloth-qwen36-27b-q8-k-xl",
    roots=[RootSpec(name="tictactoe")],
    modes=Modes(
        autonomous=False,
        workflow="problem_solving",
        edit_control="allow_all",
        command_control="permissive",
    ),
    turn_timeout=2400.0,
    eval_timeout=1800.0,
)
