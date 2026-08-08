"""Tests for ``kodo.validator._suite`` and the ``kodo.validator.suites`` package.

Covers the pure dataclasses, the summary-round prompt/report rendering, the
``run_suite`` orchestration (with ``run_scenario``/the summary round mocked),
the summary round's own harness usage (with the harness's heavy dependencies
mocked, mirroring ``test_validator_harness.py``), and the suite selector
resolver. No real server or LLM is spawned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kodo.validator._evaluate import EvaluationResult
from kodo.validator._harness import TurnResult
from kodo.validator._scenario import Scenario, ScenarioResult
from kodo.validator._suite import (
    LLMUnderTest,
    SuiteEntry,
    SuiteEntryResult,
    SuiteResult,
    ValidationSuite,
    _render_summary_prompt,
    _run_summary_round,
    _write_suite_report,
    _write_suite_summary,
    run_suite,
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_llm_under_test_defaults_to_no_knob_pins() -> None:
    lut = LLMUnderTest(llm="some-model")
    assert lut.knobs == {}


def test_llm_under_test_explicit_knobs() -> None:
    lut = LLMUnderTest(llm="some-model", knobs={"temperature": "near-greedy"})
    assert lut.knobs == {"temperature": "near-greedy"}


def test_validation_suite_requires_judge_llm_and_summary_prompt() -> None:
    with pytest.raises(TypeError):
        ValidationSuite(name="s", entries=[])  # type: ignore[call-arg]


def test_validation_suite_judge_llm_knobs_defaults_none() -> None:
    suite = ValidationSuite(name="s", entries=[], judge_llm="judge", summary_prompt="p")
    assert suite.judge_llm_knobs is None


# ---------------------------------------------------------------------------
# Helpers to build fake results without running anything
# ---------------------------------------------------------------------------


def _scenario(name: str) -> Scenario:
    return Scenario(name=name, prompts=["do it"])


def _scenario_result(
    scenario: Scenario,
    *,
    run_dir: Path,
    score: float | None = None,
    report: str = "",
) -> ScenarioResult:
    evaluation = (
        EvaluationResult(
            score=score, report=report, raw_text="", attempts=1, judge_session_id="judge-1"
        )
        if score is not None
        else None
    )
    return ScenarioResult(
        scenario=scenario,
        run_dir=run_dir,
        llm_under_test="lut-model",
        validation_llm="judge-model",
        knobs={},
        validation_llm_knobs={},
        turns=[TurnResult(prompt="do it", final_phase="done", assistant_text="ok")],
        score=score,
        evaluation=evaluation,
    )


# ---------------------------------------------------------------------------
# _render_summary_prompt
# ---------------------------------------------------------------------------


def test_render_summary_prompt_includes_every_entry_and_its_score(tmp_path: Path) -> None:
    entries = [
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-a"),
            result=_scenario_result(
                _scenario("s1"), run_dir=tmp_path / "a", score=87.5, report="did well"
            ),
        ),
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-b", knobs={"temperature": "near-greedy"}),
            result=_scenario_result(
                _scenario("s2"), run_dir=tmp_path / "b", score=42.0, report="struggled"
            ),
        ),
    ]
    prompt = _render_summary_prompt(entries)
    assert "model-a" in prompt and "s1" in prompt and "87.5" in prompt and "did well" in prompt
    assert "model-b" in prompt and "temperature=near-greedy" in prompt
    assert "s2" in prompt and "42" in prompt and "struggled" in prompt
    assert "Do not call any tool" in prompt


def test_render_summary_prompt_notes_missing_evaluation(tmp_path: Path) -> None:
    entries = [
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-a"),
            result=_scenario_result(_scenario("s1"), run_dir=tmp_path / "a"),
        )
    ]
    prompt = _render_summary_prompt(entries)
    assert "no RVP evaluation ran" in prompt
    assert "['done']" in prompt


# ---------------------------------------------------------------------------
# _write_suite_report / _write_suite_summary
# ---------------------------------------------------------------------------


def _suite_result(tmp_path: Path, *, judge_llm_knobs: dict[str, str] | None = None) -> SuiteResult:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    suite = ValidationSuite(
        name="demo-suite",
        entries=[],
        judge_llm="judge-model",
        judge_llm_knobs=judge_llm_knobs,
        summary_prompt="summarize",
    )
    entries = [
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-a"),
            result=_scenario_result(
                _scenario("s1"), run_dir=run_dir / "s1", score=90.0, report="good"
            ),
        ),
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-b"),
            result=_scenario_result(_scenario("s2"), run_dir=run_dir / "s2"),
        ),
    ]
    return SuiteResult(
        suite=suite,
        run_dir=run_dir,
        entries=entries,
        summary="model-a beat model-b overall.",
        summary_session_id="summary-session",
    )


def test_write_suite_report_lists_every_entry_and_the_summary(tmp_path: Path) -> None:
    result = _suite_result(tmp_path)
    _write_suite_report(result)
    text = (result.run_dir / "suite-report.md").read_text(encoding="utf-8")
    assert "demo-suite" in text
    assert "model-a" in text and "90" in text
    assert "model-b" in text and "n/a" in text
    assert "model-a beat model-b overall." in text


def test_write_suite_report_shows_judge_knobs_when_set(tmp_path: Path) -> None:
    result = _suite_result(tmp_path, judge_llm_knobs={"temperature": "near-greedy"})
    _write_suite_report(result)
    text = (result.run_dir / "suite-report.md").read_text(encoding="utf-8")
    assert "temperature=near-greedy" in text


def test_write_suite_summary_json_records_every_entry(tmp_path: Path) -> None:
    result = _suite_result(tmp_path)
    _write_suite_summary(result)
    summary = json.loads((result.run_dir / "suite-summary.json").read_text(encoding="utf-8"))
    assert summary["suite"] == "demo-suite"
    assert summary["judge_llm"] == "judge-model"
    assert summary["summary_session_id"] == "summary-session"
    assert [e["scenario"] for e in summary["entries"]] == ["s1", "s2"]
    assert summary["entries"][0]["score"] == 90.0


# ---------------------------------------------------------------------------
# run_suite -- orchestration (run_scenario / summary round mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_suite_runs_every_entry_and_the_summary_round(tmp_path: Path) -> None:
    import kodo.validator._suite as _suite_module

    lut_a = LLMUnderTest(llm="model-a", knobs={"tail-culling": "light"})
    lut_b = LLMUnderTest(llm="model-b")
    scenario_a = _scenario("s1")
    scenario_b = _scenario("s2")
    suite = ValidationSuite(
        name="demo-suite",
        entries=[
            SuiteEntry(llm_under_test=lut_a, scenario=scenario_a),
            SuiteEntry(llm_under_test=lut_b, scenario=scenario_b),
        ],
        judge_llm="judge-model",
        judge_llm_knobs={"temperature": "near-greedy"},
        summary_prompt="summarize",
    )

    fake_results = {
        "s1": _scenario_result(scenario_a, run_dir=tmp_path / "s1-run", score=80.0, report="ok"),
        "s2": _scenario_result(scenario_b, run_dir=tmp_path / "s2-run", score=60.0, report="meh"),
    }

    async def fake_run_scenario(
        scenario: Scenario,
        out_dir: Path,
        *,
        llm_under_test: str,
        validation_llm: str,
        knobs: dict[str, str] | None,
        validation_llm_knobs: dict[str, str] | None,
        template_home: Path | None,
    ) -> ScenarioResult:
        return fake_results[scenario.name]

    fake_summary = AsyncMock(return_value=("the comparative summary", "summary-session"))
    fake_run_scenario_mock = AsyncMock(side_effect=fake_run_scenario)

    with (
        patch.object(_suite_module, "run_scenario", fake_run_scenario_mock),
        patch.object(_suite_module, "_run_summary_round", fake_summary),
    ):
        result = await run_suite(suite, tmp_path / "out")

    calls = fake_run_scenario_mock.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["llm_under_test"] == "model-a"
    assert calls[0].kwargs["knobs"] == {"tail-culling": "light"}
    assert calls[0].kwargs["validation_llm"] == "judge-model"
    assert calls[0].kwargs["validation_llm_knobs"] == {"temperature": "near-greedy"}
    assert calls[1].kwargs["llm_under_test"] == "model-b"
    assert calls[1].kwargs["knobs"] == {}

    fake_summary.assert_awaited_once()
    assert result.summary == "the comparative summary"
    assert result.summary_session_id == "summary-session"
    assert [e.result.scenario.name for e in result.entries] == ["s1", "s2"]
    assert (result.run_dir / "suite-report.md").is_file()
    assert (result.run_dir / "suite-summary.json").is_file()


# ---------------------------------------------------------------------------
# _run_summary_round -- the final llm.select/llm.complete round (mocked harness)
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_harness_deps(tmp_path: Path) -> dict[str, Any]:
    """Mirrors test_validator_harness.py's ``_mock_deps`` fixture."""
    deps: dict[str, Any] = {}
    deps["clone_kodo_home"] = MagicMock(return_value=tmp_path / "kodo-home")

    mock_server = MagicMock()
    mock_server.start = AsyncMock()
    mock_server.stop = AsyncMock()
    mock_server.ws_url = "ws://127.0.0.1:12345/ws"
    deps["ServerProcess"] = MagicMock(return_value=mock_server)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.hello = AsyncMock(return_value={"local_registry": []})
    mock_client.close = AsyncMock()
    mock_client.session_id = "summary-session-id"

    async def fake_request(
        msg_type: str, payload: dict[str, object] | None = None, **fields: object
    ) -> dict[str, object]:
        if msg_type == "local_llm.set_knobs":
            return {"type": "local_llm.set_knobs.done", "ok": True}
        if msg_type == "llm.select":
            return {"type": "llm.select.done", "ok": True}
        assert msg_type == "llm.complete"
        return {"type": "llm.complete.done", "ok": True, "text": "the final summary"}

    mock_client.request = AsyncMock(side_effect=fake_request)
    deps["ValidatorClient"] = MagicMock(return_value=mock_client)
    deps["ensure_local_llms_installed"] = AsyncMock()
    return deps


