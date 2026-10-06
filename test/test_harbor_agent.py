"""``kodo.harbor.agent`` (doc/HARBOR.md): the Harbor adapter, exercised through
Harbor's own classes.

``run`` is executed for real: a stand-in environment runs every ``exec`` in a
local bash — as a task container would — against a scripted ``kodo-headless``
on ``PATH``, so the tests see what the command line, the exit-code handling and
the error classification actually do. The context and ``trajectory.json`` are
checked from the files such a run leaves in the agent log directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from harbor.agents.factory import AgentFactory
from harbor.agents.installed.base import AgentAuthenticationError, NonZeroAgentExitCodeError
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig
from harbor.models.trajectories import Trajectory

from kodo.harbor.agent import KodoAgent, KodoAgentOptions, KodoRunRecord, session_to_trajectory

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the command in bash")

_IMPORT_PATH = "kodo.harbor.agent:KodoAgent"

# ---------------------------------------------------------------------------
# Construction through Harbor
# ---------------------------------------------------------------------------


def test_harbor_loads_the_agent_by_import_path(tmp_path: Path) -> None:
    agent = AgentFactory.create_agent_from_import_path(
        _IMPORT_PATH,
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5",
        agent="kodo_guide",
        turn_timeout_sec=600,
    )
    assert isinstance(agent, KodoAgent)
    assert agent.name() == "kodo"
    assert agent.capabilities.atif is True
    options = agent.options
    assert isinstance(options, KodoAgentOptions)
    assert (options.agent, options.turn_timeout_sec) == ("kodo_guide", 600)


def test_unknown_kwarg_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no_such_option"):
        KodoAgent(logs_dir=tmp_path, model_name="anthropic/x", no_such_option=1)


def test_a_model_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a model"):
        KodoAgent(logs_dir=tmp_path)


def test_version_follows_the_uploaded_wheel(tmp_path: Path) -> None:
    agent = KodoAgent(
        logs_dir=tmp_path,
        model_name="anthropic/x",
        kodo_wheel=str(tmp_path / "py_kodo-9.8.7-py3-none-any.whl"),
    )
    assert agent.version() == "9.8.7"
    pinned = KodoAgent(logs_dir=tmp_path, model_name="anthropic/x", version="1.2.3")
    assert pinned.version() == "1.2.3"


# ---------------------------------------------------------------------------
# A stand-in task container
# ---------------------------------------------------------------------------

_FAKE_HEADLESS = r"""#!/usr/bin/env bash
printf '%s\n' "$PWD" "$@" > "$FAKE_ARGS"
while [ $# -gt 0 ]; do
  case "$1" in
    --result) result="$2"; shift 2 ;;
    --transcript-dir) transcript="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$FAKE_MODE" in
  ok)
    cat "$FAKE_FIXTURE/stdout.jsonl"
    cp "$FAKE_FIXTURE/result.json" "$result"
    mkdir -p "$transcript" && cp -R "$FAKE_FIXTURE/session/." "$transcript/"
    echo "KODO-RESULT outcome=completed error="
    exit 0 ;;
  nokey)
    echo '{"type": "tool.call", "document": "curl: (7) Connection refused"}'
    echo "KODO-RESULT outcome=startup_error error=No API key for 'anthropic' in the environment"
    exit 4 ;;
  runtime)
    echo '{"type": "tool.call", "document": "rate limit reached; Connection refused"}'
    echo "KODO-RESULT outcome=runtime_error error=boom"
    exit 3 ;;
  hang)
    trap 'echo stopped > "$FAKE_MARK"; exit 130' TERM
    echo '{"type": "usage", "model": "m", "input_tokens": 7, "cache_read_tokens": 2,'\
' "output_tokens": 1, "usd": 0.5}'
    while true; do sleep 0.1; done ;;
