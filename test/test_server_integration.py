"""Integration tests: start the singleton kodo server and talk to it over WS.

Fixtures start a real aiohttp server (in-process, random port) with ``HOME``
redirected to a temp dir so the real ``~/.kodo`` is never touched.  Every frame
except ``hello`` carries a ``session_id``; ``hello`` mints (or resumes) one.
No LLM calls are made — ``_temp_home`` stubs ``WorkflowEngine._resolve_plugin``
to fail fast, since a queued prompt is processed by the background worker
regardless of workflow mode (both entry agents share one code path) and would
otherwise reach a real model resolution / API-key round-trip.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.agents import AgentRegistry
from kodo.common import Envelope
from kodo.runtime import WorkflowEngine
from kodo.runtime._engine import _titling as _titling_module
from kodo.server import Config, create_app
from kodo.server import _app as _app_module

_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "agents"

_RECV_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # _start_background fire-and-forgets kodo.titling.start_titling on
    # startup when llama.cpp is installed (doc/INTERNALS.md §10c); stubbed
    # here so server boot stays fully offline/deterministic like the rest of
    # this file (see module docstring) instead of racing a real model
    # download/llama-server spin-up every test.
    async def _no_op_start_titling(kodo_dir: Path) -> None:
        return None

    monkeypatch.setattr(_app_module, "start_titling", _no_op_start_titling)

    # SessionTitler awaits kodo.titling.generate_title fire-and-forget on the
    # first prompt of every session (runtime/_engine/_titling.py) — stubbed
    # here too, otherwise any test that submits a real prompt triggers a real
    # HTTP call against a titler llama-server that was never started.
    async def _no_op_generate_title(text: str) -> None:
        return None

    monkeypatch.setattr(_titling_module, "generate_title", _no_op_generate_title)

    # A queued prompt.submit is processed by the background worker
    # (kodo/runtime/_engine/_worker.py) regardless of workflow mode — since
    # the 2026-07 multi-project rework, Guided mode no longer short-circuits
    # before a bound project exists, so it reaches _resolve_plugin exactly
    # like Problem Solver always has. With no local/cloud model configured in
    # this temp HOME, that would fall through to KeyBroker.get_key, which
    # blocks forever awaiting a client response this test harness never sends
    # (kodo/server/_key_broker.py). Fail fast instead, preserving this
    # module's "no LLM calls are made" invariant explicitly rather than by
    # accident.
    async def _no_op_resolve_plugin(
        self: WorkflowEngine, capability: str, force_model_key: str | None = None
    ) -> tuple[object, str, object]:
        raise RuntimeError("no LLM configured in this offline integration test")

    monkeypatch.setattr(WorkflowEngine, "_resolve_plugin", _no_op_resolve_plugin)
    return tmp_path


@pytest.fixture
async def server() -> AsyncGenerator[TestServer, None]:
    app = create_app(Config())
    srv = TestServer(app)
    await srv.start_server()
    yield srv
    await srv.close()


@pytest.fixture
async def ws(server: TestServer) -> AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
    session = aiohttp.ClientSession()
    conn = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    yield conn
    await conn.close()
    await session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _recv(ws: aiohttp.ClientWebSocketResponse, timeout: float = _RECV_TIMEOUT) -> Envelope:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    assert msg.type == aiohttp.WSMsgType.TEXT, f"Expected TEXT frame, got {msg.type}"
    return Envelope.from_json(str(msg.data))


async def _recv_response(
    ws: aiohttp.ClientWebSocketResponse, correlation_id: str, timeout: float = _RECV_TIMEOUT
) -> Envelope:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"No response for id={correlation_id!r} within {timeout}s")
        env = await _recv(ws, timeout=remaining)
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env


def _seed_custom_hf(name: str) -> Envelope:
    """Seed a ``custom_hf`` entry *name* and return the request that re-reads the registry.

    No message creates one any more (``local_llm.add_huggingface`` was removed
    for the Model Importer agent), but entries added before then still load,
    install and update — which is what the tests using this exercise. The
    returned ``local_llm.registry_get`` is answered with the same
    ``local_llm.registry_state`` the add used to reply with.
    """
    from kodo.llms import LocalLLMEntry
    from kodo.llms.local_registry import add_local_entry
    from kodo.project import kodo_user_dir

    add_local_entry(
        kodo_user_dir(),
        LocalLLMEntry(
            name=name,
            kind="custom_hf",
            description="",
            repo_id=f"acme/{name}",
            filename="model.gguf",
        ),
    )
    return _make_request("local_llm.registry_get")


def _make_request(msg_type: str, *, session_id: str | None = None, **payload: object) -> Envelope:
    body: dict[str, object] = {"type": msg_type, **payload}
    if session_id is not None:
        body["session_id"] = session_id
    return Envelope(kind="request", payload=body)


def _make_response(correlation_id: str, **payload: object) -> Envelope:
    body: dict[str, object] = payload
    return Envelope(kind="response", id="", correlation_id=correlation_id, payload=body)


async def _drain_hf_token_request(
    ws: aiohttp.ClientWebSocketResponse, drain_timeout: float = 1.0
) -> None:
    """Auto-respond to ``hf_token.request`` frames from the server so tests
    that don't simulate a full extension don't hang waiting for a response.

    Drains the socket for *drain_timeout* seconds, responding to every
    ``hf_token.request`` with an empty token. The first non-token-request
    frame is buffered for the next ``_recv_with_drain`` call.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + drain_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except TimeoutError:
            break
        assert msg.type == aiohttp.WSMsgType.TEXT
        env = Envelope.from_json(str(msg.data))
        if env.kind == "request" and env.payload.get("type") == "hf_token.request":
            resp = _make_response(env.id, hf_token="")
            await ws.send_str(resp.to_json())
            continue
        # Not a token request — buffer for the caller
        if not hasattr(ws, "_buffered_frame"):
            ws._buffered_frame = []  # type: ignore[attr-defined]
        ws._buffered_frame.append(env)  # type: ignore[attr-defined]
        return


async def _recv_with_drain(
    ws: aiohttp.ClientWebSocketResponse, timeout: float = _RECV_TIMEOUT
) -> Envelope:
    """Like ``_recv`` but auto-responds to any ``hf_token.request`` frames
    before returning the next expected frame."""
    # Check for buffered frames first
    if hasattr(ws, "_buffered_frame") and ws._buffered_frame:  # type: ignore[attr-defined]
        return ws._buffered_frame.pop(0)  # type: ignore[attr-defined]

    # Drain and auto-respond to token requests
    await _drain_hf_token_request(ws)

    # Check for buffered frames again
    if hasattr(ws, "_buffered_frame") and ws._buffered_frame:  # type: ignore[attr-defined]
        return ws._buffered_frame.pop(0)  # type: ignore[attr-defined]

    return await _recv(ws, timeout=timeout)


async def _hello(
    ws: aiohttp.ClientWebSocketResponse,
    *,
    session_id: str | None = None,
    window_id: str = "w1",
) -> Envelope:
    req = _make_request("hello", client="vsix", version="0.2.0", window_id=window_id)
    if session_id is not None:
        req.payload["session_id"] = session_id
    await ws.send_str(req.to_json())
    return await _recv_response(ws, req.id)


async def _open_session(ws: aiohttp.ClientWebSocketResponse) -> str:
    resp = await _hello(ws)
    return str(resp.payload["session_id"])


async def _control_hello(ws: aiohttp.ClientWebSocketResponse) -> Envelope:
    """A role=control hello — mints no session, so (unlike a plain ``hello``)
    no ``state``/``session.name`` events follow the ack. Safe to use mid-test
    as a side-effect-free ``local_registry`` snapshot without leaving stray
    frames in the socket for the next ``_recv`` to trip over."""
    req = _make_request("hello", client="vsix", window_id="control-snapshot", role="control")
    await ws.send_str(req.to_json())
    return await _recv_response(ws, req.id)


# ---------------------------------------------------------------------------
# hello — create / resume / ownership
# ---------------------------------------------------------------------------


async def test_hello_returns_version_and_session_id(ws: aiohttp.ClientWebSocketResponse) -> None:
    resp = await _hello(ws)
    assert resp.payload["type"] == "hello.ack"
    assert resp.payload["server_version"] == "0.2.0b1"
    assert resp.payload["session_id"]


async def test_control_hello_creates_no_session(server: TestServer) -> None:
    """A role=control connection (the sidebar) handshakes without a session."""
    session = aiohttp.ClientSession()
    c = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        req = _make_request("hello", client="vsix", window_id="wc", role="control")
        await c.send_str(req.to_json())
        resp = await _recv_response(c, req.id)
        assert resp.payload["type"] == "hello.ack"
        assert resp.payload["role"] == "control"
        assert "session_id" not in resp.payload  # no session was minted
        assert "cloud_registry" in resp.payload  # window-global llama/model snapshot
        assert "local_registry" in resp.payload
        # The control connection did not create any session.
        list_req = _make_request("session.list")
        await c.send_str(list_req.to_json())
        list_resp = await _recv_response(c, list_req.id)
        assert list_resp.payload["sessions"] == []
    finally:
        await c.close()
        await session.close()


