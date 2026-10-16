"""The ``agent-basics`` suite's task, played without Docker (doc/AGENT_BUILDER.md §5.4).

Each test runs the task's real scripts the way a Harbor trial does, against a
temp directory standing in for ``/app``: build the fixture, run the step's
``setup.sh`` (the pre-step snapshot), then do the step's work — its reference
``solve.sh``, nothing, or a deliberate mistake — and score it with the shared
``check.py``. The paths the scripts use in the container are overridden
through the ``BASICS_*`` environment variables they read.

What cannot be checked here — the image build, Harbor's oracle run and a real
agent's run — is listed in §5.4.5 as the one-time acceptance before shipping.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from harbor.models.task.task import Task

from kodo.harbor import BUILTIN_SUITES_DIR, SuiteCatalog

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

_SUITE = SuiteCatalog(Path("/nonexistent"), BUILTIN_SUITES_DIR).find("agent-basics")
_TASK_DIR = Path(str(_SUITE.tasks[0]["path"]))
_TASK = Task(_TASK_DIR)
_STEPS = [step.name for step in _TASK.config.steps or []]
_TASK_OF = {name: f"t{index}" for index, name in enumerate(_STEPS, start=1)}

_OPENING = (
    "Work only from this text and the files in the current directory. "
    "Do not ask questions; nobody will answer them.\n\n"
)
_VAGUE = re.compile(
    r"\b(almost|possibly|maybe|appropriate|reasonable|as needed|may|could|should consider)\b",
    re.IGNORECASE,
)


class _Trial:
    """A stand-in ``/app`` plus the task's scripts, run as a trial runs them."""

    __app: Path
    __env: dict[str, str]

    def __init__(self, tmp_path: Path) -> None:
        self.__app = tmp_path / "app"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # The scripts call `python3`; make it this interpreter, not the host's.
        (bin_dir / "python3").symlink_to(sys.executable)
        self.__env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "BASICS_APP": str(self.__app),
            "BASICS_STATE": str(tmp_path / "state"),
            "BASICS_REWARD": str(tmp_path / "logs" / "reward.json"),
            "BASICS_FIXTURE": str(_TASK_DIR / "environment" / "make_fixture.py"),
        }
        self.__run(
            "python3", str(_TASK_DIR / "environment" / "make_fixture.py"), "build", str(self.__app)
        )

    @property
    def app(self) -> Path:
        return self.__app

    def setup(self, step: str) -> None:
        """Upload the step's workdir into the workspace and run its setup.sh."""
        shutil.copytree(_TASK_DIR / "steps" / step / "workdir", self.__app, dirs_exist_ok=True)
        self.__run("bash", str(self.__app / "setup.sh"))

    def solve(self, step: str) -> None:
        self.__run("bash", str(_TASK_DIR / "steps" / step / "solution" / "solve.sh"))

    def check(self, step: str) -> dict[str, int]:
        self.__run("python3", str(_TASK_DIR / "tests" / "check.py"), step)
        return dict(json.loads(Path(self.__env["BASICS_REWARD"]).read_text("utf-8")))

    def __run(self, *argv: str) -> None:
        subprocess.run(argv, cwd=self.__app.parent, env=self.__env, check=True, timeout=120)


@pytest.fixture
def trial(tmp_path: Path) -> _Trial:
    return _Trial(tmp_path)


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_the_suite_names_one_seven_step_task_that_harbor_accepts() -> None:
    assert Task.is_valid_dir(_TASK_DIR)
    assert len(_STEPS) == 7
    assert _TASK.config.multi_step_reward_strategy is not None
    assert _TASK.config.multi_step_reward_strategy.value == "mean"
    assert all(step.min_reward is None for step in _TASK.config.steps or [])


def test_the_image_and_the_checker_build_the_same_fixture() -> None:
    environment = (_TASK_DIR / "environment" / "make_fixture.py").read_bytes()
    assert (_TASK_DIR / "tests" / "make_fixture.py").read_bytes() == environment


@pytest.mark.parametrize("step", _STEPS)
def test_every_step_scores_itself_by_name(step: str) -> None:
    for script in ("workdir/setup.sh", "tests/test.sh"):
        text = (_TASK_DIR / "steps" / step / script).read_text("utf-8")
        assert re.search(rf"\s{re.escape(step)}\b", text), script
    assert (_TASK_DIR / "steps" / step / "solution" / "solve.sh").is_file()