@pytest.mark.asyncio
async def test_run_summary_round_selects_judge_then_completes(
    tmp_path: Path, _mock_harness_deps: dict[str, Any]
) -> None:
    import kodo.validator._harness as _harness

    suite = ValidationSuite(
        name="demo-suite",
        entries=[],
        judge_llm="judge-model",
        judge_llm_knobs={"temperature": "near-greedy"},
        summary_prompt="summarize everything",
    )
    entries = [
        SuiteEntryResult(
            llm_under_test=LLMUnderTest(llm="model-a"),
            result=_scenario_result(
                _scenario("s1"), run_dir=tmp_path / "s1", score=80.0, report="ok"
            ),
        )
    ]

    with (
        patch.object(_harness, "clone_kodo_home", _mock_harness_deps["clone_kodo_home"]),
        patch.object(_harness, "ServerProcess", _mock_harness_deps["ServerProcess"]),
        patch.object(_harness, "ValidatorClient", _mock_harness_deps["ValidatorClient"]),
        patch.object(
            _harness,
            "ensure_local_llms_installed",
            _mock_harness_deps["ensure_local_llms_installed"],
        ),
    ):
        text, session_id = await _run_summary_round(suite, entries, tmp_path / "run", None)

    assert text == "the final summary"
    assert session_id == "summary-session-id"

    calls = _mock_harness_deps["ValidatorClient"].return_value.request.await_args_list
    select_calls = [c for c in calls if c.args and c.args[0] == "llm.select"]
    complete_calls = [c for c in calls if c.args and c.args[0] == "llm.complete"]
    assert len(select_calls) == 1
    assert select_calls[0].kwargs["name"] == "judge-model"
    assert len(complete_calls) == 1
    assert complete_calls[0].kwargs["system"] == "summarize everything"
    assert "s1" in complete_calls[0].kwargs["prompt"]

    knob_calls = [c for c in calls if c.args and c.args[0] == "local_llm.set_knobs"]
    assert len(knob_calls) == 1
    assert knob_calls[0].kwargs == {
        "name": "judge-model",
        "knobs": {"temperature": "near-greedy"},
    }


