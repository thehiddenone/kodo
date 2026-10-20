"""Behavioral tests for benchmark sessions (doc/BENCHMARK_SESSIONS.md §4).

A real :class:`~kodo.server.SessionManager` builds real sessions; the run is a
stand-in ``kodo-harbor`` (``_STAND_IN`` below, a Python script) that writes
Harbor-shaped trial results and a summary into the job directory, honours a
Stop the way ``kodo-harbor --stop-on-stdin-close`` does (stdin closed → wind
down → exit 130), and records the argv and environment it got. The managed
llama-server is :class:`_Llama`, which only records what it was asked. Nothing
starts Docker, Harbor or a llama-server.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest

import kodo.agents
from kodo.agents import AgentRegistry
from kodo.harbor import AGENT_BASICS_SUITE
from kodo.llms import LLMGateway, LocalLLMEntry, add_local_entry, get_cloud_registry
from kodo.project import WorkspaceLayout
from kodo.runtime import WorkflowEngine
from kodo.server import HarborJobs, Session, SessionManager, push_trial_activity
from kodo.state import TransientStore
from kodo.transport import Connection

_AGENTS_DIR = Path(kodo.agents.__file__).parent
_MODEL = "bench-gguf"
_AGENT = "kodo_problem_solver"

_STAND_IN = r"""
import json, os, sys, threading, time
from pathlib import Path

plan = json.loads(Path(os.environ["KODO_STAND_IN_PLAN"]).read_text())
args = sys.argv[2:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
Path(plan["record"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "env": {name: os.environ.get(name) for name in plan.get("env", [])},
}))
print("stand-in kodo-harbor starting", flush=True)
if plan.get("exit_before_job") is not None:
    print("error: Docker is required: the docker CLI is not on PATH", flush=True)
    sys.exit(plan["exit_before_job"])
job = Path(opt("--jobs-dir")) / opt("--job-name")
job.mkdir(parents=True)
(job / "result.json").write_text(json.dumps({"n_total_trials": len(plan["rewards"])}))
stop = threading.Event()
def watch():
    while os.read(0, 4096):
        pass
    stop.set()
if not plan.get("ignore_stop"):
    threading.Thread(target=watch, daemon=True).start()
for index, reward in enumerate(plan["rewards"]):
    if stop.wait(plan.get("trial_seconds", 0.05)):
        break
    trial = job / f"task-{index}__abc"
    trial.mkdir()
    if plan.get("activity"):
        (trial / "agent").mkdir()
        (trial / "agent" / "kodo.jsonl").write_text(
            json.dumps({"ts": f"2026-10-18T10:00:0{index}.000+00:00", "type": "run.start"}) + "\n"
            + json.dumps({"ts": "2026-10-18T10:00:09.000+00:00", "type": "text",
                          "text": f"solved task-{index}"}) + "\n"
            + "KODO-RESULT outcome=completed error=\n"
        )
    (trial / "result.json").write_text(json.dumps({
        "trial_name": trial.name,
        "task_name": f"org/task-{index}",
        "agent_info": {"name": "kodo", "model_info": {"provider": "local", "name": "m"}},
        "config": {"agent": {"kwargs": {"agent": opt("--agent")}}},
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_execution": {
            "started_at": "2026-10-18T10:00:00",
            "finished_at": "2026-10-18T10:00:30",
        },
    }))
if plan.get("ignore_stop"):
    time.sleep(600)
