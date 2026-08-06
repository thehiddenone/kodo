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
        files: Small inline text fixtures written into the root after
            *seed_from* is applied, as ``{path relative to the root: content}``.
            For the common case of a scenario needing one or two small input
            files (a CSV of test data, a config) this keeps the fixture in the
            scenario file next to the expected results the RVP asserts, which
            is where a reviewer needs to see it — no fixture tree to ship and
            keep in sync. Use *seed_from* instead for anything larger than a
            handful of lines.
    """

    name: str
    seed_from: Path | None = None
    files: dict[str, str] = field(default_factory=dict)


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
            several = multi-root; for a ``guided`` workflow scenario these are
            the bound project roots too — Guided mode has no separate binding
            step, exactly like ``problem_solving``).
        modes: Session toggles pinned before the first prompt.
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
        user_proxy_thinking_level: When set, a valid tier slug for
            ``validation_llm``'s thinking family (e.g. ``"minimal"``),
            sent as ``llm.complete``'s ``thinking_level`` field on every UPP
            answering call — keeps ``ask_user`` answers from burning time
            thinking. Ignored unless ``user_proxy_prompt`` is also set.
        result_validation_thinking_level: When set, a valid tier slug for
            ``validation_llm``'s thinking family, sent as the RVP judge
            session's own ``hello``'s ``thinking_level`` field as it opens —
            pins the judge's whole session to this tier. Ignored unless
            ``result_validation_prompt`` is also set.
        flavor: When set, the ``llm_under_test`` flavor id to make active
            before the first prompt (``local_llm.set_active_flavor``, see
            :meth:`~kodo.validator._harness.ValidationHarness.start`). This is
            how a scenario pins **sampling parameters**: a flavor's
            ``llama_args`` are ``llama-server``'s launch config, so the run
            exercises exactly what a user gets by picking that flavor from the
            sidebar dropdown — the CLI-level layer of doc/SAMPLING.md §9, not
            the request-level session overrides. ``None`` leaves whatever the
            registry already resolves to (normally the entry's ``default``).
        attachments: Files to attach to specific prompts, as
            ``{index into ``prompts``: [source paths]}``. Each source is
            staged into ``<run_dir>/attachments/`` — deliberately outside the
            workspace, see
            :meth:`~kodo.validator._harness.ValidationHarness.stage_attachment`
            — and sent with that prompt via the same
            ``<!--KODO_ATTACHMENTS:[…]-->`` marker line the VS Code extension
            uses, so no protocol change is involved. A dict rather than a
            per-prompt field so that ``prompts`` stays a plain ``list[str]``
            and every existing scenario is untouched.
    """

    name: str
    prompts: list[str]
    llm_under_test: str = field(kw_only=True)
    validation_llm: str = field(kw_only=True)
    roots: list[RootSpec] = field(default_factory=list)
    modes: Modes = field(default_factory=Modes)
    user: UserSimulator | None = None
    settings_overrides: dict[str, object] | None = None
    turn_timeout: float = 900.0
    user_proxy_prompt: str | None = None
    result_validation_prompt: str | None = None
    eval_timeout: float = 900.0
    user_proxy_thinking_level: str | None = None
    result_validation_thinking_level: str | None = None
    flavor: str | None = None
    attachments: dict[int, list[Path]] = field(default_factory=dict)


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
        user_proxy_thinking_level=scenario.user_proxy_thinking_level,
        result_validation_thinking_level=scenario.result_validation_thinking_level,
        flavor=scenario.flavor,
    )
    for root in scenario.roots:
        harness.workspace.add_root(root.name, seed_from=root.seed_from)
        for rel_path, content in root.files.items():
            harness.workspace.write_file(root.name, rel_path, content)
    # Staged before the run starts so a missing/unreadable attachment fails
    # immediately, rather than after a model download and a first turn.
    staged: dict[int, list[Path]] = {
        index: [harness.stage_attachment(src) for src in sources]
        for index, sources in scenario.attachments.items()
    }

    turns: list[TurnResult] = []
    evaluation: EvaluationResult | None = None
    async with harness:
        await harness.apply_modes(scenario.modes)
        for index, prompt in enumerate(scenario.prompts):
            _log.info("[%s] prompt: %s", scenario.name, prompt[:80])
            turn = await harness.submit_prompt(
                prompt,
                attachments=staged.get(index),
                turn_timeout=scenario.turn_timeout,
            )
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