# ---------------------------------------------------------------------------
# Suite selector resolver
# ---------------------------------------------------------------------------

_FAKE_SUITE = (
    "from kodo.validator import ValidationSuite\n"
    "SUITE = ValidationSuite(name={name!r}, entries=[], judge_llm='judge', "
    "summary_prompt='summarize')\n"
)


def _write_fake_suites(root: Path) -> None:
    (root / "fam").mkdir(parents=True)
    (root / "fam" / "a.py").write_text(_FAKE_SUITE.format(name="a"), encoding="utf-8")
    (root / "fam" / "b.py").write_text(_FAKE_SUITE.format(name="b"), encoding="utf-8")
    (root / "top.py").write_text(_FAKE_SUITE.format(name="top"), encoding="utf-8")
    (root / "_helper.py").write_text("SUITE = None\n", encoding="utf-8")


def test_resolve_suite_selectors_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kodo.validator import suites as st

    _write_fake_suites(tmp_path)
    monkeypatch.setattr(st, "_SUITES_DIR", tmp_path)

    assert set(st.suite_ids()) == {"fam.a", "fam.b", "top"}
    assert [i for i, _ in st.resolve_selectors(["fam"])] == ["fam.a", "fam.b"]
    assert [i for i, _ in st.resolve_selectors(["fam.a"])] == ["fam.a"]
    assert len(st.resolve_selectors(["all"])) == 3
    assert [i for i, _ in st.resolve_selectors(["fam", "fam.a"])] == ["fam.a", "fam.b"]
    with pytest.raises(st.SuiteResolutionError):
        st.resolve_selectors(["nope"])


def test_full_regression_suite_covers_every_shipped_scenario_and_judge() -> None:
    from kodo.validator import scenarios as scn
    from kodo.validator import suites as st
    from kodo.validator.prompts import PROMPTS

    ((dotted_id, suite),) = st.resolve_selectors(["full_regression"])
    assert dotted_id == "full_regression"
    assert suite.judge_llm == "unsloth-qwen36-27b-q8-k-xl"
    assert suite.summary_prompt == PROMPTS.get("suite_summary/default")

    # Every shipped scenario is covered exactly once.
    assert len(suite.entries) == len(scn.scenario_ids())
    assert len({e.scenario.name for e in suite.entries}) == len(suite.entries)

    by_scenario = {e.scenario.name: e.llm_under_test for e in suite.entries}
    assert by_scenario["tictactoe-detailed-task"].llm == "unsloth-qwen35-9b-q8-k-xl"
    assert by_scenario["tictactoe-sparse-task"].llm == "unsloth-qwen36-27b-q8-k-xl"
    assert by_scenario["toolchain-python"].llm == "deepreinforce-ornith10-35b-a3b-bf16"
    laguna = by_scenario["attachment-report"]
    assert laguna.llm == "unsloth-laguna-s-2-1-mxfp4-moe"
    assert laguna.knobs == {"tail-culling": "light", "temperature": "default"}