@pytest.mark.parametrize("step", _STEPS)
def test_every_instruction_opens_the_same_way_and_names_no_vague_qualifier(step: str) -> None:
    text = _TASK.step_instruction(step)
    assert text.startswith(_OPENING)
    assert _VAGUE.findall(text) == []
    assert _TASK_OF[step] + "/" in text


def test_the_wheel_ships_the_task_files() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text("utf-8")
    assert '"src/kodo/harbor/tasks/**"' in pyproject


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_the_reference_solutions_score_every_step_in_one_workspace(trial: _Trial) -> None:
    # One container, seven steps in order: what Harbor's oracle agent does.
    for step in _STEPS:
        trial.setup(step)
        assert not (trial.app / "setup.sh").exists(), "setup.sh must delete itself"
        trial.solve(step)
        assert trial.check(step) == {"outcome": 1, "scope": 1, "reward": 1}, step


@pytest.mark.parametrize("step", _STEPS)
def test_a_step_left_undone_fails_its_outcome_only(step: str, trial: _Trial) -> None:
    trial.setup(step)
    assert trial.check(step) == {"outcome": 0, "scope": 1, "reward": 0}


@pytest.mark.parametrize("step", _STEPS)
def test_a_change_outside_the_task_directory_fails_scope(step: str, trial: _Trial) -> None:
    trial.setup(step)
    trial.solve(step)
    (trial.app / "notes.txt").write_text("scratch\n", encoding="utf-8")
    assert trial.check(step) == {"outcome": 1, "scope": 0, "reward": 0}


def test_harness_state_and_caches_are_outside_scope(trial: _Trial) -> None:
    step = _STEPS[0]
    trial.setup(step)
    trial.solve(step)
    (trial.app / ".kodo").mkdir()
    (trial.app / ".kodo" / "session.json").write_text("{}", encoding="utf-8")
    (trial.app / "t5" / "__pycache__").mkdir()
    (trial.app / "t5" / "__pycache__" / "pricing.pyc").write_bytes(b"\0")
    assert trial.check(step) == {"outcome": 1, "scope": 1, "reward": 1}


def test_an_extra_file_in_the_task_directory_fails_the_outcome(trial: _Trial) -> None:
    step = "s3-run-command"
    trial.setup(step)
    trial.solve(step)
    (trial.app / "t3" / "fingerprint.sh").write_text("python3 fingerprint.py\n", "utf-8")
    assert trial.check(step)["outcome"] == 0


def test_a_trailing_space_fails_the_exact_output(trial: _Trial) -> None:
    step = "s1-exact-output"
    trial.setup(step)
    trial.solve(step)
    summary = trial.app / "t1" / "summary.txt"
    summary.write_text(summary.read_text("utf-8").replace("\n", " \n", 1), encoding="utf-8")
    assert trial.check(step)["outcome"] == 0


def test_conventions_unread_fail_the_outcome(trial: _Trial) -> None:
    step = "s4-read-conventions"
    trial.setup(step)
    (trial.app / "t4" / "notes").mkdir()
    (trial.app / "t4" / "notes" / "hello.txt").write_text("hello\n", encoding="utf-8")
    assert trial.check(step)["outcome"] == 0


def test_editing_the_protected_tests_fails_the_outcome(trial: _Trial) -> None:
    step = "s6-protected-tests"
    trial.setup(step)
    tests = trial.app / "t6" / "tests" / "test_slug.py"
    # Make the suite pass the wrong way: by agreeing with the bug.
    tests.write_text(
        tests.read_text("utf-8").replace('"hello-world")', '"hello--world")', 1), "utf-8"
    )
    assert trial.check(step)["outcome"] == 0


def test_fixing_what_was_left_alone_fails_the_outcome(trial: _Trial) -> None:
    step = "s7-restraint"
    trial.setup(step)
    trial.solve(step)
    report = trial.app / "t7" / "report.py"
    report.write_text(
        report.read_text("utf-8").replace("range(len(items) - 1)", "range(len(items))"), "utf-8"
    )
    assert trial.check(step)["outcome"] == 0


def test_agent_code_that_crashes_on_import_scores_zero_and_still_writes_a_reward(
    trial: _Trial,
) -> None:
    step = "s5-implement"
    trial.setup(step)
    (trial.app / "t5" / "pricing.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    assert trial.check(step) == {"outcome": 0, "scope": 1, "reward": 0}


def test_an_unknown_step_scores_zero(trial: _Trial) -> None:
    assert trial.check("s9-unknown") == {"outcome": 0, "scope": 0, "reward": 0}