async def test_hello_ack_embeds_state_snapshot(ws: aiohttp.ClientWebSocketResponse) -> None:
    resp = await _hello(ws)
    state = resp.payload["state"]
    assert isinstance(state, dict) and "phase" in state


async def test_hello_ack_no_longer_carries_the_agent_catalog(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """Moved to ``top_agents.list`` (§7.4g) — the picker now fetches it on
    demand, each time its popup opens, rather than once at connect."""
    resp = await _hello(ws)
    assert "agents" not in resp.payload
    assert "default_agent" not in resp.payload
    # The session's starting agent is still resolved server-side at creation —
    # only the client-facing catalog moved, not the default-resolution logic.
    assert resp.payload["state"]["top_agent"] == AgentRegistry(_AGENTS_DIR).default_top_agent()


async def test_top_agents_list_returns_the_full_catalog(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """The picker is rendered from this, so the client hardcodes no agent names.

    Every registered *selectable* agent must be here, with everything a row
    needs — adding one server-side is meant to need no client change at all.
    """
    sid = str((await _hello(ws)).payload["session_id"])
    registry = AgentRegistry(_AGENTS_DIR)
    expected = [a for a in registry.top_agents() if a.selectable]

    req = _make_request("top_agents.list", session_id=sid)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "top_agents.list.ack"

    agents = resp.payload["agents"]
    assert isinstance(agents, list)
    assert [a["name"] for a in agents] == [a.name for a in expected]
    for row, agent in zip(agents, expected, strict=True):
        assert row == {
            "name": agent.name,
            "label": agent.label,
            "description": agent.description,
            "rank": agent.rank,
        }
    # A brand-new session's starting agent, which the server resolves on its
    # own — the client sends `agent.set` with an empty name and adopts
    # whatever the follow-up `state` event echoes back.
    assert resp.payload["default_agent"] == registry.default_top_agent()


# The one shipped non-selectable agent is also non-interactive, which agent.set
# refuses (§7.4h), so these tests install a plain `selectable: false` user agent
# (doc/USER_AGENTS.md) rather than borrowing a real one.
_HIDDEN_AGENT = "hidden_agent"


async def _install_hidden_agent(ws: aiohttp.ClientWebSocketResponse, home: Path) -> None:
    """Install a ``selectable: false`` user agent and reload the registry."""
    bundle = home / ".kodo" / "agents" / _HIDDEN_AGENT
    bundle.mkdir(parents=True)
    (bundle / f"agent_{_HIDDEN_AGENT}.md").write_text(
        f"---\nname: {_HIDDEN_AGENT}\nversion: 1.0.0\n---\nYou are an agent.\n",
        encoding="utf-8",
    )
    (bundle / f"{_HIDDEN_AGENT}.json").write_text(
        json.dumps(
            {
                "name": _HIDDEN_AGENT,
                "description": "Driven over the wire only.",
                "rank": 50,
                "selectable": False,
            }
        ),
        encoding="utf-8",
    )
    req = _make_request("agents.reload")
    await ws.send_str(req.to_json())
    reloaded = await _recv_response(ws, req.id)
    assert reloaded.payload["ok"] is True, reloaded.payload


async def test_top_agents_list_omits_non_selectable_agents(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    """A non-selectable agent is registered and reachable, but never offered to a user.

    It is absent from the catalog and still accepted by ``agent.set`` — the
    whole point of the ``selectable`` flag.
    """
    sid = str((await _hello(ws)).payload["session_id"])
    await _install_hidden_agent(ws, _temp_home)

    req = _make_request("top_agents.list", session_id=sid)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    names = {a["name"] for a in resp.payload["agents"]}
    assert names, "the catalog should still list the selectable agents"
    assert _HIDDEN_AGENT not in names


async def test_agent_set_accepts_a_non_selectable_agent(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    sid = str((await _hello(ws)).payload["session_id"])
    await _install_hidden_agent(ws, _temp_home)
    req = _make_request("agent.set", session_id=sid, name=_HIDDEN_AGENT)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "agent.accepted"


# ---------------------------------------------------------------------------
# agent.run — a session started on a non-interactive agent, then locked
# (doc/WS_PROTOCOL.md §7.4h)
# ---------------------------------------------------------------------------

#: Read off the shipped configs rather than named, so the tests keep exercising
#: whichever agent agent.run can start.
_RUN_ONLY_AGENT = next(a.name for a in AgentRegistry(_AGENTS_DIR).top_agents() if not a.interactive)


async def _agent_run(
    ws: aiohttp.ClientWebSocketResponse, sid: str, name: str = _RUN_ONLY_AGENT
) -> tuple[Envelope, list[Envelope]]:
    req = _make_request("agent.run", session_id=sid, name=name, prompt="acme/Model-GGUF")
    await ws.send_str(req.to_json())
    return await _recv_until_response(ws, req.id)


async def _send_and_reply(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, sid: str, **payload: object
) -> dict[str, object]:
    req = _make_request(msg_type, session_id=sid, **payload)
    await ws.send_str(req.to_json())
    return (await _recv_response(ws, req.id)).payload


def _last_state(events: list[Envelope]) -> dict[str, object]:
    return [e.payload for e in events if e.payload.get("type") == "state"][-1]


async def test_agent_run_starts_a_locked_autonomous_session(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    resp, events = await _agent_run(ws, sid)
    assert resp.payload["type"] == "agent.run.accepted"
    state = _last_state(events)
    assert state["top_agent"] == _RUN_ONLY_AGENT
    assert state["interactive"] is False
    assert state["autonomous"] is True


async def test_a_fresh_session_takes_input(ws: aiohttp.ClientWebSocketResponse) -> None:
    hello = await _hello(ws)
    state = cast("dict[str, object]", hello.payload["state"])
    assert state["interactive"] is True


async def test_agent_run_refuses_an_interactive_agent(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    interactive = AgentRegistry(_AGENTS_DIR).default_top_agent()
    resp, _ = await _agent_run(ws, sid, name=interactive)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "agent_run_refused"
    # Nothing changed: the session still takes prompts.
    reply = await _send_and_reply(ws, "prompt.submit", sid, text="hello")
    assert reply["type"] == "prompt.accepted"


async def test_agent_run_refuses_a_session_that_already_had_a_prompt(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    assert (await _send_and_reply(ws, "prompt.submit", sid, text="hello"))["type"] == (
        "prompt.accepted"
    )
    resp, _ = await _agent_run(ws, sid)
    assert resp.payload["code"] == "agent_run_refused"


async def test_agent_run_needs_a_prompt(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    reply = await _send_and_reply(ws, "agent.run", sid, name=_RUN_ONLY_AGENT, prompt="  ")
    assert reply["code"] == "empty_prompt"


@pytest.mark.parametrize(
    ("msg_type", "payload"),
    [
        ("prompt.submit", {"text": "and another thing"}),
        ("agent.set", {"name": ""}),
        ("mode.set", {"autonomous": False}),
    ],
)
async def test_a_locked_session_refuses_further_input(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, payload: dict[str, object]
) -> None:
    sid = await _open_session(ws)
    await _agent_run(ws, sid)
    reply = await _send_and_reply(ws, msg_type, sid, **payload)
    assert reply["type"] == "error"
    assert reply["code"] == "session_locked"


async def test_agent_set_cannot_select_a_non_interactive_agent(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    reply = await _send_and_reply(ws, "agent.set", sid, name=_RUN_ONLY_AGENT)
    assert reply["code"] == "agent_run_only"


async def test_a_locked_session_is_still_locked_after_a_server_restart() -> None:
    """``interactive`` is re-derived from the stored agent, so a resume keeps the lock."""
    first = TestServer(create_app(Config()))
    await first.start_server()
    http = aiohttp.ClientSession()
    try:
        a = await http.ws_connect(f"http://127.0.0.1:{first.port}/ws")
        sid = await _open_session(a)
        await _agent_run(a, sid)
        await a.close()
        await first.close()

        second = TestServer(create_app(Config()))
        await second.start_server()
        try:
            b = await http.ws_connect(f"http://127.0.0.1:{second.port}/ws")
            hello = await _hello(b, session_id=sid)
            state = cast("dict[str, object]", hello.payload["state"])
            assert state["top_agent"] == _RUN_ONLY_AGENT
            assert state["interactive"] is False
            reply = await _send_and_reply(b, "prompt.submit", sid, text="more")
            assert reply["code"] == "session_locked"
            await b.close()
        finally:
            await second.close()
    finally:
        await http.close()


async def test_agent_set_falls_back_when_the_name_is_unknown(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """An unrecognized selection is answered with the default, not an error.

    Uses ``"guided"`` on purpose: it is the pre-``kodo_`` vocabulary, which is
    exactly the unknown name a client or a stored setting is most likely to
    still be carrying. ``agent.set`` is a *selection* path, where falling back
    keeps the session usable; resuming a session is the path that reports the
    missing agent instead.
    """
    sid = str((await _hello(ws)).payload["session_id"])
    req = _make_request("agent.set", session_id=sid, name="guided")
    await ws.send_str(req.to_json())

    # One loop for both frames: the engine emits `state` *before* replying, so
    # reading the response first would discard the very event under test.
    accepted = False
    resolved = ""
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline and not (accepted and resolved):
        try:
            env = await _recv(ws, timeout=2.0)
        except TimeoutError:
            break
        if env.kind == "response" and env.correlation_id == req.id:
            assert env.payload["type"] == "agent.accepted"
            accepted = True
        elif env.kind == "event" and env.payload.get("type") == "state":
            resolved = str(env.payload["top_agent"])

    assert accepted, "agent.set was never acknowledged"
    # The state event carries the *resolved* name, so a client that sent an
    # unknown value sees the agent it actually got rather than what it typed.
    assert resolved == "kodo_problem_solver"


async def test_default_agent_get_reports_selection_and_effect(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """With no preference set, `selected` is empty and `effective` is the shipped one."""
    await _hello(ws)
    req = _make_request("default_agent.get")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    registry = AgentRegistry(_AGENTS_DIR)
    assert resp.payload["selected"] == ""
    assert resp.payload["effective"] == registry.default_top_agent()
    assert [a["name"] for a in resp.payload["agents"]] == [
        a.name for a in registry.top_agents() if a.selectable
    ]


async def test_default_agent_set_changes_what_a_new_session_starts_on(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """The user's pick beats the shipped default, and reaches hello.ack."""
    await _hello(ws)
    req = _make_request("default_agent.set", name="kodo_guide")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["ok"] is True
    assert resp.payload["selected"] == "kodo_guide"
    assert resp.payload["effective"] == "kodo_guide"

    # Read back over a fresh connection: the preference is persisted, and the
    # catalog a new session is handed now names it as the starting agent.
    req = _make_request("default_agent.get")
    await ws.send_str(req.to_json())
    assert (await _recv_response(ws, req.id)).payload["effective"] == "kodo_guide"


async def test_default_agent_set_rejects_a_non_selectable_agent(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    """A session must not be able to start on an agent with no interactive prompt."""
    await _hello(ws)
    await _install_hidden_agent(ws, _temp_home)
    req = _make_request("default_agent.set", name=_HIDDEN_AGENT)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["ok"] is False
    assert _HIDDEN_AGENT in resp.payload["error"]

    req = _make_request("default_agent.get")
    await ws.send_str(req.to_json())
    assert (await _recv_response(ws, req.id)).payload["selected"] == ""


async def test_default_agent_set_empty_clears_the_preference(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _hello(ws)
    for name in ("kodo_guide", ""):
        req = _make_request("default_agent.set", name=name)
        await ws.send_str(req.to_json())
        assert (await _recv_response(ws, req.id)).payload["ok"] is True

    req = _make_request("default_agent.get")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["selected"] == ""
    assert resp.payload["effective"] == AgentRegistry(_AGENTS_DIR).default_top_agent()


async def test_hello_emits_state_event(ws: aiohttp.ClientWebSocketResponse) -> None:
    await _hello(ws)
    received: list[Envelope] = []
    for _ in range(5):
        try:
            received.append(await _recv(ws, timeout=2.0))
        except TimeoutError:
            break
        if any(e.kind == "event" and e.payload.get("type") == "state" for e in received):
            break
    assert any(e.kind == "event" and e.payload.get("type") == "state" for e in received)


async def test_two_windows_get_distinct_sessions(server: TestServer) -> None:
    session = aiohttp.ClientSession()
    a = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    b = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid_a = str((await _hello(a, window_id="wa")).payload["session_id"])
        sid_b = str((await _hello(b, window_id="wb")).payload["session_id"])
        assert sid_a != sid_b
    finally:
        await a.close()
        await b.close()
        await session.close()


async def test_resume_in_use_session_is_rejected(server: TestServer) -> None:
    session = aiohttp.ClientSession()
    a = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    b = await session.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid = str((await _hello(a, window_id="wa")).payload["session_id"])
        resp = await _hello(b, session_id=sid, window_id="wb")
        assert resp.payload.get("error") == "session_in_use"
    finally:
        await a.close()
        await b.close()
        await session.close()


# ---------------------------------------------------------------------------
# unknown / session.list
# ---------------------------------------------------------------------------


async def test_unknown_message_returns_error(ws: aiohttp.ClientWebSocketResponse) -> None:
    req = _make_request("does.not.exist")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_message"


async def test_session_list_includes_open_session(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("session.list")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    sessions = resp.payload["sessions"]
    assert isinstance(sessions, list)
    entry = next(s for s in sessions if s["id"] == sid)
    assert entry["taken"] is True
    # The picker row carries the resolved name *and* its label, so the client
    # renders it without a mapping of its own.
    assert entry["agent"] == "kodo_problem_solver"
    assert entry["agent_label"] == "Problem Solver"


# ---------------------------------------------------------------------------
# session-scoped handlers require a session_id
# ---------------------------------------------------------------------------


async def test_prompt_submit_with_text_returns_accepted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    req = _make_request("prompt.submit", session_id=sid, text="Build me a trading bot.")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "prompt.accepted"


async def test_prompt_submit_with_empty_text_returns_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    sid = await _open_session(ws)
    req = _make_request("prompt.submit", session_id=sid, text="")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "empty_prompt"


async def test_prompt_submit_unknown_session_errors(ws: aiohttp.ClientWebSocketResponse) -> None:
    req = _make_request("prompt.submit", session_id="nope", text="hi")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "error"
    assert resp.payload["code"] == "unknown_session"


async def test_mode_set_autonomous_returns_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("mode.set", session_id=sid, autonomous=True)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "mode.accepted"


async def test_stop_returns_accepted(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    req = _make_request("stop", session_id=sid)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "stop.accepted"


async def test_server_shutdown_acks_with_this_processes_pid(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """`server.shutdown` (WS_PROTOCOL.md §7.6g) acks before it stops.

    The ack carries the PID precisely so kodo-vsix can then watch that process
    disappear — which is its real "shutdown finished" signal, since the ack
    only means "accepted". Nothing actually stops here: the stop callback is
    wired in `kodo.server.__main__`, not `create_app`, so this in-process test
    server takes the no-op branch of `ConnectionRegistry.request_shutdown`
    (covered in test_connection_registry.py) and survives the request.
    """
    req = _make_request("server.shutdown", reason="py-kodo upgrade")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "server.shutdown.ack"
    assert resp.payload["ok"] is True
    assert resp.payload["pid"] == os.getpid()


async def test_session_delete_closes_socket_and_drops_listing(server: TestServer) -> None:
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    sid = ""
    try:
        sid = await _open_session(conn)
        req = _make_request("session.delete", session_id=sid)
        await conn.send_str(req.to_json())
        # The server closes the socket on success (possibly after a trailing
        # state event emitted by the engine stop). Drain until the close.
        closed = False
        for _ in range(10):
            msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                closed = True
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                env = Envelope.from_json(str(msg.data))
                assert env.payload.get("type") != "session.delete.error"
        assert closed
    finally:
        await conn.close()
        await csession.close()

    # A fresh connection no longer lists the deleted session. (session.list
    # needs no session of its own, so we don't open one — which would otherwise
    # reuse the just-freed timestamp id and reappear in the listing.)
    csession2 = aiohttp.ClientSession()
    conn2 = await csession2.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        req = _make_request("session.list")
        await conn2.send_str(req.to_json())
        resp = await _recv_response(conn2, req.id)
        ids = {s["id"] for s in resp.payload["sessions"]}
        assert sid not in ids
    finally:
        await conn2.close()
        await csession2.close()


async def test_session_delete_unknown_session_errors(server: TestServer) -> None:
    # autoping=False so the raw PONG frame below is observable via receive()
    # instead of being swallowed by aiohttp's automatic control-frame handling.
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws", autoping=False)
    try:
        req = _make_request("session.delete", session_id="nope")
        await conn.send_str(req.to_json())
        resp = await _recv_response(conn, req.id)
        assert resp.payload["type"] == "error"
        assert resp.payload["code"] == "unknown_session"
        # The socket stays open: a raw WS ping still round-trips a pong.
        await conn.ping()
        msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
        assert msg.type == aiohttp.WSMsgType.PONG
    finally:
        await conn.close()
        await csession.close()


async def test_orphan_response_is_silently_dropped(server: TestServer) -> None:
    csession = aiohttp.ClientSession()
    conn = await csession.ws_connect(f"http://127.0.0.1:{server.port}/ws", autoping=False)
    try:
        orphan = Envelope(
            kind="response", correlation_id="no-such-request", payload={"action": "agree"}
        )
        await conn.send_str(orphan.to_json())
        # The socket stays open: a raw WS ping still round-trips a pong.
        await conn.ping()
        msg = await asyncio.wait_for(conn.receive(), timeout=_RECV_TIMEOUT)
        assert msg.type == aiohttp.WSMsgType.PONG
    finally:
        await conn.close()
        await csession.close()


# ---------------------------------------------------------------------------
# checkpoint.* — full wire protocol against a real RootMirrorManager-backed
# root (no LLM/tool-dispatch involved: the checkpoint history is seeded
# directly via RootMirrorManager, the same on-disk artifacts a real tool
# call would produce — see test_checkpoint_state.py for the engine-level
# coverage this builds on).
# ---------------------------------------------------------------------------


async def _recv_until_response(
    ws: aiohttp.ClientWebSocketResponse, correlation_id: str, timeout: float = _RECV_TIMEOUT
) -> tuple[Envelope, list[Envelope]]:
    """Like _recv_response, but also returns every event seen along the way."""
    events: list[Envelope] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"No response for id={correlation_id!r} within {timeout}s")
        env = await _recv(ws, timeout=remaining)
        if env.kind == "response" and env.correlation_id == correlation_id:
            return env, events
        if env.kind == "event":
            events.append(env)


async def _seed_two_checkpoints(root: Path) -> tuple[str, str]:
    """Create a real two-checkpoint mirror history at *root* and return the shas."""
    from kodo.runtime._checkpoints import RootMirrorManager

    mgr = RootMirrorManager([root])
    await mgr.prepare(root / "a.txt")
    (root / "a.txt").write_text("one\n")
    ref1 = await mgr.commit_for_path(root / "a.txt", "create a")
    assert ref1 is not None
    await mgr.prepare(root / "a.txt")
    (root / "a.txt").write_text("two\n")
    ref2 = await mgr.commit_for_path(root / "a.txt", "edit a")
    assert ref2 is not None
    return ref1.sha, ref2.sha


async def test_checkpoint_list_returns_seeded_state(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.list", session_id=sid, root=str(root))
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "checkpoint.list.done"
    assert resp.payload["current_index"] == 1
    entries = resp.payload["entries"]
    assert [e["sha"] for e in entries] == [sha1, sha2]
    assert all(e["undone"] is False for e in entries)


async def test_checkpoint_undo_flips_undone_and_pushes_state(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, _sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.undo", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp, events = await _recv_until_response(ws, req.id)

    assert resp.payload["type"] == "checkpoint.undo.done"
    # entries grew by one (the undo itself is a new forward commit).
    entries = resp.payload["entries"]
    assert len(entries) == 3
    assert entries[0]["sha"] == sha1
    assert entries[0]["undone"] is True
    assert resp.payload["current_index"] == 2
    assert not (root / "a.txt").exists()

    state_events = [e for e in events if e.payload.get("type") == "checkpoint.state"]
    assert len(state_events) == 1
    assert state_events[0].payload["root"] == str(root)
    assert state_events[0].payload["current_index"] == 2


async def test_checkpoint_rollback_then_roll_forward(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    req = _make_request("checkpoint.rollback", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.rollback.done"
    assert resp.payload["current_index"] == 0
    assert (root / "a.txt").read_text() == "one\n"

    req = _make_request("checkpoint.roll_forward", session_id=sid, root=str(root), sha=sha2)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.roll_forward.done"
    assert resp.payload["current_index"] == 1
    assert (root / "a.txt").read_text() == "two\n"


async def test_checkpoint_undo_on_dirty_tree_needs_confirmation_then_stash(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sha1, _sha2 = await _seed_two_checkpoints(root)
    sid = await _open_session(ws)

    # An edit made outside of Kodo, never committed to the mirror.
    (root / "untracked.txt").write_text("surprise\n")

    req = _make_request("checkpoint.undo", session_id=sid, root=str(root), sha=sha1)
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.undo.needs_confirmation"
    assert resp.payload["root"] == str(root)
    assert resp.payload["sha"] == sha1
    # Nothing was touched — the dirty file is still there untouched.
    assert (root / "untracked.txt").read_text() == "surprise\n"
    assert (root / "a.txt").read_text() == "two\n"

    req = _make_request(
        "checkpoint.undo", session_id=sid, root=str(root), sha=sha1, resolution="stash"
    )
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)
    assert resp.payload["type"] == "checkpoint.undo.done"
    assert not (root / "a.txt").exists()
    # Stashed change reapplied afterwards.
    assert (root / "untracked.txt").read_text() == "surprise\n"


# ---------------------------------------------------------------------------
# local_llm.install — the fire-and-forget background download pushes a
# second local_llm.registry_state once it actually finishes, not just the
# immediate kickoff one (see _run_background_download in _app.py).
# ---------------------------------------------------------------------------


def _local_entry(payload: dict[str, object], name: str) -> dict[str, object]:
    registry = cast("list[dict[str, object]]", payload["local_registry"])
    return next(e for e in registry if e["name"] == name)


async def test_local_llm_install_pushes_registry_state_again_on_completion(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.llms.local import LocalModelManager

    req = _seed_custom_hf("test-model")
    await ws.send_str(req.to_json())
    added = await _recv_with_drain(ws)
    assert added.payload["type"] == "local_llm.registry_state"
    assert _local_entry(added.payload, "test-model")["installed"] is False

    # download_model would otherwise block on a real HF fetch; get_model_path
    # is what _local_registry_payload consults for installed/installed_path,
    # so faking both — gated on the same "has the fake download run yet" flag
    # — reproduces the real installed=False-then-True transition without
    # touching the real transfer machinery (covered separately by
    # test_llms_local.py).
    downloaded = {"done": False}

    async def _fake_download(self: object, *a: object, **k: object) -> None:
        downloaded["done"] = True

    monkeypatch.setattr(LocalModelManager, "download_model", _fake_download)
    monkeypatch.setattr(
        LocalModelManager,
        "get_model_path",
        lambda self, name: Path("/fake/model.gguf") if downloaded["done"] else None,
    )

    req = _make_request("local_llm.install", name="test-model")
    await ws.send_str(req.to_json())

    kickoff = await _recv_with_drain(ws)
    assert kickoff.payload["type"] == "local_llm.registry_state"
    assert _local_entry(kickoff.payload, "test-model")["installed"] is False

    completed = await _recv_with_drain(ws)
    assert completed.payload["type"] == "local_llm.registry_state"
    completed_entry = _local_entry(completed.payload, "test-model")
    assert completed_entry["installed"] is True
    assert Path(completed_entry["installed_path"]) == Path("/fake/model.gguf")


async def test_local_llm_install_pushes_registry_state_after_failure_too(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.llms.local import LocalModelError, LocalModelManager

    req = _seed_custom_hf("test-model")
    await ws.send_str(req.to_json())
    await _recv_with_drain(ws)  # kickoff-of-add registry_state, not under test here

    async def _boom(self: object, *a: object, **k: object) -> None:
        raise LocalModelError("network is on fire")

    monkeypatch.setattr(LocalModelManager, "download_model", _boom)

    req = _make_request("local_llm.install", name="test-model")
    await ws.send_str(req.to_json())

    await _recv_with_drain(ws)  # kickoff registry_state

    error_evt = await _recv_with_drain(ws)
    assert error_evt.payload["type"] == "error"
    assert error_evt.payload["code"] == "local_llm_error"
    assert "network is on fire" in error_evt.payload["message"]

    completed = await _recv_with_drain(ws)
    assert completed.payload["type"] == "local_llm.registry_state"
    assert _local_entry(completed.payload, "test-model")["installed"] is False


async def test_local_llm_update_uninstalls_then_reinstalls(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_llm.update is a server-side "click Uninstall, wait, click
    Install" (doc/LOCAL_MODEL_MANAGER.md §12) — reusing the exact same
    LocalModelManager.uninstall/download_model calls those two commands make,
    so it must push the same shape of registry_state transitions: installed
    (already true here) -> uninstalled -> kickoff (still uninstalled) ->
    installed again."""
    from kodo.llms.local import LocalModelManager

    req = _seed_custom_hf("test-model")
    await ws.send_str(req.to_json())
    await _recv_with_drain(ws)  # add's own registry_state, not under test

    installed = {"v": True}
    monkeypatch.setattr(
        LocalModelManager,
        "get_model_path",
        lambda self, name: Path("/fake/model.gguf") if installed["v"] else None,
    )

    def _fake_uninstall(self: object, name: str) -> None:
        installed["v"] = False

    async def _fake_download(self: object, *a: object, **k: object) -> None:
        installed["v"] = True

    monkeypatch.setattr(LocalModelManager, "uninstall", _fake_uninstall)
    monkeypatch.setattr(LocalModelManager, "download_model", _fake_download)

    req = _make_request("local_llm.update", name="test-model")
    await ws.send_str(req.to_json())

    uninstalled = await _recv_with_drain(ws)
    assert uninstalled.payload["type"] == "local_llm.registry_state"
    assert _local_entry(uninstalled.payload, "test-model")["installed"] is False

    completed = await _recv_with_drain(ws)
    assert completed.payload["type"] == "local_llm.registry_state"
    completed_entry = _local_entry(completed.payload, "test-model")
    assert completed_entry["installed"] is True
    assert Path(completed_entry["installed_path"]) == Path("/fake/model.gguf")


class _FakeDownloads:
    """Stands in for LocalModelManager's download state: a set of finished model ids.

    Every manager method the local_llm.* handlers and the MTP head helpers go
    through is patched to read or write this set, so a handler's whole effect
    on downloads is observable as which ids end up in it.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, downloaded: set[str]) -> None:
        from kodo.llms.local import LocalModelManager, ModelRecord

        self.downloaded = downloaded

        async def _download(_self: object, model_id: str, *a: object, **k: object) -> None:
            downloaded.add(model_id)

        def _record(model_id: str) -> ModelRecord:
            return ModelRecord(model_id=model_id, repo_id="r", revision="main", commit_hash=None)

        monkeypatch.setattr(LocalModelManager, "download_model", _download)
        monkeypatch.setattr(
            LocalModelManager,
            "get_model_path",
            lambda _self, name: Path(f"/fake/{name}.gguf") if name in downloaded else None,
        )
        monkeypatch.setattr(
            LocalModelManager, "uninstall", lambda _self, name: downloaded.discard(name)
        )
        monkeypatch.setattr(
            LocalModelManager, "list_models", lambda _self: [_record(i) for i in downloaded]
        )


def _entry_with_mtp_heads() -> tuple[str, list[str], str, str]:
    """A shipped entry whose family ships MTP heads: its name, the heads' download
    ids, the head picker's knob id and one head's option id — read off the live
    catalog."""
    from kodo.llms import get_local_registry, get_mtp_sidecars, mtp_sidecar_model_id
    from kodo.project import kodo_user_dir

    kodo_dir = kodo_user_dir()
    entry = next(
        e for e in get_local_registry(kodo_dir).values() if get_mtp_sidecars(kodo_dir, e.base_llm)
    )
    heads = [
        mtp_sidecar_model_id(entry.base_llm, h.id)
        for h in get_mtp_sidecars(kodo_dir, entry.base_llm)
    ]
    picker = next(k.id for k in entry.knobs if k.id.startswith("spec-decoding-mtp-head:"))
    return entry.name, heads, picker, get_mtp_sidecars(kodo_dir, entry.base_llm)[0].id


async def test_local_llm_install_also_downloads_the_familys_mtp_heads(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, heads, _, _ = _entry_with_mtp_heads()
    fake = _FakeDownloads(monkeypatch, set())

    req = _make_request("local_llm.install", name=name)
    await ws.send_str(req.to_json())
    await _recv_with_drain(ws)  # kickoff
    await _recv_with_drain(ws)  # the quant finished
    heads_done = await _recv_with_drain(ws)  # the heads finished

    assert heads_done.payload["type"] == "local_llm.registry_state"
    assert fake.downloaded == {name, *heads}


async def test_local_llm_uninstall_of_the_last_quant_also_deletes_its_mtp_heads(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, heads, _, _ = _entry_with_mtp_heads()
    fake = _FakeDownloads(monkeypatch, {name, *heads})

    req = _make_request("local_llm.uninstall", name=name)
    await ws.send_str(req.to_json())
    state = await _recv_with_drain(ws)

    assert state.payload["type"] == "local_llm.registry_state"
    assert fake.downloaded == set()


async def test_picking_a_missing_mtp_head_downloads_the_familys_heads(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model installed before its family had heads gets them once one is picked."""
    name, heads, picker, head_id = _entry_with_mtp_heads()
    fake = _FakeDownloads(monkeypatch, {name})

    req = _make_request("local_llm.set_knobs", name=name, knobs={picker: head_id})
    await ws.send_str(req.to_json())
    await _recv_with_drain(ws)  # the knob change itself
    heads_done = await _recv_with_drain(ws)

    assert heads_done.payload["type"] == "local_llm.registry_state"
    assert fake.downloaded == {name, *heads}


async def test_local_llm_update_rejects_unknown_model(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request("local_llm.update", name="does-not-exist")
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_local_llm_check_updates_reports_only_stale_names(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodo.llms.local import LocalModelManager

    for name in ("stale-model", "current-model"):
        req = _seed_custom_hf(name)
        await ws.send_str(req.to_json())
        await _recv(ws)  # each add's own registry_state, not under test

    async def _fake_check(self: object, name: str, **k: object) -> bool:
        return name == "stale-model"

    monkeypatch.setattr(LocalModelManager, "check_for_update", _fake_check)

    req = _make_request(
        "local_llm.check_updates",
        names=["stale-model", "current-model", "unknown-model"],
    )
    await ws.send_str(req.to_json())

    evt = await _recv(ws)
    assert evt.payload["type"] == "local_llm.updates_available"
    assert evt.payload["updatable"] == ["stale-model"]


async def test_add_file_keeps_its_llama_args_as_base_args(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    """A freshly-added custom entry's own `llama_args` field (still collected
    by the "Add local LLM from file" modal) becomes its Default profile's base
    args rather than being dropped on the floor — see doc/LLM_REGISTRY.md §4.6.
    The shared base args are merged underneath, so --jinja survives too."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    req = _make_request(
        "local_llm.add_file",
        name="test-model",
        description="",
        path=str(model),
        llama_args={"--cache-type-k": "q8_0"},
        context_window=32768,
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    entry = _local_entry(added.payload, "test-model")
    assert entry["active_profile"] == ""
    assert entry["profiles"] == []
    args = cast("dict[str, str]", entry["default_profile_args"])
    assert args["--cache-type-k"] == "q8_0"
    assert args["--jinja"] == ""


async def test_add_file_gets_the_shared_knobs(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    """A user-added LLM is configurable exactly like a built-in one."""
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    req = _make_request(
        "local_llm.add_file",
        name="test-model",
        description="",
        path=str(model),
        context_window=32768,
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    entry = _local_entry(added.payload, "test-model")
    knobs = cast("list[str]", entry["knobs"])
    assert "tail-culling" in knobs and "temperature" in knobs
    # Every knob it offers has a resolved selection — never sparse.
    assert set(cast("dict[str, str]", entry["knob_selections"])) == set(knobs)


# ---------------------------------------------------------------------------
# Launch configuration (local_llm.add_profile / .update_profile /
# .remove_profile / .set_active_profile / .set_knobs) — see
# doc/LLM_REGISTRY.md §4.6. Uses a real hardcoded entry name (no download
# needed — none of this touches the download manager) so add_profile's own
# entry-existence check passes without first adding a custom entry.
# ---------------------------------------------------------------------------

_PROFILE_TEST_ENTRY = "unsloth-qwen35-9b-q8-k-xl"


def _profile_ids(entry: dict[str, object]) -> list[str]:
    return [cast("dict[str, object]", p)["id"] for p in cast("list[object]", entry["profiles"])]


async def test_registry_state_ships_a_deduplicated_knob_def_table(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    """Entries reference knobs by id; the definitions are sent once, centrally."""
    ack = await _control_hello(ws)
    knob_defs = cast("dict[str, object]", ack.payload["knob_defs"])
    entry = _local_entry(ack.payload, _PROFILE_TEST_ENTRY)
    for knob_id in cast("list[str]", entry["knobs"]):
        assert knob_id in knob_defs
    culling = cast("dict[str, object]", knob_defs["tail-culling"])
    assert culling["kind"] == "dropdown"
    assert [cast("dict[str, object]", o)["id"] for o in cast("list[object]", culling["options"])][
        0
    ] == "off"


async def test_registry_state_ships_the_llama_arg_catalog(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    ack = await _control_hello(ws)
    catalog = cast("list[dict[str, object]]", ack.payload["llama_arg_catalog"])
    flags = {c["flag"] for c in catalog}
    assert "--ctx-size" in flags and "--temp" in flags
    # Reserved flags kodo sets itself are never offered.
    assert "--port" not in flags and "--model" not in flags


async def test_set_knobs_changes_the_default_profile_args(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.set_knobs",
        name=_PROFILE_TEST_ENTRY,
        knobs={"tail-culling": "medium"},
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"
    entry = _local_entry(resp.payload, _PROFILE_TEST_ENTRY)
    assert cast("dict[str, str]", entry["knob_selections"])["tail-culling"] == "medium"
    assert cast("dict[str, str]", entry["default_profile_args"])["--min-p"] == "0.08"


async def test_set_knobs_rejects_an_unknown_option(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.set_knobs", name=_PROFILE_TEST_ENTRY, knobs={"tail-culling": "nope"}
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_add_profile_appears_in_registry_state(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_profile",
        name=_PROFILE_TEST_ENTRY,
        profile_name="1M Context",
        description="",
        llama_args_text="--ctx-size 1048576\n--rope-scaling yarn",
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"
    entry = _local_entry(resp.payload, _PROFILE_TEST_ENTRY)
    # Adding a profile does not select it — the Default profile stays active.
    assert entry["active_profile"] == ""
    profiles = cast("list[dict[str, object]]", entry["profiles"])
    assert len(profiles) == 1
    assert profiles[0]["id"] == "1m-context"
    assert profiles[0]["name"] == "1M Context"
    assert profiles[0]["llama_args"] == {"--ctx-size": "1048576", "--rope-scaling": "yarn"}


async def test_add_profile_strips_reserved_args(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_profile",
        name=_PROFILE_TEST_ENTRY,
        profile_name="Sneaky",
        llama_args_text="--ctx-size 4096\n--port 9999",
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    entry = _local_entry(resp.payload, _PROFILE_TEST_ENTRY)
    profiles = cast("list[dict[str, object]]", entry["profiles"])
    assert profiles[0]["llama_args"] == {"--ctx-size": "4096"}


async def test_update_profile_edits_it_in_place(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_profile",
        name=_PROFILE_TEST_ENTRY,
        profile_name="Tight VRAM",
        llama_args_text="--n-gpu-layers 20",
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    profile_id = _profile_ids(_local_entry(added.payload, _PROFILE_TEST_ENTRY))[0]

    req = _make_request(
        "local_llm.update_profile",
        name=_PROFILE_TEST_ENTRY,
        profile_id=profile_id,
        profile_name="Tight VRAM (v2)",
        description="edited",
        llama_args_text="--n-gpu-layers 10",
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    entry = _local_entry(resp.payload, _PROFILE_TEST_ENTRY)
    profiles = cast("list[dict[str, object]]", entry["profiles"])
    updated = next(p for p in profiles if p["id"] == profile_id)
    assert updated["name"] == "Tight VRAM (v2)"
    assert updated["description"] == "edited"
    assert updated["llama_args"] == {"--n-gpu-layers": "10"}


async def test_update_profile_rejects_unknown_profile_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.update_profile",
        name=_PROFILE_TEST_ENTRY,
        profile_id="nonexistent",
        profile_name="Whatever",
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_add_profile_rejects_custom_server_url_entry(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_server_url", name="remote", description="", url="http://host:8042"
    )
    await ws.send_str(req.to_json())
    await _recv(ws)  # add's own registry_state, not under test here

    req = _make_request("local_llm.add_profile", name="remote", profile_name="whatever")
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_add_profile_dedupes_id_when_different_names_share_a_slug(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    resp = None
    for profile_name in ("Tight VRAM", "tight vram"):
        req = _make_request(
            "local_llm.add_profile", name=_PROFILE_TEST_ENTRY, profile_name=profile_name
        )
        await ws.send_str(req.to_json())
        resp = await _recv(ws)
    assert resp is not None
    assert _profile_ids(_local_entry(resp.payload, _PROFILE_TEST_ENTRY)) == [
        "tight-vram",
        "tight-vram-2",
    ]


async def test_add_profile_rejects_duplicate_name(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_profile", name=_PROFILE_TEST_ENTRY, profile_name="Tight VRAM"
    )
    await ws.send_str(req.to_json())
    await _recv(ws)
    req = _make_request(
        "local_llm.add_profile", name=_PROFILE_TEST_ENTRY, profile_name="Tight VRAM"
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"
    assert "already exists" in str(err.payload["message"])


async def test_set_active_profile_then_remove_resets_to_default(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.add_profile", name=_PROFILE_TEST_ENTRY, profile_name="Tight VRAM"
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    profile_id = _profile_ids(_local_entry(added.payload, _PROFILE_TEST_ENTRY))[0]

    req = _make_request(
        "local_llm.set_active_profile", name=_PROFILE_TEST_ENTRY, profile_id=profile_id
    )
    await ws.send_str(req.to_json())
    active = await _recv(ws)
    assert active.payload["type"] == "local_llm.registry_state"
    assert _local_entry(active.payload, _PROFILE_TEST_ENTRY)["active_profile"] == profile_id

    req = _make_request("local_llm.remove_profile", name=_PROFILE_TEST_ENTRY, profile_id=profile_id)
    await ws.send_str(req.to_json())
    removed = await _recv(ws)
    entry = _local_entry(removed.payload, _PROFILE_TEST_ENTRY)
    assert entry["profiles"] == []
    assert entry["active_profile"] == ""


async def test_set_active_profile_rejects_unknown_profile_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.set_active_profile", name=_PROFILE_TEST_ENTRY, profile_id="nonexistent"
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_remove_profile_rejects_unknown_profile_id(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    req = _make_request(
        "local_llm.remove_profile", name=_PROFILE_TEST_ENTRY, profile_id="nonexistent"
    )
    await ws.send_str(req.to_json())
    err = await _recv(ws)
    assert err.payload["type"] == "error"
    assert err.payload["code"] == "local_llm_error"


async def test_reconfiguring_the_currently_selected_model_does_not_crash_without_server(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    """Exercises the restart-check path (_restart_llama_server_if_running) for
    the entry that IS the currently selected local model — it must no-op
    cleanly when nothing is actually running (llama.cpp isn't installed in
    this sandboxed test environment), not raise. Covers both triggers, a
    profile switch and a knob change. The actual subprocess restart itself is
    out of scope here."""
    # Select the entry the way the panel's settings write does, without
    # requiring a real llama-server process to actually start.
    settings = _temp_home / ".kodo" / "etc" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"mode": "local", "models": {"local": _PROFILE_TEST_ENTRY}}), encoding="utf-8"
    )

    req = _make_request(
        "local_llm.set_knobs", name=_PROFILE_TEST_ENTRY, knobs={"tail-culling": "strong"}
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"

    req = _make_request(
        "local_llm.add_profile", name=_PROFILE_TEST_ENTRY, profile_name="Tight VRAM"
    )
    await ws.send_str(req.to_json())
    added = await _recv(ws)
    profile_id = _profile_ids(_local_entry(added.payload, _PROFILE_TEST_ENTRY))[0]

    req = _make_request(
        "local_llm.set_active_profile", name=_PROFILE_TEST_ENTRY, profile_id=profile_id
    )
    await ws.send_str(req.to_json())
    resp = await _recv(ws)
    assert resp.payload["type"] == "local_llm.registry_state"
    assert _local_entry(resp.payload, _PROFILE_TEST_ENTRY)["active_profile"] == profile_id


# ---------------------------------------------------------------------------
# llamacpp.update — failure reporting and process teardown
# ---------------------------------------------------------------------------


async def test_llamacpp_update_reports_a_failure_that_never_emitted_progress(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before the first progress frame still ends the stream.

    ``update_llamacpp`` deletes the previous build before ``install_llamacpp``
    reports anything, and on Windows that delete raises a sharing violation
    when a llama-server is still mapped onto those files. The exception used to
    be swallowed with no event and no log line, so kodo-vsix's progress toast —
    which is only ever dismissed by a terminal frame — hung forever, and the
    busy flag it sets made every retry a silent no-op. The synthesized
    ``percent: -1`` below is what makes that impossible.
    """

    from kodo.llms.llamacpp import LlamaServer

    async def _no_op_stop_titling() -> None:
        return None

    def _boom(*_a: object, **_k: object) -> object:
        raise PermissionError(32, "The process cannot access the file")

    # Pinned, not assumed: LlamaServer tracks the active server in a
    # class-level singleton that other test modules leave set, and a live one
    # makes the handler emit a llama.state event ahead of the frame under test.
    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: None))
    monkeypatch.setattr(_app_module, "stop_titling", _no_op_stop_titling)
    monkeypatch.setattr(_app_module, "find_installed", lambda _dir: None)
    monkeypatch.setattr(_app_module, "fetch_latest_build_number", lambda: 9999)
    monkeypatch.setattr(_app_module, "update_llamacpp", _boom)

    await ws.send_str(_make_request("llamacpp.update").to_json())

    evt = await _recv_with_drain(ws)
    assert evt.payload["type"] == "llamacpp.install.progress"
    assert evt.payload["percent"] == -1
    assert "cannot access the file" in str(evt.payload["message"])


async def test_llamacpp_update_does_not_duplicate_a_reported_failure(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work that reports its own -1 before raising produces exactly one error frame."""

    from kodo.llms.llamacpp import LlamaServer

    async def _no_op_stop_titling() -> None:
        return None

    def _fail_after_reporting(
        _dir: Path, *, version: int | None = None, progress_cb: object = None
    ) -> object:
        assert callable(progress_cb)
        progress_cb(-1, "llama-server --version returned non-zero exit code")
        raise RuntimeError("llama-server --version returned non-zero exit code")

    # See the sibling test above for why this is pinned rather than assumed.
    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: None))
    monkeypatch.setattr(_app_module, "stop_titling", _no_op_stop_titling)
    monkeypatch.setattr(_app_module, "find_installed", lambda _dir: None)
    monkeypatch.setattr(_app_module, "fetch_latest_build_number", lambda: 9999)
    monkeypatch.setattr(_app_module, "update_llamacpp", _fail_after_reporting)

    await ws.send_str(_make_request("llamacpp.update").to_json())

    evt = await _recv_with_drain(ws)
    assert evt.payload["percent"] == -1
    assert evt.payload["message"] == "llama-server --version returned non-zero exit code"

    # Nothing further: no synthesized duplicate riding on top of the real one.
    with pytest.raises(TimeoutError):
        await _recv_with_drain(ws, timeout=0.5)


async def test_llamacpp_update_to_the_installed_build_short_circuits(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned version equal to the installed build succeeds without doing anything.

    Update never reinstalls in place — that route is the user's explicit
    "Uninstall llama.cpp" then "Install llama.cpp" pair. So this must not stop
    either llama-server, must not probe GitHub, and must not touch the install.
    """
    from kodo.llms.llamacpp import LlamaInstall, LlamaServer

    def _must_not_run(*_a: object, **_k: object) -> object:
        raise AssertionError("the installed build must be left completely alone")

    # Recorded rather than raised: app teardown legitimately calls
    # stop_titling (_stop_background), long after the request under test.
    titler_stops: list[object] = []

    async def _record_stop_titling() -> None:
        titler_stops.append(object())

    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: None))
    monkeypatch.setattr(
        _app_module,
        "find_installed",
        lambda _dir: LlamaInstall(
            build=9876, install_dir=Path("/fake/b9876"), executable=Path("/fake/llama-server")
        ),
    )
    monkeypatch.setattr(_app_module, "stop_titling", _record_stop_titling)
    monkeypatch.setattr(_app_module, "build_exists", _must_not_run)
    monkeypatch.setattr(_app_module, "update_llamacpp", _must_not_run)

    await ws.send_str(_make_request("llamacpp.update", version="b9876").to_json())

    evt = await _recv_with_drain(ws)
    assert evt.payload["type"] == "llamacpp.install.progress"
    assert evt.payload["percent"] == 100
    assert evt.payload["up_to_date"] is True
    assert "already installed" in str(evt.payload["message"])
    assert titler_stops == [], "a no-op update must not stop the titler"


async def test_llamacpp_update_stops_the_chat_llama_server_first(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat llama-server holds the very files the update replaces.

    Stopping only the titler (what this handler did until now) leaves kodo's own
    llama-server mapped onto the build directory, which Windows then refuses to
    delete — the root cause of the stuck update. `llamacpp.uninstall` has always
    stopped both; this asserts `llamacpp.update` does too.
    """
    from kodo.llms.llamacpp import LlamaServer

    stopped: list[str] = []

    async def _no_op_stop_titling() -> None:
        stopped.append("titler")

    class _FakeServer:
        # Read by _stop_background/_release_llama_gpu during app teardown.
        is_running = False

        async def stop(self) -> None:
            stopped.append("chat")

    def _update(_dir: Path, *, version: int | None = None, progress_cb: object = None) -> object:
        assert stopped == ["titler", "chat"], "both servers must be down before the delete"
        assert callable(progress_cb)
        progress_cb(100, "installed")
        return object()

    monkeypatch.setattr(_app_module, "stop_titling", _no_op_stop_titling)
    monkeypatch.setattr(_app_module, "find_installed", lambda _dir: None)
    monkeypatch.setattr(_app_module, "fetch_latest_build_number", lambda: 9999)
    monkeypatch.setattr(_app_module, "update_llamacpp", _update)
    monkeypatch.setattr(
        LlamaServer, "get_active_llama_server", classmethod(lambda cls: _FakeServer())
    )
    monkeypatch.setattr(_app_module, "start_titling", lambda *_a, **_k: asyncio.sleep(0))

    await ws.send_str(_make_request("llamacpp.update").to_json())

    evt = await _recv_with_drain(ws)
    assert evt.payload["type"] == "llama.state"
    assert evt.payload["running"] is False

    evt = await _recv_with_drain(ws)
    assert evt.payload["type"] == "llamacpp.install.progress"
    assert evt.payload["percent"] == 100
    assert stopped == ["titler", "chat"]


# ---------------------------------------------------------------------------
# skills.list / skills.delete (doc/WS_PROTOCOL.md §7.6j, doc/SKILLS.md §5)
#
# ``_temp_home`` redirects HOME, so the skills root these exercise is
# ``tmp_path/.kodo/skills`` — the developer's real installed skills are never
# read and, more importantly, never deleted.
# ---------------------------------------------------------------------------


def _install_skill(home: Path, name: str, text: str) -> Path:
    directory = home / ".kodo" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


async def _skills_request(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, **payload: object
) -> dict[str, object]:
    req = _make_request(msg_type, **payload)
    await ws.send_str(req.to_json())
    return (await _recv_response(ws, req.id)).payload


async def test_server_creates_the_skills_root_on_startup(
    server: TestServer, _temp_home: Path
) -> None:
    """The user is told to drop skills into a directory, so it must exist."""
    assert (_temp_home / ".kodo" / "skills").is_dir()


async def test_skills_list_returns_installed_skills_and_the_root(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _install_skill(
        _temp_home, "pdf", "---\nname: pdf\ndescription: Work with PDF files.\n---\n\nUse pypdf.\n"
    )
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.list")

    assert payload["type"] == "skills.list.ack"
    assert payload["root"] == str(_temp_home / ".kodo" / "skills")
    skills = cast(list[dict[str, object]], payload["skills"])
    assert len(skills) == 1
    assert skills[0]["name"] == "pdf"
    assert skills[0]["description"] == "Work with PDF files."
    assert skills[0]["path"] == str(_temp_home / ".kodo" / "skills" / "pdf")
    assert skills[0]["error"] == ""


async def test_skills_list_includes_broken_skills_with_an_error(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    """Broken skills must stay visible in the panel so they can be deleted."""
    _install_skill(_temp_home, "halfbaked", "no frontmatter here\n")
    await _control_hello(ws)

    skills = cast(list[dict[str, object]], (await _skills_request(ws, "skills.list"))["skills"])

    assert len(skills) == 1
    assert skills[0]["name"] == "halfbaked"
    assert skills[0]["description"] == ""
    assert "frontmatter" in str(skills[0]["error"])


async def test_skills_delete_removes_the_directory_and_returns_the_new_listing(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    doomed = _install_skill(_temp_home, "pdf", "---\nname: pdf\ndescription: D.\n---\n\nB.\n")
    (doomed / "REFERENCE.md").write_text("companion", encoding="utf-8")
    _install_skill(_temp_home, "keeper", "---\nname: keeper\ndescription: K.\n---\n\nB.\n")
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.delete", name="pdf")

    assert payload["type"] == "skills.delete.ack"
    assert payload["ok"] is True
    assert not doomed.exists(), "the whole directory goes, companion files included"
    skills = cast(list[dict[str, object]], payload["skills"])
    assert [s["name"] for s in skills] == ["keeper"], "the ack carries the refreshed listing"


async def test_skills_delete_of_an_unknown_name_fails_without_closing_the_socket(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _install_skill(_temp_home, "keeper", "---\nname: keeper\ndescription: K.\n---\n\nB.\n")
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.delete", name="ghost")

    assert payload["ok"] is False
    assert "ghost" in str(payload["error"])
    # The failure ack still carries the listing, which is what lets the panel
    # correct itself when it was showing something already gone from disk.
    assert [s["name"] for s in cast(list[dict[str, object]], payload["skills"])] == ["keeper"]


async def test_skills_delete_cannot_escape_the_skills_root(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    """``name`` is re-validated server-side regardless of what a client sends."""
    (_temp_home / ".kodo" / "skills").mkdir(parents=True, exist_ok=True)
    victim = _temp_home / ".kodo" / "etc"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "settings.json").write_text("{}", encoding="utf-8")
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.delete", name="../etc")

    assert payload["ok"] is False
    assert (victim / "settings.json").exists()


# ---------------------------------------------------------------------------
# skills.install_scan / skills.install (doc/WS_PROTOCOL.md §7.6j, doc/SKILLS.md §2)
#
# ``git clone`` accepts a local path as its URL, so a local repo built with
# ``_git_skill_repo`` stands in for a GitHub URL with no network access.
# ---------------------------------------------------------------------------

_GIT_AVAILABLE = shutil.which("git") is not None
_requires_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git CLI not on PATH")


def _git_skill_repo(tmp_path: Path, dirname: str, files: dict[str, str]) -> Path:
    repo = tmp_path / dirname
    repo.mkdir(parents=True)
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    cfg = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
    subprocess.run(["git", *cfg, "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", *cfg, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *cfg, "commit", "--quiet", "-m", "init"], cwd=repo, check=True)
    return repo


@_requires_git
async def test_skills_install_scan_returns_only_valid_skills(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    repo = _git_skill_repo(
        tmp_path,
        "skillpack",
        {
            "pdf/SKILL.md": "---\nname: pdf\ndescription: Work with PDF files.\n---\n\nB.\n",
            "broken/SKILL.md": "junk, no frontmatter\n",
        },
    )
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_scan", repo_url=str(repo))

    assert payload["type"] == "skills.install_scan.ack"
    assert payload["ok"] is True
    skills = cast(list[dict[str, object]], payload["skills"])
    assert [s["name"] for s in skills] == ["pdf"]
    assert skills[0]["description"] == "Work with PDF files."


async def test_skills_install_scan_reports_git_missing(
    ws: aiohttp.ClientWebSocketResponse,
    _temp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_scan", repo_url="https://example.invalid/x")

    assert payload["ok"] is False
    assert "git" in str(payload["error"]).lower()


@_requires_git
async def test_skills_install_copies_selected_skills_and_refreshes_the_listing(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    repo = _git_skill_repo(
        tmp_path,
        "skillpack",
        {
            "pdf/SKILL.md": "---\nname: pdf\ndescription: Work with PDF files.\n---\n\nB.\n",
            "docx/SKILL.md": "---\nname: docx\ndescription: Word files.\n---\n\nB.\n",
        },
    )
    await _control_hello(ws)

    payload = await _skills_request(
        ws, "skills.install", repo_url=str(repo), install=[{"name": "pdf", "overwrite": False}]
    )

    assert payload["type"] == "skills.install.ack"
    assert payload["ok"] is True
    assert payload["installed"] == ["pdf"]
    assert payload["conflicts"] == []
    assert payload["missing"] == []
    assert (_temp_home / ".kodo" / "skills" / "pdf" / "SKILL.md").is_file()
    assert not (_temp_home / ".kodo" / "skills" / "docx").exists()
    skills = cast(list[dict[str, object]], payload["skills"])
    assert [s["name"] for s in skills] == ["pdf"], "the ack carries the refreshed listing"


@_requires_git
async def test_skills_install_reports_conflict_without_overwrite(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    _install_skill(_temp_home, "pdf", "---\nname: pdf\ndescription: Old.\n---\n\nB.\n")
    repo = _git_skill_repo(
        tmp_path, "skillpack", {"pdf/SKILL.md": "---\nname: pdf\ndescription: New.\n---\n\nB.\n"}
    )
    await _control_hello(ws)

    payload = await _skills_request(
        ws, "skills.install", repo_url=str(repo), install=[{"name": "pdf", "overwrite": False}]
    )

    assert payload["installed"] == []
    assert payload["conflicts"] == ["pdf"]
    skills = cast(list[dict[str, object]], payload["skills"])
    assert skills[0]["description"] == "Old.", (
        "an unconfirmed conflict must not touch the existing skill"
    )


@_requires_git
async def test_skills_install_overwrites_when_confirmed(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    _install_skill(_temp_home, "pdf", "---\nname: pdf\ndescription: Old.\n---\n\nB.\n")
    repo = _git_skill_repo(
        tmp_path, "skillpack", {"pdf/SKILL.md": "---\nname: pdf\ndescription: New.\n---\n\nB.\n"}
    )
    await _control_hello(ws)

    payload = await _skills_request(
        ws, "skills.install", repo_url=str(repo), install=[{"name": "pdf", "overwrite": True}]
    )

    assert payload["installed"] == ["pdf"]
    skills = cast(list[dict[str, object]], payload["skills"])
    assert skills[0]["description"] == "New."


# ---------------------------------------------------------------------------
# skills.install_local (doc/WS_PROTOCOL.md §7.6j, doc/SKILLS.md §2)
#
# No ``git`` involved — *path* is read straight off disk, so these need
# neither ``_requires_git`` nor a repo fixture.
# ---------------------------------------------------------------------------


def _local_skill_source(tmp_path: Path, name: str, text: str) -> Path:
    """A local ``SKILL.md``, elsewhere on disk, standing in for a picked file."""
    directory = tmp_path / "source" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


async def test_skills_install_local_from_a_skill_md_path(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    source = _local_skill_source(
        tmp_path, "pdf", "---\nname: pdf\ndescription: Work with PDF files.\n---\n\nB.\n"
    )
    await _control_hello(ws)

    payload = await _skills_request(
        ws, "skills.install_local", path=str(source / "SKILL.md"), overwrite=False
    )

    assert payload["type"] == "skills.install_local.ack"
    assert payload["ok"] is True
    assert payload["installed"] == ["pdf"]
    assert payload["conflicts"] == []
    assert payload["missing"] == []
    assert (_temp_home / ".kodo" / "skills" / "pdf" / "SKILL.md").is_file()
    skills = cast(list[dict[str, object]], payload["skills"])
    assert [s["name"] for s in skills] == ["pdf"], "the ack carries the refreshed listing"


async def test_skills_install_local_from_a_directory_path(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    source = _local_skill_source(
        tmp_path, "pdf", "---\nname: pdf\ndescription: Work with PDF files.\n---\n\nB.\n"
    )
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_local", path=str(source), overwrite=False)

    assert payload["ok"] is True
    assert payload["installed"] == ["pdf"]


async def test_skills_install_local_reports_conflict_without_overwrite(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    _install_skill(_temp_home, "pdf", "---\nname: pdf\ndescription: Old.\n---\n\nB.\n")
    source = _local_skill_source(tmp_path, "pdf", "---\nname: pdf\ndescription: New.\n---\n\nB.\n")
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_local", path=str(source), overwrite=False)

    assert payload["ok"] is True
    assert payload["installed"] == []
    assert payload["conflicts"] == ["pdf"]
    skills = cast(list[dict[str, object]], payload["skills"])
    assert skills[0]["description"] == "Old.", (
        "an unconfirmed conflict must not touch the existing skill"
    )


async def test_skills_install_local_overwrites_when_confirmed(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    _install_skill(_temp_home, "pdf", "---\nname: pdf\ndescription: Old.\n---\n\nB.\n")
    source = _local_skill_source(tmp_path, "pdf", "---\nname: pdf\ndescription: New.\n---\n\nB.\n")
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_local", path=str(source), overwrite=True)

    assert payload["installed"] == ["pdf"]
    skills = cast(list[dict[str, object]], payload["skills"])
    assert skills[0]["description"] == "New."


async def test_skills_install_local_fails_without_touching_the_socket_for_a_bad_path(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    await _control_hello(ws)

    payload = await _skills_request(
        ws, "skills.install_local", path=str(tmp_path / "does-not-exist"), overwrite=False
    )

    assert payload["ok"] is False
    assert "does not exist" in str(payload["error"])
    # The failure ack still carries the (empty) listing, same contract as
    # skills.install's failure ack.
    assert cast(list[dict[str, object]], payload["skills"]) == []


async def test_skills_install_local_does_not_scan_recursively(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path, tmp_path: Path
) -> None:
    """A directory bundling several skills as subdirectories is not discovered
    — unlike the repo flow's recursive scan, this requires ``SKILL.md``
    directly at the given path (doc/SKILLS.md §2)."""
    bundle = tmp_path / "bundle"
    (bundle / "pdf").mkdir(parents=True)
    (bundle / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: P.\n---\n\nB.\n", encoding="utf-8"
    )
    await _control_hello(ws)

    payload = await _skills_request(ws, "skills.install_local", path=str(bundle), overwrite=False)

    assert payload["ok"] is False
    assert "SKILL.md" in str(payload["error"])


# ---------------------------------------------------------------------------
# local_llm.hf_search — the "add a local LLM" dialog's repo search
# ---------------------------------------------------------------------------


@pytest.fixture
async def hf_models_api(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[list[str], None]:
    """A local stand-in for the Hub's ``/api/models``; yields the queries it saw."""
    import huggingface_hub
    import huggingface_hub.constants
    from aiohttp import web

    seen: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request.query.get("search", ""))
        if request.query.get("search") == "boom":
            return web.Response(status=502)
        return web.json_response(
            [
                {"id": "someone-else/Popular-GGUF", "downloads": 900, "tags": ["gguf"]},
                {"id": "ggml-org/Original-GGUF", "downloads": 1, "tags": ["license:mit"]},
            ]
        )

    app = web.Application()
    app.router.add_get("/api/models", handler)
    api = TestServer(app)
    await api.start_server()
    monkeypatch.setattr(huggingface_hub.constants, "ENDPOINT", f"http://127.0.0.1:{api.port}")
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    yield seen
    await api.close()


async def test_hf_search_replies_with_ranked_results(
    ws: aiohttp.ClientWebSocketResponse, hf_models_api: list[str]
) -> None:
    from kodo.llms.model_import import HF_TOP_PUBLISHERS

    assert "ggml-org" in HF_TOP_PUBLISHERS, (
        "this test's fake Hub reply assumes ggml-org is a top publisher; "
        "update the test if that list ever changes"
    )
    await _control_hello(ws)
    req = _make_request("local_llm.hf_search", query="original")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "local_llm.hf_search.ack"
    assert resp.payload["query"] == "original"
    assert resp.payload["error"] == ""
    results = cast("list[dict[str, object]]", resp.payload["results"])
    assert [r["repo_id"] for r in results] == [
        "ggml-org/Original-GGUF",
        "someone-else/Popular-GGUF",
    ]
    assert results[0]["license"] == "mit"
    assert hf_models_api == ["original"]


async def test_hf_search_short_query_never_reaches_the_hub(
    ws: aiohttp.ClientWebSocketResponse, hf_models_api: list[str]
) -> None:
    await _control_hello(ws)
    req = _make_request("local_llm.hf_search", query="q")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    assert resp.payload["results"] == [] and resp.payload["error"] == ""
    assert hf_models_api == []


async def test_hf_search_hub_failure_replies_with_an_error(
    ws: aiohttp.ClientWebSocketResponse, hf_models_api: list[str]
) -> None:
    await _control_hello(ws)
    req = _make_request("local_llm.hf_search", query="boom")
    await ws.send_str(req.to_json())
    resp = await _recv_response(ws, req.id)

    assert resp.payload["type"] == "local_llm.hf_search.ack"
    assert resp.payload["results"] == []
    assert "HTTP 502" in str(resp.payload["error"])
