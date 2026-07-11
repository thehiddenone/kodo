"""Declarative validation scenarios and their runner.

A :class:`Scenario` describes one validation: the simulated workspace shape,
the mode toggles, the prompt sequence, and the simulated-user policy — plus,
since phase 2, the two validation prompts. With a ``user_proxy_prompt`` the
LUT's questions are answered by the validation LLM (doc/VALIDATOR.md §9);
with a ``result_validation_prompt`` the run ends with a judge session whose
verdict fills :attr:`ScenarioResult.score` (0 = fail … 100 = perfect) and
``<run_dir>/report.md``. Without them, phase-1 behaviour is unchanged
(scripted answers, ``score=None``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._evaluate import EvaluationResult
from ._harness import Modes, TurnResult, ValidationHarness
from ._user import UserSimulator

__all__ = ["RootSpec", "Scenario", "ScenarioResult", "run_scenario"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RootSpec:
    """One simulated workspace folder of a scenario.

    Attributes:
        name: Workspace-folder display name.
        seed_from: Optional file/directory whose content initializes the root.
    """

    name: str
    seed_from: Path | None = None


@dataclass(frozen=True)
class Scenario:
    """A complete, repeatable validation recipe.

    Attributes:
        name: Scenario identifier (used for the run directory name).
        prompts: Prompt sequence, submitted one turn at a time.
        llm_under_test: Local registry name of the model this run exercises
            — the harness pins it as the active model and downloads it first
            if missing. Mandatory: there is no meaningful default.
        validation_llm: Local registry name of the fixed, capable model
            reserved for the (not yet built) Phase 2 evaluator — ensured
            present/downloaded but not otherwise invoked in phase 1.
            Mandatory: there is no meaningful default.
        roots: Simulated workspace folders (one = single-root VS Code window,
            several = multi-root).
        modes: Session toggles pinned before the first prompt.
        project_root: Root name to bind as the Guided-mode project
            (required by the ``guided`` workflow, ignored otherwise).
        user: Simulated-user policy; the harness default when None. With a
            ``user_proxy_prompt`` this is the *base* policy — questions go to
            the validation LLM, everything else still lands here.
        settings_overrides: Per-run ``etc/settings.json`` overrides (the
            ``llm_under_test`` pin is applied on top, see
            :class:`~kodo.validator._harness.ValidationHarness`).
        turn_timeout: Per-prompt turn timeout in seconds. VLLM-proxied
            question answers (two model swaps + a completion each) count
            against it — size generously when ``user_proxy_prompt`` is set.
        user_proxy_prompt: The UPP — enables VLLM-answered questions.
        result_validation_prompt: The RVP — enables the judge session that
            scores the run after its last turn.
        eval_timeout: Per-judge-turn timeout in seconds.
    """

    name: str
    prompts: list[str]
    llm_under_test: str = field(kw_only=True)
    validation_llm: str = field(kw_only=True)
    roots: list[RootSpec] = field(default_factory=list)
    modes: Modes = field(default_factory=Modes)
    project_root: str | None = None
    user: UserSimulator | None = None
    settings_overrides: dict[str, object] | None = None
    turn_timeout: float = 900.0
    user_proxy_prompt: str | None = None
    result_validation_prompt: str | None = None
    eval_timeout: float = 900.0


@dataclass(frozen=True)
class ScenarioResult:
    """Outcome of one scenario run.

    Attributes:
        scenario: The executed scenario.
        run_dir: Artifact directory (home, workspace, transcript, and —
            when evaluated — ``report.md`` + ``judge-transcript.jsonl``).
        turns: Per-prompt results, in order.
        score: The judge's 0–100 verdict. None when the scenario carried no
            ``result_validation_prompt`` or a turn ended in ``error`` (the
            judge is skipped — an infra failure must not masquerade as a
            low-scoring run).
        evaluation: The full judge outcome, when one ran.
    """

    scenario: Scenario
    run_dir: Path
    turns: list[TurnResult]
    score: float | None = None
    evaluation: EvaluationResult | None = None


async def run_scenario(
    scenario: Scenario,
    out_dir: Path,
    *,
    template_home: Path | None = None,
) -> ScenarioResult:
    """Execute one scenario in a fresh isolated harness.

    Args:
        scenario (Scenario): The recipe to run.
        out_dir (Path): Parent directory for run artifacts; the run itself
            lands in ``out_dir/<name>-<timestamp>/``.
        template_home (Path | None): ``.kodo`` template for the isolated home.

    Returns:
        ScenarioResult: Turn results plus the artifact location.
    """
    run_dir = out_dir / f"{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    harness = ValidationHarness(
        run_dir,
        llm_under_test=scenario.llm_under_test,
        validation_llm=scenario.validation_llm,
        template_home=template_home,
        user=scenario.user,
        settings_overrides=scenario.settings_overrides,
        user_proxy_prompt=scenario.user_proxy_prompt,
        result_validation_prompt=scenario.result_validation_prompt,
    )
    for root in scenario.roots:
        harness.workspace.add_root(root.name, seed_from=root.seed_from)

    turns: list[TurnResult] = []
    evaluation: EvaluationResult | None = None
    async with harness:
        await harness.apply_modes(scenario.modes)
        if scenario.project_root is not None:
            await harness.bind_project(scenario.project_root)
        for prompt in scenario.prompts:
            _log.info("[%s] prompt: %s", scenario.name, prompt[:80])
            turn = await harness.submit_prompt(prompt, turn_timeout=scenario.turn_timeout)
            turns.append(turn)
            if turn.final_phase in ("error", "done"):
                break
        ran_clean = bool(turns) and all(t.final_phase != "error" for t in turns)
        if scenario.result_validation_prompt is not None and ran_clean:
            evaluation = await harness.evaluate(turn_timeout=scenario.eval_timeout)

    result = ScenarioResult(
        scenario=scenario,
        run_dir=run_dir,
        turns=turns,
        score=evaluation.score if evaluation is not None else None,
        evaluation=evaluation,
    )
    _write_summary(result)
    if evaluation is not None:
        _write_report(result, evaluation)
    return result


def _write_summary(result: ScenarioResult) -> None:
    """Persist a machine-readable run summary next to the transcript.

    Args:
        result (ScenarioResult): The finished run.
    """
    summary: dict[str, object] = {
        "scenario": result.scenario.name,
        "score": result.score,
        "evaluation": (
            {
                "attempts": result.evaluation.attempts,
                "judge_session_id": result.evaluation.judge_session_id,
                "report_file": "report.md",
            }
            if result.evaluation is not None
            else None
        ),
        "turns": [
            {
                "prompt": t.prompt,
                "final_phase": t.final_phase,
                "assistant_chars": len(t.assistant_text),
                "tool_calls": [c.get("tool_name") for c in t.tool_calls],
                "interactions": [i.payload.get("interaction") for i in t.interactions],
                "errors": t.errors,
            }
            for t in result.turns
        ],
    }
    (result.run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_report(result: ScenarioResult, evaluation: EvaluationResult) -> None:
    """Persist the judge's verdict as ``<run_dir>/report.md``.

    Args:
        result (ScenarioResult): The finished run.
        evaluation (EvaluationResult): The judge outcome to write.
    """
    lines = [
        f"# Validation report — {result.scenario.name}",
        "",
        f"- **Score:** {evaluation.score:g} / 100",
        f"- **LLM under test:** {result.scenario.llm_under_test}",
        f"- **Validation LLM:** {result.scenario.validation_llm}",
        f"- **Judge attempts:** {evaluation.attempts}",
        f"- **Judge session:** {evaluation.judge_session_id or 'n/a'}",
        "",
        "## Judge report",
        "",
        evaluation.report or "(the judge returned an empty report)",
        "",
    ]
    (result.run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
