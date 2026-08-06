"""Validation suites: several LLMs (+ flavors) under test, run and compared.

A :class:`ValidationSuite` is the decoupled replacement for pinning
``llm_under_test``/``validation_llm``/``flavor`` directly on a
:class:`~kodo.validator._scenario.Scenario` (the scenario is content-only,
see its module docstring): a suite is the thing that says which LLM(s) —
each an :class:`LLMUnderTest`, a registry name plus a flavor id — exercise
which scenarios, and which model judges all of them.

Execution is a flat, **explicit** list of :class:`SuiteEntry` pairs
(``LLMUnderTest`` + ``Scenario``) — not an implicit cross product — because in
practice different scenarios target different LUTs on purpose (a
fully-specified task for a weak LUT, a deliberately vague one to exercise
``ask_user`` on a strong one, a toolchain-triggering task for a mid-size one,
…). See the shipped ``kodo.validator.suites`` files.

Each entry runs through :func:`~kodo.validator._scenario.run_scenario`
exactly as a standalone scenario would — its own fresh isolated
harness/home/server/workspace — so a suite is "the same isolated runs, just
batched and reported together". Only after *every* entry has finished does
:func:`run_suite` open one more, final round: a single session-less
``llm.complete`` call on the judge model, given every entry's already-written
report, producing one comparative summary across every LLM the suite
validated (``<run_dir>/suite-report.md``). See doc/VALIDATOR.md §10.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from kodo.transport import MSG_LLM_COMPLETE, MSG_LLM_SELECT

from ._harness import ValidationHarness
from ._scenario import Scenario, ScenarioResult, run_scenario

__all__ = [
    "DEFAULT_SUMMARY_SWITCH_TIMEOUT",
    "DEFAULT_SUMMARY_TIMEOUT",
    "LLMUnderTest",
    "SuiteEntry",
    "SuiteEntryResult",
    "SuiteResult",
    "ValidationSuite",
    "run_suite",
]

_log = logging.getLogger(__name__)

# llm.complete response timeout for the final summary round, and the
# llm.select confirming judge_llm is actually serving before it.
DEFAULT_SUMMARY_TIMEOUT = 900.0
DEFAULT_SUMMARY_SWITCH_TIMEOUT = 600.0


@dataclass(frozen=True)
class LLMUnderTest:
    """One LLM-under-test identity: a local registry name plus its flavor.

    Attributes:
        llm: Local registry name (``kodo/doc/LLM_REGISTRY.md``).
        flavor: Flavor id to make active before the first prompt
            (``local_llm.set_active_flavor``). Defaults to ``"default"`` —
            every registry entry ships an ``id == "default"`` flavor, so a
            suite's runs are deterministic unless a preset is named
            explicitly (doc/VALIDATOR.md §8a.1).
    """

    llm: str
    flavor: str = "default"


@dataclass(frozen=True)
class SuiteEntry:
    """One (LLM-under-test, scenario) pair to validate.

    Attributes:
        llm_under_test: The LLM + flavor this entry exercises.
        scenario: The content-only scenario to run against it.
    """

    llm_under_test: LLMUnderTest
    scenario: Scenario


@dataclass(frozen=True)
class ValidationSuite:
    """A batch of (LLM-under-test, scenario) validations, judged by one model.

    Attributes:
        name: Suite identifier (used for the run directory name).
        entries: Explicit (LLM-under-test, scenario) pairs to run — not a
            cross product. A suite author lists exactly which scenarios
            exercise which LUT; the common "these N scenarios against this
            one LUT" case is just a list comprehension in the suite file
            (``[SuiteEntry(lut, s) for s in scenarios]``).
        judge_llm: Local registry name of the model that answers every
            entry's UPP, judges every entry's RVP, and produces the final
            cross-entry summary. Mandatory: there is no meaningful default.
        judge_llm_flavor: Flavor id to make active for ``judge_llm``, the
            same way as :attr:`LLMUnderTest.flavor`. None (the default)
            leaves whatever the registry already resolves to for it.
        summary_prompt: Instructions (system prompt) for the final round —
            given every entry's score/report, produce a detailed comparative
            summary of every LLM the suite validated. Mandatory: comparing
            LLMs is a suite's whole purpose, so this round always runs.
        summary_timeout: ``llm.complete`` response timeout for the final
            round, in seconds.
        summary_thinking_level: When set, a valid tier slug for
            ``judge_llm``'s thinking family, sent as the final round's
            ``llm.complete`` ``thinking_level`` field.
    """

    name: str
    entries: list[SuiteEntry]
    judge_llm: str = field(kw_only=True)
    judge_llm_flavor: str | None = None
    summary_prompt: str = field(kw_only=True)
    summary_timeout: float = DEFAULT_SUMMARY_TIMEOUT
    summary_thinking_level: str | None = None


@dataclass(frozen=True)
class SuiteEntryResult:
    """One entry's outcome within a suite run.

    Attributes:
        llm_under_test: The LLM + flavor that was exercised.
        result: The standalone :func:`~kodo.validator._scenario.run_scenario`
            outcome for this entry (its own isolated ``run_dir``).
    """

    llm_under_test: LLMUnderTest
    result: ScenarioResult


@dataclass(frozen=True)
class SuiteResult:
    """Outcome of one suite run.

    Attributes:
        suite: The executed suite.
        run_dir: Artifact directory — one subdirectory per entry (each a
            standalone scenario run, see :attr:`SuiteEntryResult.result`),
            plus the final round's own harness under ``summary/`` and the
            suite-level ``suite-report.md``/``suite-summary.json``.
        entries: Per-entry results, in order.
        summary: The judge's free-form comparative summary text.
        summary_session_id: The final round's session id.
    """

    suite: ValidationSuite
    run_dir: Path
    entries: list[SuiteEntryResult]
    summary: str
    summary_session_id: str | None


async def run_suite(
    suite: ValidationSuite,
    out_dir: Path,
    *,
    template_home: Path | None = None,
) -> SuiteResult:
    """Execute every entry of a suite, then the final cross-entry summary round.

    Args:
        suite (ValidationSuite): The suite to run.
        out_dir (Path): Parent directory for run artifacts; the run itself
            lands in ``out_dir/<name>-<timestamp>/``.
        template_home (Path | None): ``.kodo`` template for every isolated home.

    Returns:
        SuiteResult: Every entry's outcome plus the final summary.
    """
    run_dir = out_dir / f"{suite.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    entries: list[SuiteEntryResult] = []
    for entry in suite.entries:
        _log.info(
            "[%s] entry: %s @ %s (flavor=%s)",
            suite.name,
            entry.scenario.name,
            entry.llm_under_test.llm,
            entry.llm_under_test.flavor,
        )
        scenario_result = await run_scenario(
            entry.scenario,
            run_dir,
            llm_under_test=entry.llm_under_test.llm,
            validation_llm=suite.judge_llm,
            flavor=entry.llm_under_test.flavor,
            validation_llm_flavor=suite.judge_llm_flavor,
            template_home=template_home,
        )
        entries.append(
            SuiteEntryResult(llm_under_test=entry.llm_under_test, result=scenario_result)
        )

    summary, summary_session_id = await _run_summary_round(suite, entries, run_dir, template_home)

    suite_result = SuiteResult(
        suite=suite,
        run_dir=run_dir,
        entries=entries,
        summary=summary,
        summary_session_id=summary_session_id,
    )
    _write_suite_summary(suite_result)
    _write_suite_report(suite_result)
    return suite_result


async def _run_summary_round(
    suite: ValidationSuite,
    entries: list[SuiteEntryResult],
    run_dir: Path,
    template_home: Path | None,
) -> tuple[str, str | None]:
    """Ask the judge model for one comparative summary of every entry.

    A dedicated, lightweight harness — no workspace, no session prompts, just
    enough to get ``judge_llm`` serving and issue one session-less
    ``llm.complete`` call (the same primitive the UPP proxy uses, doc/
    VALIDATOR.md §9.1). The input is every entry's already-generated report
    text, which is compact and needs no tool-based exploration, unlike the
    RVP judge's read of the generated code itself — so a real agentic session
    (workspace, tools, the ``judge`` workflow) would be unnecessary machinery
    here.

    Args:
        suite (ValidationSuite): The suite whose entries were just run.
        entries (list[SuiteEntryResult]): Every entry's outcome, in order.
        run_dir (Path): The suite run's artifact directory.
        template_home (Path | None): ``.kodo`` template for the isolated home.

    Returns:
        tuple[str, str | None]: The summary text and the round's session id.
    """
    harness = ValidationHarness(
        run_dir / "summary",
        llm_under_test=suite.judge_llm,
        validation_llm=suite.judge_llm,
        template_home=template_home,
        flavor=suite.judge_llm_flavor,
    )
    async with harness:
        client = harness.client
        await client.request(
            MSG_LLM_SELECT,
            name=suite.judge_llm,
            session_scoped=False,
            timeout=DEFAULT_SUMMARY_SWITCH_TIMEOUT,
        )
        prompt = _render_summary_prompt(entries)
        response = await client.request(
            MSG_LLM_COMPLETE,
            prompt=prompt,
            system=suite.summary_prompt,
            session_scoped=False,
            timeout=suite.summary_timeout,
            thinking_level=suite.summary_thinking_level,
        )
        text = str(response.get("text", ""))
        return text, harness.session_id


def _render_summary_prompt(entries: list[SuiteEntryResult]) -> str:
    """Assemble the summary round's user message: every entry's outcome.

    Args:
        entries (list[SuiteEntryResult]): Every entry's outcome, in order.

    Returns:
        str: The composed prompt text (``ValidationSuite.summary_prompt``
        rides as the ``system`` message).
    """
    blocks: list[str] = []
    for entry in entries:
        result = entry.result
        header = (
            f"### {entry.llm_under_test.llm} (flavor: {entry.llm_under_test.flavor}) "
            f"— scenario: {result.scenario.name}"
        )
        if result.evaluation is not None:
            body = f"Score: {result.evaluation.score:g} / 100\n\n{result.evaluation.report}"
        else:
            final_phases = [t.final_phase for t in result.turns]
            body = f"(no RVP evaluation ran for this entry — turn phases: {final_phases})"
        blocks.append(f"{header}\n\n{body}")
    return (
        "## Per-entry validation reports\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n## Response format\n\nReply with your full written summary in markdown "
        "prose. Do not call any tool."
    )


def _write_suite_summary(result: SuiteResult) -> None:
    """Persist a machine-readable suite summary under ``<run_dir>/suite-summary.json``.

    Args:
        result (SuiteResult): The finished suite run.
    """
    summary: dict[str, object] = {
        "suite": result.suite.name,
        "judge_llm": result.suite.judge_llm,
        "judge_llm_flavor": result.suite.judge_llm_flavor,
        "summary_session_id": result.summary_session_id,
        "entries": [
            {
                "llm": e.llm_under_test.llm,
                "flavor": e.llm_under_test.flavor,
                "scenario": e.result.scenario.name,
                "score": e.result.score,
                "run_dir": str(e.result.run_dir),
            }
            for e in result.entries
        ],
    }
    (result.run_dir / "suite-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_suite_report(result: SuiteResult) -> None:
    """Persist the human-readable ``<run_dir>/suite-report.md``.

    Args:
        result (SuiteResult): The finished suite run.
    """
    lines = [
        f"# Validation suite report — {result.suite.name}",
        "",
        f"- **Judge LLM:** {result.suite.judge_llm}"
        + (
            f" (flavor: {result.suite.judge_llm_flavor})"
            if result.suite.judge_llm_flavor
            else ""
        ),
        f"- **Entries:** {len(result.entries)}",
        "",
        "## Per-entry scores",
        "",
    ]
    for e in result.entries:
        score = f"{e.result.score:g}" if e.result.score is not None else "n/a"
        lines.append(
            f"- `{e.llm_under_test.llm}` (flavor: `{e.llm_under_test.flavor}`) — "
            f"{e.result.scenario.name}: **{score}** / 100 ({e.result.run_dir.name})"
        )
    lines += [
        "",
        "## Judge summary",
        "",
        result.summary or "(the judge returned an empty summary)",
        "",
    ]
    (result.run_dir / "suite-report.md").write_text("\n".join(lines), encoding="utf-8")
