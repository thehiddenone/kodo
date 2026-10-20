"""Behavioral tests for :class:`kodo.harbor.TrialActivity`.

The job directory is laid out as Harbor 0.23's ``docker`` environment leaves
it: ``<trial>/agent/kodo.jsonl`` while the agent runs, moved to
``<trial>/steps/<step>/agent/`` after each step of a multi-step task, and
``<trial>/result.json`` once the trial ended. ``kodo.harbor.agent`` writes
each step's ``instruction.md`` beside its log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kodo.harbor import TrialActivity


def _line(kind: str, ts: str = "2026-10-18T10:00:00.000+00:00", **fields: object) -> str:
    return json.dumps({"ts": ts, "type": kind, **fields}) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)


def _types(chunk: dict[str, object]) -> list[str]:
    events = chunk["events"]
    assert isinstance(events, list)
    return [str(event["type"]) for event in events]


def test_the_first_read_returns_each_log_in_full_without_the_result_line(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    _write(log, _line("run.start", ts="2026-10-18T10:00:01.000+00:00", home="/tmp/h1"))
    _write(log, _line("text", text="done") + "KODO-RESULT outcome=completed error=\n")

    (chunk,) = TrialActivity(tmp_path).read()

    assert chunk["trial"] == "task__abc"
    assert chunk["step"] is None
    assert chunk["first"] == 0
    assert chunk["started"] == "2026-10-18T10:00:01.000+00:00"
    assert _types(chunk) == ["run.start", "text"]


def test_later_reads_return_only_the_new_events_and_where_they_go(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    _write(log, _line("run.start", home="/tmp/h1") + _line("llm.turn"))
    activity = TrialActivity(tmp_path)
    (first,) = activity.read()

    assert activity.read() == []
    _write(log, _line("thinking", text="hmm") + _line("text", text="ok"))
    (second,) = activity.read()

    assert second["log"] == first["log"]
    assert second["first"] == 2
    assert _types(second) == ["thinking", "text"]


def test_a_half_written_line_waits_for_the_next_read(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    whole = _line("text", text="all of it")
    _write(log, _line("run.start", home="/tmp/h1") + whole[:10])
    activity = TrialActivity(tmp_path)
    (first,) = activity.read()
    assert _types(first) == ["run.start"]

    _write(log, whole[10:])
    (second,) = activity.read()

    assert second["first"] == 1
    assert _types(second) == ["text"]


def test_a_log_without_its_first_line_yet_is_left_for_later(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    _write(log, '{"ts": "2026')

    assert TrialActivity(tmp_path).read() == []


def test_a_log_harbor_files_under_its_step_keeps_its_name_and_gains_the_step(
    tmp_path: Path,
) -> None:
    trial = tmp_path / "basics__abc"
    live = trial / "agent" / "kodo.jsonl"
    _write(live, _line("run.start", home="/tmp/h1") + _line("text", text="step one"))
    activity = TrialActivity(tmp_path)
    (running,) = activity.read()

    # Harbor's step archive: agent/ contents move under steps/<step>/agent/;
    # the next step writes a fresh agent/kodo.jsonl.
    filed = trial / "steps" / "s1-exact-output" / "agent" / "kodo.jsonl"
    filed.parent.mkdir(parents=True)
    live.rename(filed)
    _write(live, _line("run.start", ts="2026-10-18T10:05:00.000+00:00", home="/tmp/h2"))
    chunks = activity.read()

    by_step = {chunk["step"]: chunk for chunk in chunks}
    assert set(by_step) == {"s1-exact-output", None}
    assert by_step["s1-exact-output"]["log"] == running["log"]
    assert by_step["s1-exact-output"]["events"] == []  # nothing new, only its step
    assert by_step[None]["log"] != running["log"]
    assert by_step[None]["first"] == 0
    assert by_step[None]["started"] == "2026-10-18T10:05:00.000+00:00"


def test_a_fresh_reader_names_filed_logs_by_their_step(tmp_path: Path) -> None:
    trial = tmp_path / "basics__abc"
    _write(trial / "steps" / "s2-edit" / "agent" / "kodo.jsonl", _line("run.start", home="/b"))
    _write(trial / "steps" / "s1-read" / "agent" / "kodo.jsonl", _line("run.start", home="/a"))

    chunks = TrialActivity(tmp_path).read()

    assert [chunk["step"] for chunk in chunks] == ["s1-read", "s2-edit"]


def test_an_ended_trial_is_read_once_more_and_then_never_again(tmp_path: Path) -> None:
    trial = tmp_path / "task__abc"
    log = trial / "agent" / "kodo.jsonl"
    _write(log, _line("run.start", home="/tmp/h1"))
    (trial / "result.json").write_text("{}", encoding="utf-8")
    activity = TrialActivity(tmp_path)

    assert len(activity.read()) == 1
    _write(log, _line("text", text="written after the end"))
    assert activity.read() == []


def test_a_log_comes_with_the_instruction_beside_it_once(tmp_path: Path) -> None:
    agent = tmp_path / "task__abc" / "agent"
    _write(agent / "instruction.md", "Create `t1/summary.txt`.\n")
    _write(agent / "kodo.jsonl", _line("run.start", home="/tmp/h1"))
    activity = TrialActivity(tmp_path)
    (first,) = activity.read()

    _write(agent / "kodo.jsonl", _line("text", text="done"))
    (second,) = activity.read()

    assert first["prompt"] == "Create `t1/summary.txt`.\n"
    assert second["prompt"] is None


def test_each_step_of_a_multi_step_trial_comes_with_its_own_instruction(tmp_path: Path) -> None:
    trial = tmp_path / "basics__abc"
    agent = trial / "agent"
    _write(agent / "instruction.md", "step one\n")
    _write(agent / "kodo.jsonl", _line("run.start", home="/tmp/h1"))
    activity = TrialActivity(tmp_path)
    (running,) = activity.read()

    filed = trial / "steps" / "s1-exact-output" / "agent"
    filed.parent.mkdir(parents=True)
    agent.rename(filed)
    _write(agent / "instruction.md", "step two\n")
    _write(agent / "kodo.jsonl", _line("run.start", ts="2026-10-18T10:05:00.000+00:00", home="/h2"))
    by_step = {chunk["step"]: chunk for chunk in activity.read()}

    assert running["prompt"] == "step one\n"
    assert by_step["s1-exact-output"]["prompt"] is None  # already sent
    assert by_step[None]["prompt"] == "step two\n"
    fresh = {chunk["step"]: chunk["prompt"] for chunk in TrialActivity(tmp_path).read()}
    assert fresh == {"s1-exact-output": "step one\n", None: "step two\n"}


def test_an_instruction_not_found_beside_its_log_is_looked_for_again(tmp_path: Path) -> None:
    # Harbor moved the log to steps/ but not the instruction yet, or the
    # instruction was not there yet: the next read finds it.
    agent = tmp_path / "task__abc" / "agent"
    _write(agent / "kodo.jsonl", _line("run.start", home="/tmp/h1"))
    activity = TrialActivity(tmp_path)
    (first,) = activity.read()

    _write(agent / "instruction.md", "late\n")
    (second,) = activity.read()

    assert first["prompt"] is None
    assert second["prompt"] == "late\n"
    assert second["events"] == []
    assert activity.read() == []


def test_a_missing_job_directory_has_no_activity(tmp_path: Path) -> None:
    assert TrialActivity(tmp_path / "not-yet").read() == []


def test_a_trial_without_a_kodo_log_has_no_activity(tmp_path: Path) -> None:
    # A control arm (terminus-2) writes no kodo.jsonl.
    _write(tmp_path / "control__abc" / "agent" / "terminus.log", "anything\n")

    assert TrialActivity(tmp_path).read() == []


def test_lines_that_are_no_event_are_skipped_and_not_counted(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    _write(log, _line("run.start", home="/tmp/h1") + "[1, 2]\n" + '"text"\n' + "not json\n")
    activity = TrialActivity(tmp_path)
    (first,) = activity.read()
    assert _types(first) == ["run.start"]

    _write(log, _line("text", text="after"))
    (second,) = activity.read()

    assert second["first"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_log_or_steps_folder_that_cannot_be_read_is_left_for_later(tmp_path: Path) -> None:
    log = tmp_path / "task__abc" / "agent" / "kodo.jsonl"
    _write(log, _line("run.start", home="/tmp/h1"))
    steps = tmp_path / "task__abc" / "steps"
    steps.mkdir()
    log.chmod(0)
    steps.chmod(0)
    try:
        assert TrialActivity(tmp_path).read() == []
    finally:
        log.chmod(0o644)
        steps.chmod(0o755)
