"""Scenario: exercise the User Proxy — underspecified task forces ``ask_user``.

Selector: ``tictactoe_sparse_task`` (or ``all``).

Companion to ``tictactoe_detailed_task``. The prompt under test is the
**deliberately underspecified** ``tictactoe/sparse_task``, which tells the
assistant to call ``ask_user`` before coding; to make that reliable this
scenario is only meaningful when run with the **same capable model as both
the LLM under test and the validation LLM**, which actually uses the
structured ``ask_user`` tool. With LUT == VLLM the ``llm.select`` swaps
around each answer are same-model no-ops, so the proxy round-trip is just the
grammar-constrained ``llm.complete``. The shared ``tictactoe/upp`` and
``tictactoe/rvp`` are the same files the detailed variant uses — only the
``_task`` differs. Flow demonstrated: the LLM under test asks via
``ask_user`` → proxy answers per the UPP → it builds → judge scores via the
``submit_evaluation`` tool per the RVP.

This file is content-only (see :mod:`kodo.validator._scenario`) — the LLM
under test and judge are pinned by whatever runs it (a suite, or
``--llm-under-test``/``--validation-llm``); per the above, this scenario only
demonstrates its ask/answer path when both are pinned to the same capable
model.
"""

from __future__ import annotations

from kodo.validator import Modes, RootSpec, Scenario
from kodo.validator.prompts import PROMPTS

SCENARIO = Scenario(
    name="tictactoe-sparse-task",
    prompts=[PROMPTS.get("tictactoe/sparse_task")],
    user_proxy_prompt=PROMPTS.get("tictactoe/upp"),
    result_validation_prompt=PROMPTS.get("tictactoe/rvp"),
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