(job / "kodo-summary.md").write_text("# Benchmark summary\n")
sys.exit(130 if stop.is_set() else plan.get("exit_code", 0))
"""


class _Llama:
    """The managed llama-server, recording what a run asked of it."""

    def __init__(self) -> None:
        self.served: list[str] = []
        self.unloaded = 0
        self.serve_gate: asyncio.Event | None = None

    def command(self, entry: str) -> tuple[str, ...]:
        return ("llama-server", "--model", f"{entry}.gguf")

    async def serve(self, entry: str) -> str:
        if self.serve_gate is not None:
            await self.serve_gate.wait()
        self.served.append(entry)
        return "http://127.0.0.1:8042"

    async def unload(self) -> None:
        self.unloaded += 1


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, object]] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(json.loads(data))


class _Bench:
    """One test's server side: a session manager, a gateway and the jobs."""

    def __init__(self, home: Path, platform: str, grace: float) -> None:
        self.home = home
        self.layout = WorkspaceLayout(home / ".kodo")
        self.layout.init()
        self.gateway = LLMGateway(cloud_concurrency=lambda: 2)
        self.llama = _Llama()
        self.manager = SessionManager(
            registry=AgentRegistry(_AGENTS_DIR),
            gateway=self.gateway,
            get_settings=lambda: {"bedrock_region": "eu-west-1"},
            layout=self.layout,
        )
        script = home / "stand_in_kodo_harbor.py"
        script.write_text(_STAND_IN, encoding="utf-8")
        self.record = home / "stand-in-record.json"
        self.plan_path = home / "stand-in-plan.json"
        self.jobs = HarborJobs(
            gateway=self.gateway,
            kodo_dir=self.layout.kodo_dir,
            get_settings=lambda: {"bedrock_region": "eu-west-1"},
            llama=self.llama,
            command=[sys.executable, str(script)],
            platform=platform,
            poll_seconds=0.05,
            grace_seconds=grace,
        )
        self.frames: dict[str, _FakeWS] = {}

    def plan(self, **plan: object) -> None:
        self.plan_path.write_text(
            json.dumps({"record": str(self.record), "rewards": [1.0, 0.0], **plan}),
            encoding="utf-8",
        )

    async def session(self) -> Session:
        session = await self.manager.create("window", greet=False)
        ws = _FakeWS()
        await self.manager.bind_connection(session, Connection(ws))  # type: ignore[arg-type]
        self.frames[session.id] = ws
        return session

    def events(self, session: Session, kind: str) -> list[dict[str, object]]:
        return [
            frame["payload"]  # type: ignore[misc]
            for frame in self.frames[session.id].sent
            if frame.get("kind") == "event"
            and isinstance(frame.get("payload"), dict)
            and frame["payload"].get("type") == kind  # type: ignore[union-attr]
        ]

    def requests(self, session: Session) -> list[dict[str, object]]:
        return [frame for frame in self.frames[session.id].sent if frame.get("kind") == "request"]

    def recorded(self) -> dict[str, object]:
        return json.loads(self.record.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _request(model: str = _MODEL, **overrides: object) -> dict[str, object]:
    return {
        "agent": _AGENT,
        "model": model,
        "suite": AGENT_BASICS_SUITE,
        "attempts": 1,
        "control": False,
        "thinking_level": None,
        **overrides,
    }


@pytest.fixture
async def bench_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Callable[..., _Bench], None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KODO_STAND_IN_PLAN", str(home / "stand-in-plan.json"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    # A submitted prompt reaches model resolution in the engine's worker;
    # fail it fast instead of letting it wait for a key nobody answers.
    async def _no_llm(
        self: WorkflowEngine, capability: str, force_model_key: str | None = None
    ) -> tuple[object, str, object]:
        raise RuntimeError("no LLM in this test")

    monkeypatch.setattr(WorkflowEngine, "_resolve_plugin", _no_llm)
    gguf = home / "bench.gguf"
    gguf.write_bytes(b"GGUF")
    add_local_entry(home / ".kodo", LocalLLMEntry(name=_MODEL, kind="custom_file", path=str(gguf)))
    created: list[_Bench] = []

    def make(platform: str = "darwin", grace: float = 30.0) -> _Bench:
        bench = _Bench(home, platform, grace)
        bench.plan()
        created.append(bench)
        return bench

    yield make
    for bench in created:
        await bench.jobs.stop_all()
        await bench.manager.shutdown()


async def _until(condition: Callable[[], bool], seconds: float = 20.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def _finished(bench: _Bench, session: Session) -> dict[str, object]:
    job = bench.jobs.for_session(session.id)
    assert job is not None
    await asyncio.wait_for(job.wait(), 30)
    (finished,) = bench.events(session, "harbor.finished")
    return finished


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


async def test_a_run_names_and_locks_the_session_and_reports_each_trial(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session = await bench.session()

    assert await bench.jobs.start(session, _request()) is None

    state = session.engine.session
    assert state.interactive is False
    assert state.job is not None and state.job["kind"] == "basics"
    assert bench.events(session, "session.name")[-1]["name"] == (
        f"Basics test: {_AGENT} · {_MODEL}"
    )
    finished = await _finished(bench, session)
    assert finished["outcome"] == "completed"
    assert finished["exit_code"] == 0
    assert finished["summary_markdown"] == "# Benchmark summary\n"
    trials = bench.events(session, "harbor.trial")
    assert [(t["reward"], t["finished"], t["total"]) for t in trials] == [(1.0, 1, 2), (0.0, 2, 2)]
    assert trials[0]["seconds"] == 30.0
    assert session.engine.session.job["status"] == "completed"  # type: ignore[index]


async def test_a_local_run_attaches_kodo_harbor_to_the_managed_llama_server(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory(platform="darwin")
    session = await bench.session()

    assert await bench.jobs.start(session, _request(thinking_level=None, control=True)) is None
    await _finished(bench, session)

    argv = bench.recorded()["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--llama-url") + 1] == "http://127.0.0.1:8042"
    assert argv[argv.index("--control") + 1] == "terminus-2"
    assert "--stop-on-stdin-close" in argv
    assert bench.llama.served == [_MODEL]
    assert bench.gateway.local_holders == frozenset()  # released at the end
    # The sidebar sees the model load, as it does for a chat turn's.
    states = bench.events(session, "llama.state")
    assert states[0] == {"type": "llama.state", "running": False, "starting": True}
    assert states[-1] == {"type": "llama.state", "running": True, "model": _MODEL, "port": 8042}


async def test_linux_unloads_the_managed_server_and_lets_kodo_harbor_start_its_own(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory(platform="linux")
    session = await bench.session()

    assert await bench.jobs.start(session, _request()) is None
    await _finished(bench, session)

    assert "--llama-url" not in bench.recorded()["argv"]  # type: ignore[operator]
    assert bench.llama.unloaded == 1
    assert bench.llama.served == []
    assert bench.gateway.local_holders == frozenset()


async def test_the_run_holds_llama_server_exclusively_while_it_runs(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(trial_seconds=0.5)
    session = await bench.session()

    assert await bench.jobs.start(session, _request()) is None
    await _until(lambda: bench.gateway.local_holders == {f"harbor:{session.id}"})

    assert bench.gateway.exclusive_label == f"Basics test: {_AGENT} · {_MODEL}"
    chat = asyncio.create_task(bench.gateway.acquire_local("chat", bench.llama.command(_MODEL)))
    await asyncio.sleep(0.1)
    assert not chat.done()  # even the same command line waits for the run
    await _finished(bench, session)
    await asyncio.wait_for(chat, 5)
    assert bench.gateway.local_holders == {"chat"}


async def test_a_cloud_run_asks_vs_code_for_the_key_and_passes_it_only_in_the_environment(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(env=["ANTHROPIC_API_KEY"])
    session = await bench.session()
    model = f"anthropic/{get_cloud_registry()['anthropic'][0].model_id}"

    assert await bench.jobs.start(session, _request(model)) is None
    await _until(lambda: bool(bench.requests(session)))
    (request,) = bench.requests(session)
    assert request["payload"] == {"type": "api_key.request", "vendor": "anthropic"}
    session.channel.resolve_response(str(request["id"]), {"api_key": "sk-secret-123"})
    finished = await _finished(bench, session)

    assert finished["outcome"] == "completed"
    record = bench.recorded()
    assert record["env"] == {"ANTHROPIC_API_KEY": "sk-secret-123"}
    assert "sk-secret-123" not in json.dumps(record["argv"])
    log = Path(str(finished["log_path"])).read_text(encoding="utf-8")
    assert "sk-secret-123" not in log
    assert bench.gateway.local_holders == frozenset()  # a cloud run never takes the lease


async def test_a_rejected_key_fails_the_run_with_the_reason(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session = await bench.session()
    model = f"anthropic/{get_cloud_registry()['anthropic'][0].model_id}"

    assert await bench.jobs.start(session, _request(model)) is None
    await _until(lambda: bool(bench.requests(session)))
    (request,) = bench.requests(session)
    session.channel.resolve_response(str(request["id"]), {"error": "user_cancelled"})
    finished = await _finished(bench, session)

    assert finished["outcome"] == "failed"
    assert "user_cancelled" in str(finished["detail"])
    assert not bench.record.exists()  # kodo-harbor never ran


async def test_a_run_that_fails_before_its_job_reports_the_end_of_its_log(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(exit_before_job=2)
    session = await bench.session()

    assert await bench.jobs.start(session, _request()) is None
    finished = await _finished(bench, session)

    assert finished["outcome"] == "failed"
    assert finished["exit_code"] == 2
    assert "Docker is required" in str(finished["detail"])
    assert bench.gateway.local_holders == frozenset()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": "no-such-model"}, "No local model 'no-such-model'"),
        ({"suite": "no-such-suite"}, "no-such-suite"),
        ({"attempts": 0}, "'attempts'"),
        ({"agent": "  "}, "'agent'"),
        ({"thinking_level": "turbo"}, "--thinking-level 'turbo'"),
    ],
)
async def test_a_request_kodo_harbor_would_refuse_changes_nothing(
    bench_factory: Callable[..., _Bench], overrides: dict[str, object], message: str
) -> None:
    bench = bench_factory()
    session = await bench.session()

    refusal = await bench.jobs.start(session, {**_request(), **overrides})

    assert refusal is not None and message in refusal
    assert session.engine.session.interactive is True
    assert session.engine.session.job is None
    assert bench.jobs.for_session(session.id) is None


async def test_a_session_that_already_had_a_prompt_is_refused(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session = await bench.session()
    await session.engine.handle_prompt_submit("hello", "r1")

    refusal = await bench.jobs.start(session, _request())

    assert refusal == "a benchmark can only start in a brand-new session"
    assert bench.jobs.for_session(session.id) is None


async def test_one_benchmark_runs_at_a_time(bench_factory: Callable[..., _Bench]) -> None:
    bench = bench_factory()
    bench.plan(trial_seconds=0.5)
    first = await bench.session()
    second = await bench.session()

    assert await bench.jobs.start(first, _request()) is None
    refusal = await bench.jobs.start(second, _request())

    assert refusal is not None and f"Basics test: {_AGENT} · {_MODEL}" in refusal
    assert second.engine.session.interactive is True
    await _finished(bench, first)
    assert await bench.jobs.start(second, _request()) is None  # free again once it ended


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def test_stop_winds_the_run_down_and_keeps_the_finished_trials(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(rewards=[1.0, 1.0, 1.0, 1.0], trial_seconds=0.4)
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _until(lambda: bool(bench.events(session, "harbor.trial")))

    job = bench.jobs.for_session(session.id)
    assert job is not None
    job.stop()
    finished = await _finished(bench, session)

    assert finished["outcome"] == "stopped"
    assert session.engine.session.job["stopped_by"] == "user"  # type: ignore[index]
    assert finished["exit_code"] == 130
    assert finished["summary_markdown"] == "# Benchmark summary\n"
    assert 1 <= len(bench.events(session, "harbor.trial")) < 4
    assert bench.gateway.local_holders == frozenset()


async def test_a_run_that_ignores_stop_is_killed_after_the_grace_period(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory(grace=0.3)
    bench.plan(rewards=[1.0], ignore_stop=True)
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _until(lambda: bool(bench.events(session, "harbor.trial")))

    job = bench.jobs.for_session(session.id)
    assert job is not None
    job.stop()
    finished = await _finished(bench, session)

    assert finished["outcome"] == "stopped"
    assert finished["exit_code"] != 0
    assert bench.gateway.local_holders == frozenset()


async def test_stop_while_waiting_for_llama_server_never_starts_kodo_harbor(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    await bench.gateway.acquire_local("a-chat-turn", ("llama-server", "--model", "other.gguf"))
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    assert session.engine.session.job["status"] == "waiting"  # type: ignore[index]

    job = bench.jobs.for_session(session.id)
    assert job is not None
    job.stop()
    finished = await _finished(bench, session)

    assert finished["outcome"] == "stopped"
    assert session.engine.session.job["stopped_by"] == "user"  # type: ignore[index]
    assert not bench.record.exists()
    assert bench.gateway.local_holders == {"a-chat-turn"}  # its hold never taken


async def test_stop_all_stops_every_run_for_shutdown(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(rewards=[1.0, 1.0, 1.0], trial_seconds=0.4)
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _until(lambda: session.engine.session.job["status"] == "running")  # type: ignore[index]

    await asyncio.wait_for(bench.jobs.stop_all(), 30)

    assert bench.jobs.running is None
    assert bench.events(session, "harbor.finished")[0]["outcome"] == "stopped"
    assert session.engine.session.job["stopped_by"] == "server"  # type: ignore[index]


async def test_discarding_a_session_kills_its_run(bench_factory: Callable[..., _Bench]) -> None:
    bench = bench_factory()
    bench.plan(rewards=[1.0], ignore_stop=True)
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _until(lambda: bool(bench.events(session, "harbor.trial")))

    await asyncio.wait_for(bench.jobs.discard(session.id), 10)

    assert bench.jobs.for_session(session.id) is None
    assert bench.jobs.running is None
    assert bench.gateway.local_holders == frozenset()


# ---------------------------------------------------------------------------
# Persistence: reopening, and a server that stopped mid-run
# ---------------------------------------------------------------------------


async def test_a_reopened_session_replays_the_run_and_stays_locked(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _finished(bench, session)
    await bench.manager.shutdown()

    reopened = SessionManager(
        registry=AgentRegistry(_AGENTS_DIR),
        gateway=bench.gateway,
        get_settings=dict,
        layout=bench.layout,
    )
    again = await reopened.open(session.id, "another-window")
    assert again is not None
    try:
        assert again.engine.session.interactive is False
        assert again.engine.session.job is not None
        assert again.engine.session.job["status"] == "completed"
        assert "process" not in again.engine.session.job
        entries = (await again.engine.full_history())["entries"]
        assert isinstance(entries, list)
        kinds = [entry["type"] for entry in entries]
        assert kinds == ["harbor_job", "harbor_trial", "harbor_trial", "harbor_finished"]
        assert entries[-1]["outcome"] == "completed"
        assert entries[1]["reward"] == 1.0
    finally:
        await reopened.shutdown()


def _payload(frame: dict[str, object]) -> dict[str, object]:
    payload = frame["payload"]
    assert isinstance(payload, dict)
    return payload


def _chunk_events(chunk: dict[str, object]) -> list[dict[str, object]]:
    events = chunk["events"]
    assert isinstance(events, list)
    return events


async def test_the_agent_activity_of_each_trial_is_pushed_while_a_window_is_attached(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(activity=True)
    session = await bench.session()
    assert await bench.jobs.start(session, _request()) is None
    await _finished(bench, session)

    activity = bench.events(session, "harbor.activity")
    by_trial = {str(chunk["trial"]): chunk for chunk in activity}
    assert sorted(by_trial) == ["task-0__abc", "task-1__abc"]
    chunk = by_trial["task-1__abc"]
    assert chunk["first"] == 0
    assert chunk["step"] is None
    assert chunk["started"] == "2026-10-18T10:00:01.000+00:00"
    assert [e["type"] for e in _chunk_events(chunk)] == ["run.start", "text"]
    # Each event once, though the job directory was read many times.
    assert sum(len(_chunk_events(c)) for c in activity) == 4
    # Never persisted: the job directory is the record.
    entries = (await session.engine.full_history())["entries"]
    assert isinstance(entries, list)
    assert [entry["type"] for entry in entries] == [
        "harbor_job",
        "harbor_trial",
        "harbor_trial",
        "harbor_finished",
    ]


async def test_a_detached_run_sends_no_activity_and_a_window_gets_it_all_on_attach(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    bench.plan(activity=True)
    session = await bench.session()
    session.channel.detach()
    assert await bench.jobs.start(session, _request()) is None
    job = bench.jobs.for_session(session.id)
    assert job is not None
    await asyncio.wait_for(job.wait(), 30)
    ws = _FakeWS()
    await session.channel.attach(Connection(ws))  # type: ignore[arg-type]
    await session.channel.replay_backlog()
    buffered = [_payload(frame)["type"] for frame in ws.sent]
    assert "harbor.finished" in buffered
    assert "harbor.activity" not in buffered

    ws.sent.clear()
    await push_trial_activity(session)
    chunks = [_payload(frame) for frame in ws.sent]
    assert [(c["trial"], c["first"], len(_chunk_events(c))) for c in chunks] == [
        ("task-0__abc", 0, 2),
        ("task-1__abc", 0, 2),
    ]


async def test_push_trial_activity_sends_nothing_for_an_ordinary_session(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session = await bench.session()
    before = len(bench.frames[session.id].sent)
    await push_trial_activity(session)
    assert len(bench.frames[session.id].sent) == before


async def test_a_run_the_server_never_saw_end_is_marked_interrupted(
    bench_factory: Callable[..., _Bench],
) -> None:
    bench = bench_factory()
    session_dir = bench.layout.sessions_dir / "2026-10-18-crashed"
    store = TransientStore(bench.layout.kodo_dir)
    store.attach_session(session_dir.name, False)
    store.update(
        harbor_job={
            "status": "running",
            "job_dir": str(bench.home / "jobs" / "gone"),
            "log_path": str(session_dir / "harbor-output.log"),
            # A pid that is ours but started at another time: never killed.
            "process": {"pid": 1, "created": 0.0},
        }
    )

    assert bench.jobs.recover(bench.layout.sessions_dir) == [session_dir.name]

    reread = TransientStore(bench.layout.kodo_dir)
    reread.attach_session(session_dir.name, True)
    assert reread.harbor_job is not None
    assert reread.harbor_job["status"] == "interrupted"
    assert "process" not in reread.harbor_job
    (marker,) = [line for line in reread.read_session_lines() if line.get("type")]
    assert marker["type"] == "harbor_finished"
    assert marker["outcome"] == "interrupted"
    # A second start finds nothing left to mark.
    assert bench.jobs.recover(bench.layout.sessions_dir) == []
