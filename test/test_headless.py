"""``kodo-headless`` (doc/HEADLESS.md): the isolated home, the WS client against a
scripted fake server, and the run's startup-failure reporting.

The client is driven by an in-process aiohttp WebSocket server that speaks
just enough of the wire contract to walk one turn: it answers the setup
requests, streams thinking and text, reports tool calls (one refused by the
sandbox), usage, a sub-session, fires every server→client request kodo
defines, and rests the phase. The sandbox itself is tested end to end in
``test_server_headless.py``; this file tests what the client makes of it.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

import kodo.transport as transport
from kodo.common import Envelope
from kodo.headless import (
    HEADLESS_ANSWERED_REQUESTS,
    EventSink,
    HeadlessClient,
    HeadlessOptions,
    HeadlessRun,
    RunOutcome,
    build_headless_home,
)

# ---------------------------------------------------------------------------
# Isolated home
# ---------------------------------------------------------------------------


@pytest.fixture
def template(tmp_path: Path) -> Path:
    kodo = tmp_path / "real" / ".kodo"
    (kodo / "etc").mkdir(parents=True)
    (kodo / "bin").mkdir()
    (kodo / "agents").mkdir()
    (kodo / "skills").mkdir()
    (kodo / "checkpoints").mkdir()
    (kodo / "checkpoints" / "big.pack").write_bytes(b"\0" * (10 * 1024 * 1024))
    (kodo / "llama.cpp").mkdir()
    (kodo / "some-future-entry").mkdir()
    (kodo / "etc" / "hf_tokens.json").write_text('{"secret": 1}', encoding="utf-8")
    (kodo / "etc" / "settings.json").write_text(
        json.dumps({"mode": "cloud", "models": {"local": "old", "cloud": {"x": 1}}}),
        encoding="utf-8",
    )
    (kodo / "etc" / "local-llm-registry.json").write_text('{"entries": []}', encoding="utf-8")
    return kodo


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_home_is_an_allowlist(tmp_path: Path, template: Path) -> None:
    before = _snapshot(template)
    kodo = build_headless_home(tmp_path / "iso", model="m-q4", template_kodo_dir=template)

    assert sorted(p.name for p in kodo.iterdir()) == ["agents", "bin", "etc", "skills"]
    assert sorted(p.name for p in (kodo / "etc").iterdir()) == [
        "local-llm-registry.json",
        "settings.json",
    ]
    for name in ("bin", "agents", "skills"):
        assert (kodo / name).is_symlink(), name
    settings = json.loads((kodo / "etc" / "settings.json").read_text(encoding="utf-8"))
    assert settings["mode"] == "local"
    assert settings["models"] == {"local": "m-q4", "cloud": {"x": 1}}
    assert _snapshot(template) == before


def test_registry_file_wins_and_is_copied_not_linked(tmp_path: Path, template: Path) -> None:
    mounted = tmp_path / "mounted-registry.json"
    mounted.write_text('{"entries": [{"name": "host"}]}', encoding="utf-8")
    kodo = build_headless_home(
        tmp_path / "iso", model="m", template_kodo_dir=template, registry_file=mounted
    )
    copy = kodo / "etc" / "local-llm-registry.json"
    assert not copy.is_symlink()
    assert json.loads(copy.read_text(encoding="utf-8"))["entries"][0]["name"] == "host"


def test_home_without_template_is_minimal(tmp_path: Path) -> None:
    kodo = build_headless_home(tmp_path / "iso", model="m", template_kodo_dir=None)
    settings = json.loads((kodo / "etc" / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"models": {"local": "m"}, "mode": "local"}


# ---------------------------------------------------------------------------
# Client coverage of the wire contract
# ---------------------------------------------------------------------------


def test_every_server_request_type_has_a_deterministic_answer() -> None:
    defined = {
        str(getattr(transport, name))
        for name in dir(transport)
        if name.startswith("SREQ_") and isinstance(getattr(transport, name), str)
    }
    assert defined - HEADLESS_ANSWERED_REQUESTS == set()


_Handler = Callable[[web.WebSocketResponse, dict[str, object], str], Awaitable[None]]


class _FakeServer:
    """A scripted kodo server: request type → handler, plus recorded frames."""

    def __init__(self, on_prompt: _Handler) -> None:
        self.received: list[dict[str, object]] = []
        self.answers: dict[str, dict[str, object]] = {}
        self.__on_prompt = on_prompt

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            env = Envelope.from_json(str(msg.data))
            if env.kind == "response":
                self.answers[str(env.correlation_id)] = env.payload
                continue
            self.received.append(env.payload)
            kind = str(env.payload.get("type"))
            reply: dict[str, object] = {"type": f"{kind}.ack"}
            if kind == "hello":
                reply = {"type": "hello.ack", "session_id": "s-1", "state": {"phase": "intake"}}
            elif kind == "top_agents.list":
                reply = {"type": "top_agents.list.ack", "agents": [{"name": "kodo_problem_solver"}]}
            await ws.send_str(Envelope.make_response(env.id, reply).to_json())
            if kind == "prompt.submit":
                asyncio.create_task(self.__on_prompt(ws, env.payload, env.id))
        return ws


async def _event(ws: web.WebSocketResponse, **payload: object) -> None:
    await ws.send_str(Envelope.make_event(str(payload.pop("type")), payload).to_json())


async def _stream(ws: web.WebSocketResponse, kind: str, stream_id: str, text: str) -> None:
    payload_type = "agent.thinking" if kind == "thinking_chunk" else "agent.tokens"
    for part in (text[: len(text) // 2], text[len(text) // 2 :]):
        env = Envelope(
            kind=kind, correlation_id=stream_id, payload={"type": payload_type, "text": part}
        )
        await ws.send_str(env.to_json())
    await ws.send_str(Envelope(kind="stream_end", correlation_id=stream_id, payload={}).to_json())


@pytest.fixture
async def fake() -> AsyncIterator[Callable[[_Handler], Awaitable[tuple[_FakeServer, str]]]]:
    servers: list[TestServer] = []

    async def _start(on_prompt: _Handler) -> tuple[_FakeServer, str]:
        fake_server = _FakeServer(on_prompt)
        app = web.Application()
        app.router.add_get("/ws", fake_server.handle)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return fake_server, f"ws://127.0.0.1:{server.port}/ws"

    yield _start
    for server in servers:
        await server.close()


def _events(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]


async def test_client_walks_a_turn_and_reports_everything(
    tmp_path: Path,
    fake: Callable[[_Handler], Awaitable[tuple[_FakeServer, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied_doc = tmp_path / "tu_1.md"
    denied_doc.write_text(
        "## Input\n\ncommand: git commit -m x\n\n## Output\n\n"
        "error: Blocked by the headless sandbox: 'git commit' can modify the repository\n",
        encoding="utf-8",
    )
    ok_doc = tmp_path / "tu_2.md"
    ok_doc.write_text("## Input\n\npath: hello.py\n\n## Output\n\nstatus: created\n", "utf-8")

    async def on_prompt(ws: web.WebSocketResponse, _payload: dict[str, object], _id: str) -> None:
        await _event(ws, type="state", phase="running")
        await _event(ws, type="llm.turn_start", agent="problem_solver", model="fake-q4")
        await _stream(ws, "thinking_chunk", "st1", "let me think")
        await _event(ws, type="agent.tool_call_prep", tool_name="run_command", tool_call_id="tu_1")
        await _event(ws, type="agent.tool_call_detail", tool_call_id="tu_1", file=str(denied_doc))
        await _event(
            ws, type="subsession.started", subsession_id="sub-1", agent="planner", task="plan it"
        )
        await _event(ws, type="llm.turn_start", agent="planner", model="fake-q4")
        await _stream(ws, "stream_chunk", "st2", "the plan")
        await _event(
            ws,
            type="usage.update",
            cumulative_input_tokens=30,
            cumulative_input_tokens_uncached=30,
            cumulative_output_tokens=7,
            cumulative_usd=0.0,
            last_call_tokens={"input": 10, "output": 3, "cache_read": 0, "cache_write": 0},
            model="fake-q4",
            agent="planner",
            usd_cost=0.0,
        )
        await _event(ws, type="subsession.ended", subsession_id="sub-1", agent="planner")
        await _event(ws, type="agent.tool_call_prep", tool_name="create_file", tool_call_id="tu_2")
        await _event(ws, type="agent.tool_call_detail", tool_call_id="tu_2", file=str(ok_doc))
        for request_type in sorted(HEADLESS_ANSWERED_REQUESTS):
            payload: dict[str, object] = {"type": request_type}
            if request_type == "prompt.question":
                payload["questions"] = [{"question": "Which?", "options": ["A", "B"]}]
            if request_type == "api_key.request":
                payload["vendor"] = "fakevendor"
            await ws.send_str(Envelope(kind="request", id=request_type, payload=payload).to_json())
        await _stream(ws, "stream_chunk", "st3", "all done")
        await _event(ws, type="state", phase="awaiting_user")

    fake_server, url = await fake(on_prompt)
    buffer = io.StringIO()
    client = HeadlessClient(url, EventSink("jsonl", buffer), tmp_path)
    monkeypatch.setenv("FAKEVENDOR_API_KEY", "k-123")
    try:
        await client.connect()
        await client.hello()
        client.begin_turn()
        await client.request("prompt.submit", {"text": "go"})
        phase = await client.wait_turn_end(timeout=10, settle_seconds=0.2)
    finally:
        await client.close()

    assert phase == "awaiting_user"
    assert client.assistant_text == "all done"  # the planner's text is a sub-session's
    assert (client.tool_calls, client.tool_denials, client.questions_asked) == (2, 1, 1)
    assert client.per_agent == {
        "planner": {"calls": 1, "input_tokens": 10, "output_tokens": 3, "usd": 0.0}
    }
    assert client.cumulative["cumulative_input_tokens"] == 30

    events = _events(buffer)
    kinds = [e["type"] for e in events]
    for expected in (
        "llm.turn",
        "thinking",
        "tool.start",
        "tool.call",
        "tool.denied",
        "subsession.start",
        "subsession.end",
        "usage",
        "question",
        "text",
    ):
        assert expected in kinds, expected
    thinking = next(e for e in events if e["type"] == "thinking")
    assert thinking["text"] == "let me think"  # coalesced from two chunks
    plan = next(e for e in events if e["type"] == "text" and e["text"] == "the plan")
    assert (plan["agent"], plan["subsession_id"]) == ("planner", "sub-1")
    denied_call = next(e for e in events if e["type"] == "tool.call" and e["tool"] == "run_command")
    assert "git commit" in str(denied_call["document"])

    answers = fake_server.answers
    assert answers["prompt.permission"]["action"] == "deny"
    assert answers["prompt.approval"]["action"] == "agree"
    assert answers["prompt.edit_review"]["action"] == "approve"
    assert answers["prompt.question"]["answers"][0]["selected"] == ["A"]  # type: ignore[index]
    assert answers["api_key.request"] == {"api_key": "k-123"}
    assert answers["workspace.confirm_folder"]["attached"] is False
    setup = [r["type"] for r in fake_server.received]
    assert setup[:2] == ["hello", "prompt.submit"]


async def test_wait_turn_end_times_out_and_stop_reaches_the_server(
    tmp_path: Path, fake: Callable[[_Handler], Awaitable[tuple[_FakeServer, str]]]
) -> None:
    async def on_prompt(ws: web.WebSocketResponse, _payload: dict[str, object], _id: str) -> None:
        await _event(ws, type="state", phase="running")  # never rests

    fake_server, url = await fake(on_prompt)
    client = HeadlessClient(url, EventSink("jsonl", io.StringIO()), tmp_path)
    await client.connect()
    await client.hello()
    client.begin_turn()
    await client.request("prompt.submit", {"text": "go"})
    with pytest.raises(TimeoutError):
        await client.wait_turn_end(timeout=0.5, settle_seconds=0.1)
    await client.stop()
    await client.close()
    assert "stop" in [r["type"] for r in fake_server.received]


async def test_error_after_the_last_response_is_the_turn_error(
    tmp_path: Path, fake: Callable[[_Handler], Awaitable[tuple[_FakeServer, str]]]
) -> None:
    async def on_prompt(ws: web.WebSocketResponse, _payload: dict[str, object], _id: str) -> None:
        await _event(ws, type="state", phase="running")
        await _event(ws, type="error", code="runtime_error", message="rate limit", recoverable=True)
        await _event(ws, type="state", phase="awaiting_user")

    _, url = await fake(on_prompt)
    client = HeadlessClient(url, EventSink("text", io.StringIO()), tmp_path)
    await client.connect()
    await client.hello()
    client.begin_turn()
    await client.request("prompt.submit", {"text": "go"})
    await client.wait_turn_end(timeout=5, settle_seconds=0.1)
    await client.close()
    assert client.turn_error == "rate limit"


# ---------------------------------------------------------------------------
# The run: startup failures are reported, and nothing is left behind
# ---------------------------------------------------------------------------


async def test_run_reports_startup_error_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    sandbox = tmp_path / "proj"
    sandbox.mkdir()
    result_path = tmp_path / "result.json"
    buffer = io.StringIO()
    options = HeadlessOptions(
        prompt="do it", model="no-such-model", cwd=sandbox, result_path=result_path
    )

    result = await HeadlessRun(options, EventSink("jsonl", buffer)).run()

    assert result.outcome == RunOutcome.STARTUP_ERROR.value
    assert RunOutcome(result.outcome).exit_code == 4
    assert json.loads(result_path.read_text(encoding="utf-8"))["outcome"] == "startup_error"
    lines = buffer.getvalue().splitlines()
    assert lines[-1].startswith("KODO-RESULT outcome=startup_error")
    assert json.loads(lines[-2])["type"] == "run.result"
    assert not (sandbox / ".kodo").exists()
    started = json.loads(lines[0])
    assert started["type"] == "run.start"
    assert not Path(str(started["home"])).exists()  # the temporary isolated home is gone


def test_cli_rejects_an_empty_prompt(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "kodo.headless", "--prompt", "  ", "--model", "m"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == RunOutcome.STARTUP_ERROR.exit_code
    assert "empty" in completed.stderr


async def test_one_stream_id_per_turn_is_cut_into_thinking_and_text_segments(
    tmp_path: Path, fake: Callable[[_Handler], Awaitable[tuple[_FakeServer, str]]]
) -> None:
    """The real server keeps one stream id for a whole agent turn: every LLM
    round, thinking and text interleaved, one `stream_end` at the very end."""

    async def chunk(ws: web.WebSocketResponse, kind: str, text: str) -> None:
        payload_type = "agent.thinking" if kind == "thinking_chunk" else "agent.tokens"
        env = Envelope(
            kind=kind, correlation_id="turn", payload={"type": payload_type, "text": text}
        )
        await ws.send_str(env.to_json())

    async def on_prompt(ws: web.WebSocketResponse, _payload: dict[str, object], _id: str) -> None:
        await _event(ws, type="state", phase="running")
        await _event(ws, type="llm.turn_start", agent="problem_solver", model="m")
        await chunk(ws, "thinking_chunk", "plan ")
        await chunk(ws, "thinking_chunk", "the work")
        await _event(ws, type="agent.tool_call_prep", tool_name="read_file", tool_call_id="t1")
        await _event(ws, type="llm.turn_start", agent="problem_solver", model="m")
        await chunk(ws, "thinking_chunk", "it worked")
        await chunk(ws, "stream_chunk", "Here is ")
        await chunk(ws, "stream_chunk", "the answer.")
        await ws.send_str(Envelope(kind="stream_end", correlation_id="turn", payload={}).to_json())
        await _event(ws, type="state", phase="awaiting_user")

    _, url = await fake(on_prompt)
    buffer = io.StringIO()
    client = HeadlessClient(url, EventSink("jsonl", buffer), tmp_path)
    await client.connect()
    await client.hello()
    client.begin_turn()
    await client.request("prompt.submit", {"text": "go"})
    await client.wait_turn_end(timeout=5, settle_seconds=0.1)
    await client.close()

    spoken = [(e["type"], e["text"]) for e in _events(buffer) if e["type"] in ("thinking", "text")]
    assert spoken == [
        ("thinking", "plan the work"),
        ("thinking", "it worked"),
        ("text", "Here is the answer."),
    ]
    assert client.assistant_text == "Here is the answer."