esac
"""


class _ShellEnvironment:
    """Runs ``exec`` in a local bash with a private HOME, like a task container."""

    default_user: str | None = None

    def __init__(self, workdir: Path, home: Path, env: dict[str, str]) -> None:
        self.__workdir = workdir
        self.__env = {"HOME": str(home), "PATH": f"{home / '.local/bin'}:{os.environ['PATH']}"}
        self.__env.update(env)
        self.commands: list[str] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd or self.__workdir,
            env={**self.__env, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return ExecResult(stdout=out.decode(), stderr=err.decode(), return_code=proc.returncode)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(Path(source_path).read_bytes())


class _Trial:
    def __init__(self, tmp_path: Path, mode: str, **kwargs: object) -> None:
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.workdir = tmp_path / "app"
        self.workdir.mkdir()
        home = tmp_path / "home"
        (home / ".local/bin").mkdir(parents=True)
        script = home / ".local/bin/kodo-headless"
        script.write_text(_FAKE_HEADLESS, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.args_file = tmp_path / "args.txt"
        self.mark = tmp_path / "stopped.txt"
        self.fixture = tmp_path / "fixture"
        self.shell = _ShellEnvironment(
            self.workdir,
            home,
            {
                "FAKE_ARGS": str(self.args_file),
                "FAKE_MODE": mode,
                "FAKE_MARK": str(self.mark),
                "FAKE_FIXTURE": str(self.fixture),
            },
        )
        self.agent = KodoAgent(
            logs_dir=self.logs,
            model_name=str(kwargs.pop("model_name", "anthropic/claude-sonnet-5")),
            environment_logs_dir=PurePosixPath(str(self.logs)),
            **kwargs,
        )

    @property
    def environment(self) -> BaseEnvironment:
        return cast(BaseEnvironment, self.shell)

    def args(self) -> list[str]:
        return self.args_file.read_text(encoding="utf-8").splitlines()

    async def run(self, instruction: str = "fix it") -> None:
        await self.agent.run(instruction, self.environment, AgentContext())


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def test_run_hands_the_instruction_over_as_a_file(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "ok", agent="kodo_guide", thinking_level="high")
    _write_fixture(trial.fixture)
    instruction = "Fix `it`;\n$(rm -rf /) \"quoted\" 'single'"

    await trial.run(instruction)

    args = trial.args()
    cwd = args[0]
    assert cwd == str(trial.workdir)
    assert _flag(args, "--cwd") == str(trial.workdir)
    assert _flag(args, "--model") == "anthropic/claude-sonnet-5"
    assert _flag(args, "--agent") == "kodo_guide"
    assert _flag(args, "--thinking-level") == "high"
    assert Path(_flag(args, "--prompt-file")).read_text(encoding="utf-8") == instruction
    assert "--llama-url" not in args
    assert (trial.logs / "kodo.jsonl").is_file()


async def test_local_model_attaches_to_the_host_llama(tmp_path: Path) -> None:
    trial = _Trial(
        tmp_path,
        "ok",
        model_name="local/qwen-q4",
        llama_url="http://host.docker.internal:8090",
        registry_file="/kodo-host/local-llm-registry.json",
        turn_timeout_sec=900,
    )
    _write_fixture(trial.fixture)

    await trial.run()

    args = trial.args()
    assert _flag(args, "--model") == "local/qwen-q4"
    assert _flag(args, "--llama-url") == "http://host.docker.internal:8090"
    assert _flag(args, "--registry-file") == "/kodo-host/local-llm-registry.json"
    assert float(_flag(args, "--timeout")) == 900


async def test_workdir_overrides_the_task_directory(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    trial = _Trial(tmp_path, "ok", workdir=str(other))
    _write_fixture(trial.fixture)

    await trial.run()

    assert _flag(trial.args(), "--cwd") == str(other)


async def test_missing_key_is_an_authentication_error(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "nokey")
    with pytest.raises(AgentAuthenticationError):
        await trial.run()


async def test_tool_output_never_decides_the_error_class(tmp_path: Path) -> None:
    """kodo.jsonl is full of tool output; only the KODO-RESULT line is classified."""
    trial = _Trial(tmp_path, "runtime")
    with pytest.raises(NonZeroAgentExitCodeError) as caught:
        await trial.run()
    assert type(caught.value) is NonZeroAgentExitCodeError
    assert "outcome=runtime_error error=boom" in str(caught.value)


async def test_cancelled_run_stops_kodo_inside_the_container(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "hang")
    task = asyncio.create_task(trial.run())
    stream = trial.logs / "kodo.jsonl"
    for _ in range(200):  # the fake prints once its TERM trap is installed
        if stream.exists() and stream.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert trial.mark.read_text(encoding="utf-8").strip() == "stopped"
    context = AgentContext()
    trial.agent.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.cost_usd) == (7, 2, 0.5)
    assert context.metadata == {
        "kodo": {
            "outcome": "killed",
            "kodo_version": trial.agent.version(),
            "result_source": "partial_log",
            "result_schema_version": 1,
        }
    }


# ---------------------------------------------------------------------------
# populate_context_post_run
# ---------------------------------------------------------------------------


async def test_context_and_trajectory_come_from_the_run(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "ok")
    _write_fixture(trial.fixture)
    await trial.run()

    context = AgentContext()
    trial.agent.populate_context_post_run(context)

    assert context.n_input_tokens == 1500
    assert context.n_cache_tokens == 400
    assert context.n_output_tokens == 60
    assert context.cost_usd == pytest.approx(0.03)
    assert context.model_usage is not None
    assert context.model_usage["claude-sonnet-5"].n_cache_tokens == 400
    assert context.metadata is not None
    kodo = cast(dict[str, object], context.metadata["kodo"])
    assert (kodo["outcome"], kodo["questions_asked"], kodo["result_source"]) == (
        "completed",
        0,
        "result",
    )
    trajectory = Trajectory.model_validate_json(
        (trial.logs / "trajectory.json").read_text(encoding="utf-8")
    )
    assert trajectory.agent.name == "kodo"
    assert trajectory.subagent_trajectories is not None


def test_nothing_on_disk_leaves_the_context_empty(tmp_path: Path) -> None:
    assert KodoRunRecord.load(tmp_path) is None
    agent = KodoAgent(logs_dir=tmp_path, model_name="anthropic/x")
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.is_empty()
    assert not (tmp_path / "trajectory.json").exists()


# ---------------------------------------------------------------------------
# The session log → ATIF
# ---------------------------------------------------------------------------


def test_session_log_becomes_one_trajectory_with_embedded_subagents(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write_session(session)

    trajectory = session_to_trajectory(
        session,
        agent_version="0.5.29",
        model_name="anthropic/claude-sonnet-5",
        top_agent="kodo_guide",
    )

    assert trajectory is not None
    assert trajectory.session_id == "session"
    sources = [step.source for step in trajectory.steps]
    assert sources == ["user", "agent", "agent"]
    first = trajectory.steps[1]
    assert first.reasoning_content == "plan it"
    assert first.message == "Let me look."
    assert first.model_name == "claude-sonnet-5"
    assert first.metrics is not None
    assert (first.metrics.prompt_tokens, first.metrics.cached_tokens) == (600, 400)
    assert first.tool_calls is not None
    assert [c.function_name for c in first.tool_calls] == ["read_file", "run_subagent_kodo_planner"]
    assert first.tool_calls[0].arguments == {"path": "a.py"}
    assert first.observation is not None
    by_call = {r.source_call_id: r for r in first.observation.results}
    assert by_call["t1"].content == '{"content": "print(1)"}'
    spawn_ref = by_call["t2"].subagent_trajectory_ref
    assert spawn_ref is not None and spawn_ref[0].trajectory_id == "sub1"
    assert trajectory.steps[2].message == "Done."

    assert trajectory.subagent_trajectories is not None
    (planner,) = trajectory.subagent_trajectories
    assert (planner.trajectory_id, planner.agent.name) == ("sub1", "kodo/kodo_planner")
    assert [step.source for step in planner.steps] == ["user", "agent"]
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 600 + 900 + 300
    assert trajectory.final_metrics.total_completion_tokens == 20 + 40 + 10


def test_empty_session_has_no_trajectory(tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    assert (
        session_to_trajectory(tmp_path / "s", agent_version="v", model_name="m", top_agent="a")
        is None
    )


# ---------------------------------------------------------------------------
# Fixtures written the way kodo writes them (doc/SESSIONS.md, doc/HEADLESS.md)
# ---------------------------------------------------------------------------


def _usage(model: str, inp: int, cache_read: int, out: int, usd: float) -> dict[str, object]:
    return {
        "type": "usage",
        "model": model,
        "agent": "kodo_guide",
        "last_call_tokens": {
            "input": inp,
            "cache_read": cache_read,
            "cache_write": 0,
            "output": out,
        },
        "usd_cost": usd,
        "stop_reason": "end_turn",
    }


def _jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _write_session(session: Path) -> None:
    ts = "2026-09-25T10:00:00+00:00"
    _jsonl(
        session / "session.jsonl",
        [
            {"type": "greeting", "text": "hi"},
            {"role": "user", "content": "fix it", "ts": ts},
            _usage("claude-sonnet-5", 200, 400, 20, 0.01),
            {
                "role": "assistant",
                "ts": ts,
                "content": [
                    {"type": "thinking", "thinking": "plan it"},
                    {"type": "text", "text": "Let me look."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "run_subagent_kodo_planner",
                        "input": {"instructions": "plan"},
                    },
                ],
            },
            {"type": "subsession_start", "subsession_id": "sub1", "agent": "kodo_planner"},
            {"type": "subsession_end", "subsession_id": "sub1", "agent": "kodo_planner"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": '{"content": "print(1)"}',
                    },
                    {"type": "tool_result", "tool_use_id": "t2", "content": '{"plan": "x"}'},
                ],
            },
            _usage("claude-sonnet-5", 900, 0, 40, 0.01),
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ],
    )
    _jsonl(
        session / "subsessions" / "sub1.jsonl",
        [
            {"role": "user", "content": "# Task\n\nplan"},
            _usage("claude-sonnet-5", 300, 0, 10, 0.01),
            {"role": "assistant", "content": [{"type": "text", "text": "The plan."}]},
        ],
    )


def _write_fixture(fixture: Path) -> None:
    _write_session(fixture / "session")
    result = {
        "schema_version": 1,
        "outcome": "completed",
        "session_id": "s1",
        "agent": "kodo_problem_solver",
        "model": "anthropic/claude-sonnet-5",
        "per_model": {
            "claude-sonnet-5": {
                "calls": 3,
                "input_tokens": 1500,
                "cache_read_tokens": 400,
                "output_tokens": 60,
                "usd": 0.03,
            }
        },
        "per_agent": {},
        "tool_calls": 2,
        "tool_denials": 0,
        "questions_asked": 0,
        "nudges": 0,
    }
    (fixture / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _jsonl(fixture / "stdout.jsonl", [{"type": "run.start"}])


# ---------------------------------------------------------------------------
# More of the adapter's contract
# ---------------------------------------------------------------------------


def test_an_agent_without_kodo_options_is_refused(tmp_path: Path) -> None:
    class _Bare(KodoAgent):
        options_model = None

    with pytest.raises(ValueError, match="requires KodoAgentOptions"):
        _Bare(logs_dir=tmp_path, model_name="anthropic/x")


def test_version_falls_back_to_this_kodo_or_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.metadata

    unversioned = KodoAgent(
        logs_dir=tmp_path, model_name="anthropic/x", kodo_wheel=str(tmp_path / "custom.whl")
    )
    assert unversioned.version() == importlib.metadata.version("py-kodo")

    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    assert KodoAgent(logs_dir=tmp_path, model_name="anthropic/x").version() is None


class _RecordingEnvironment:
    """A container that accepts every command and upload, and remembers them."""

    default_user: str | None = None

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[Path, str]] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout="", stderr="", return_code=0)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        self.uploads.append((Path(source_path), target_path))

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        self.uploads.append((Path(source_dir), target_dir))


async def test_install_uploads_the_wheel_and_the_user_agents(tmp_path: Path) -> None:
    wheel = tmp_path / "py_kodo-9.8.7-py3-none-any.whl"
    agents = tmp_path / "agents"
    agent = KodoAgent(
        logs_dir=tmp_path,
        model_name="anthropic/x",
        kodo_wheel=str(wheel),
        agents_dir=str(agents),
        python_version="3.13",
    )
    environment = _RecordingEnvironment()

    await agent.install(cast(BaseEnvironment, environment))

    staged_wheel = f"/installed-agent/{wheel.name}"
    assert environment.uploads == [
        (wheel, staged_wheel),
        (agents, "/installed-agent/kodo-agents"),
    ]
    script = "\n".join(environment.commands)
    assert f"uv tool install --force --python 3.13 {staged_wheel}" in script
    assert 'cp -R /installed-agent/kodo-agents "$HOME/.kodo/agents"' in script


@pytest.mark.parametrize(
    ("version", "requirement"), [("1.2.3", "py-kodo==1.2.3"), (None, "py-kodo")]
)
async def test_install_without_a_wheel_uses_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None, requirement: str
) -> None:
    import importlib.metadata

    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    agent = KodoAgent(logs_dir=tmp_path, model_name="anthropic/x", version=version)
    environment = _RecordingEnvironment()

    await agent.install(cast(BaseEnvironment, environment))

    assert environment.uploads == []
    install = next(c for c in environment.commands if "uv tool install" in c)
    assert f"uv tool install --force --python 3.12 {requirement};" in install


async def test_run_warns_about_mcp_servers_and_installs_task_skills(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    skills = tmp_path / "task-skills"
    (skills / "lint").mkdir(parents=True)
    (skills / "lint" / "SKILL.md").write_text("# lint\n", encoding="utf-8")
    trial = _Trial(
        tmp_path,
        "ok",
        mcp_servers=[MCPServerConfig(name="docs", url="http://mcp.invalid/sse")],
        skills_dir=str(skills),
    )
    _write_fixture(trial.fixture)

    with caplog.at_level("WARNING"):
        await trial.run()

    assert "Kodo has no MCP client" in caplog.text
    copied = tmp_path / "home" / ".kodo" / "skills" / "lint" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == "# lint\n"


async def test_the_instruction_file_is_handed_to_the_default_user(tmp_path: Path) -> None:
    trial = _Trial(tmp_path, "ok")
    _write_fixture(trial.fixture)
    owner = str(os.getuid())
    trial.shell.default_user = owner

    await trial.run()

    instruction = trial.logs / "instruction.md"
    assert instruction.stat().st_uid == os.getuid()
    assert any(c.startswith(f"set -o pipefail; chown {owner} ") for c in trial.shell.commands)


def test_an_unconvertible_session_log_still_fills_the_context(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _jsonl(
        tmp_path / "kodo-session" / "session.jsonl",
        [
            {"role": "user", "content": "fix it", "ts": "not-a-timestamp"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ],
    )
    _jsonl(tmp_path / "kodo.jsonl", [{"type": "run.result", "outcome": "completed"}])
    agent = KodoAgent(logs_dir=tmp_path, model_name="anthropic/x")
    context = AgentContext()

    with caplog.at_level("WARNING"):
        agent.populate_context_post_run(context)

    assert "Could not convert kodo's session log to ATIF" in caplog.text
    assert not (tmp_path / "trajectory.json").exists()
    assert context.metadata is not None
    assert cast(dict[str, object], context.metadata["kodo"])["outcome"] == "completed"


# ---------------------------------------------------------------------------
# KodoRunRecord
# ---------------------------------------------------------------------------


def test_the_final_event_stands_in_for_a_missing_result_file(tmp_path: Path) -> None:
    (tmp_path / "kodo-result.json").write_text('["not", "a", "result"]', encoding="utf-8")
    _jsonl(
        tmp_path / "kodo.jsonl",
        [
            {"type": "run.result", "outcome": "runtime_error"},
            {"type": "run.result", "outcome": "completed", "per_model": {"m": {"calls": 1}}},
        ],
    )

    record = KodoRunRecord.load(tmp_path)

    assert record is not None
    assert (record.outcome, record.source) == ("completed", "result")


def test_a_killed_run_is_rebuilt_from_its_usage_events(tmp_path: Path) -> None:
    lines = [
        json.dumps({"type": "run.start"}),
        json.dumps({"type": "usage", "model": "a", "input_tokens": 10, "usd": 0.25}),
        json.dumps({"type": "usage", "input_tokens": True, "output_tokens": "many"}),
        json.dumps(["not", "an", "event"]),
        '{"type": "usage", "model": "a", "input_tok',
        json.dumps({"type": "usage", "model": "a", "cache_read_tokens": 4, "output_tokens": 3}),
        "KODO-RESULT outcome=killed error=",
    ]
    (tmp_path / "kodo.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    record = KodoRunRecord.load(tmp_path)

    assert record is not None
    assert (record.outcome, record.source) == ("killed", "partial_log")
    context = AgentContext()
    record.fill(context, kodo_version="1.0")
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (10, 4, 3)
    assert context.cost_usd == 0.25
    assert context.model_usage is not None
    assert set(context.model_usage) == {"a", "unknown"}
    assert context.model_usage["unknown"].n_input_tokens == 0


@pytest.mark.parametrize("per_model", [None, "not a mapping", {"m": "not a row"}])
def test_a_result_without_per_model_rows_uses_the_cumulative_totals(
    tmp_path: Path, per_model: object
) -> None:
    result: dict[str, object] = {
        "schema_version": 7,
        "outcome": "completed",
        "cumulative_input_tokens": 1000,
        "cumulative_input_tokens_uncached": 250,
        "cumulative_output_tokens": 80,
        "cumulative_usd": 0.5,
        "unrelated": "dropped",
    }
    if per_model is not None:
        result["per_model"] = per_model
    (tmp_path / "kodo-result.json").write_text(json.dumps(result), encoding="utf-8")

    record = KodoRunRecord.load(tmp_path)
    assert record is not None
    context = AgentContext()
    record.fill(context, kodo_version="2.0")

    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (
        1000,
        750,
        80,
    )
    assert context.cost_usd == 0.5
    assert context.model_usage == {}
    assert context.metadata == {
        "kodo": {
            "outcome": "completed",
            "kodo_version": "2.0",
            "result_source": "result",
            "result_schema_version": 7,
        }
    }
