"""Control-plane handlers of the WebSocket server app, driven over a real socket.

Covers the window-global, session-less frames the Kōdo Settings panel and the
sidebar send: session management by id, global and per-session security
rules, the stuck-detection / housekeeper-LLM / default-agent settings, the
user-installed agents and skills management, ``config.reload``, and the
``create_app`` startup checks. ``HOME`` is redirected to a temp dir, and every
startup step that would touch the network or a real llama-server is stubbed,
so nothing here leaves the machine or reads the real ``~/.kodo``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from kodo.agents import KIND_AGENT, KIND_SUBAGENT, AgentLoadError, AgentRegistry
from kodo.common import Envelope
from kodo.llms.llamacpp import LlamaServer
from kodo.security import add_global_rule
from kodo.server import Config, SessionManager, create_app
from kodo.titling import DEFAULT_HOUSEKEEPER_LLM_ID, HOUSEKEEPER_LLM_OPTIONS

_RECV_TIMEOUT = 5.0
_APP = "kodo.server._app"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def titler_starts() -> list[tuple[Path, str]]:
    """Every ``start_titling`` call the server made, in order."""
    return []


@pytest.fixture(autouse=True)
def _temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, titler_starts: list[tuple[Path, str]]
) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    async def _record_start_titling(kodo_dir: Path, model_id: str = "") -> None:
        titler_starts.append((kodo_dir, model_id))

    async def _no_op() -> None:
        return None

    async def _no_op_refresh_loop(_kodo_dir: Path) -> None:
        return None

    monkeypatch.setattr(f"{_APP}.start_titling", _record_start_titling)
    monkeypatch.setattr(f"{_APP}.stop_titling", _no_op)
    monkeypatch.setattr(f"{_APP}.run_openrouter_catalog_refresh_loop", _no_op_refresh_loop)
    monkeypatch.setattr(f"{_APP}.ensure_all_utils", lambda _kodo_dir: {})
    monkeypatch.setattr(f"{_APP}.find_running_server", lambda _kodo_dir: None)
    monkeypatch.setattr(f"{_APP}.find_installed", lambda _kodo_dir: None)
    # Other test modules can leave a server registered in LlamaServer's
    # process-wide singleton; pin it so teardown never touches a real one.
    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: None))
    return home


@pytest.fixture
async def server() -> AsyncGenerator[TestServer, None]:
    srv = TestServer(create_app(Config()))
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


async def _request(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, **payload: object
) -> dict[str, object]:
    """Send a request and return the payload of its correlated response."""
    req = Envelope(kind="request", payload={"type": msg_type, **payload})
    await ws.send_str(req.to_json())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RECV_TIMEOUT
    while True:
        env = await _recv(ws, timeout=max(0.01, deadline - loop.time()))
        if env.kind == "response" and env.correlation_id == req.id:
            return env.payload


async def _open_session(ws: aiohttp.ClientWebSocketResponse, window_id: str = "w1") -> str:
    ack = await _request(ws, "hello", client="test", window_id=window_id)
    return str(ack["session_id"])


def _settings_path(home: Path) -> Path:
    return home / ".kodo" / "etc" / "settings.json"


def _read_settings(home: Path) -> dict[str, object]:
    data = json.loads(_settings_path(home).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write_user_agent(home: Path, name: str, *, prompt_name: str | None = None) -> Path:
    """Write a top-level user agent bundle under the temp ``~/.kodo/agents``."""
    directory = home / ".kodo" / "agents" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"agent_{name}.md").write_text(
        f"---\nname: {prompt_name or name}\nversion: 1.0.0\n---\nYou are an agent.\n",
        encoding="utf-8",
    )
    (directory / f"{name}.json").write_text(
        json.dumps({"name": name, "description": f"The {name} agent.", "rank": 50}),
        encoding="utf-8",
    )
    return directory


def _agent_source(tmp_path: Path) -> Path:
    """An install source directory offering a single top-level agent."""
    src = tmp_path / "agent-src"
    src.mkdir()
    (src / "agent_reviewer.md").write_text(
        "---\nname: reviewer\nversion: 2.0.0\n---\nYou are Reviewer.\n", encoding="utf-8"
    )
    (src / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "Audits a codebase."}), encoding="utf-8"
    )
    return src


# ---------------------------------------------------------------------------
# create_app startup checks
# ---------------------------------------------------------------------------


def test_create_app_exits_when_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        create_app(Config())


async def test_a_user_agent_broken_at_startup_is_listed_with_its_error(
    _temp_home: Path,
) -> None:
    """A bundle that fails to load never stops the server — it shows up as a broken row."""
    _write_user_agent(_temp_home, "mismatch", prompt_name="other")
    srv = TestServer(create_app(Config()))
    await srv.start_server()
    session = aiohttp.ClientSession()
    try:
        conn = await session.ws_connect(f"http://127.0.0.1:{srv.port}/ws")
        listing = await _request(conn, "agents.list")
        await conn.close()
    finally:
        await session.close()
        await srv.close()

    rows = {str(r["name"]): r for r in listing["agents"]}  # type: ignore[union-attr]
    assert rows["mismatch"]["error"]
    assert rows["mismatch"]["kind"] == KIND_AGENT
    assert rows["mismatch"]["version"] == ""


# ---------------------------------------------------------------------------
# session.release / session.delete / session.delete_by_id
# ---------------------------------------------------------------------------


async def test_session_release_frees_the_session_for_another_window(server: TestServer) -> None:
    client = aiohttp.ClientSession()
    a = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    b = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid = await _open_session(a, window_id="wa")
        ack = await _request(a, "session.release", session_id=sid)
        assert ack["type"] == "session.release.ack"

        resumed = await _request(b, "hello", client="test", window_id="wb", session_id=sid)
        assert resumed.get("error") is None
        assert resumed["session_id"] == sid
    finally:
        await a.close()
        await b.close()
        await client.close()


async def test_session_release_without_an_id_still_acks(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    ack = await _request(ws, "session.release")
    assert ack == {"type": "session.release.ack"}


async def test_session_delete_failure_is_reported_without_closing_the_socket(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await _open_session(ws)

    async def _boom(self: SessionManager, session_id: str) -> None:
        raise OSError("disk is read-only")

    monkeypatch.setattr(SessionManager, "delete", _boom)
    reply = await _request(ws, "session.delete", session_id=sid)
    assert reply == {"type": "session.delete.error", "message": "disk is read-only"}
    # Still open: another request on the same socket is answered.
    assert (await _request(ws, "session.release"))["type"] == "session.release.ack"


async def test_session_delete_by_id_requires_an_id(ws: aiohttp.ClientWebSocketResponse) -> None:
    reply = await _request(ws, "session.delete_by_id")
    assert reply["type"] == "session.delete_by_id.error"
    assert reply["message"]


async def test_session_delete_by_id_removes_the_session_from_the_listing(
    server: TestServer,
) -> None:
    client = aiohttp.ClientSession()
    owner = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    control = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        sid = await _open_session(owner)
        reply = await _request(control, "session.delete_by_id", session_id=sid)
        assert reply == {"type": "session.delete_by_id.ack", "session_id": sid}
        listing = await _request(control, "session.list")
        assert sid not in {s["id"] for s in listing["sessions"]}  # type: ignore[union-attr]
    finally:
        await owner.close()
        await control.close()
        await client.close()


async def test_session_delete_by_id_failure_replies_with_the_error(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: SessionManager, session_id: str) -> None:
        raise RuntimeError("cannot delete")

    monkeypatch.setattr(SessionManager, "delete", _boom)
    reply = await _request(ws, "session.delete_by_id", session_id="20260101-000000")
    assert reply == {"type": "session.delete_by_id.error", "message": "cannot delete"}


# ---------------------------------------------------------------------------
# Global and session-scoped security rules
# ---------------------------------------------------------------------------


async def test_global_security_rules_are_listed_and_deleted(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    add_global_rule("git", "status")
    add_global_rule("npm", "test")

    listing = await _request(ws, "security.rules.list")
    assert listing["type"] == "security.rules.list.ack"
    assert {"kind": "command", "executable": "git", "value": "status"} in listing["rules"]  # type: ignore[operator]

    reply = await _request(
        ws,
        "security.rules.delete",
        rules=[{"kind": "command", "executable": "git", "value": "status"}, "not-a-rule"],
    )
    assert reply["type"] == "security.rules.delete.ack"
    assert reply["rules"] == [{"kind": "command", "executable": "npm", "value": "test"}]


async def test_global_security_rules_delete_ignores_a_non_list_payload(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    add_global_rule("git", "status")
    reply = await _request(ws, "security.rules.delete", rules="everything")
    assert reply["rules"] == [{"kind": "command", "executable": "git", "value": "status"}]


async def test_session_security_rules_for_a_session(ws: aiohttp.ClientWebSocketResponse) -> None:
    sid = await _open_session(ws)
    listing = await _request(ws, "session.security_rules.list", session_id=sid)
    assert listing == {"type": "session.security_rules.list.ack", "rules": []}

    reply = await _request(
        ws,
        "session.security_rules.delete",
        session_id=sid,
        rules=[{"kind": "command", "executable": "git", "value": "push"}],
    )
    assert reply == {"type": "session.security_rules.delete.ack", "rules": []}


async def test_session_security_rules_without_a_session_id_are_empty(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    listing = await _request(ws, "session.security_rules.list")
    assert listing["rules"] == []
    reply = await _request(ws, "session.security_rules.delete", rules="nope")
    assert reply["rules"] == []


# ---------------------------------------------------------------------------
# stuck_detection.get / stuck_detection.set
# ---------------------------------------------------------------------------


async def test_stuck_detection_get_reports_the_defaults(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    reply = await _request(ws, "stuck_detection.get")
    assert reply["type"] == "stuck_detection.get.ack"
    assert reply["active"] in ("off", "local_only", "local_and_cloud")
    assert reply["scope"] in ("top_level", "top_level_and_subagents")
    assert isinstance(reply["auto_unstuck_interactive"], bool)


async def test_stuck_detection_set_persists_and_is_read_back(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    reply = await _request(
        ws,
        "stuck_detection.set",
        active="local_and_cloud",
        scope="top_level",
        auto_unstuck_interactive=True,
    )
    expected = {"active": "local_and_cloud", "scope": "top_level", "auto_unstuck_interactive": True}
    assert reply == {"type": "stuck_detection.set.ack", **expected}
    assert _read_settings(_temp_home)["stuck_detection"] == expected

    read_back = await _request(ws, "stuck_detection.get")
    assert read_back == {"type": "stuck_detection.get.ack", **expected}


async def test_stuck_detection_set_clamps_unknown_values_to_defaults(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    reply = await _request(ws, "stuck_detection.set", active="always", scope="galaxy")
    assert reply["active"] == "local_only"
    assert reply["scope"] == "top_level_and_subagents"
    assert reply["auto_unstuck_interactive"] is False


async def test_stuck_detection_set_rewrites_an_unreadable_settings_file(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _settings_path(_temp_home).write_text("{not json", encoding="utf-8")
    await _request(ws, "stuck_detection.set", active="off")
    assert _read_settings(_temp_home) == {
        "stuck_detection": {
            "active": "off",
            "scope": "top_level_and_subagents",
            "auto_unstuck_interactive": False,
        }
    }


async def test_stuck_detection_set_keeps_unrelated_settings(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _settings_path(_temp_home).write_text(json.dumps({"mode": "local"}), encoding="utf-8")
    await _request(ws, "stuck_detection.set", active="off")
    assert _read_settings(_temp_home)["mode"] == "local"


# ---------------------------------------------------------------------------
# housekeeper_llm.get / housekeeper_llm.set
# ---------------------------------------------------------------------------


async def test_housekeeper_llm_get_lists_every_option_and_the_default(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    reply = await _request(ws, "housekeeper_llm.get")
    assert reply["type"] == "housekeeper_llm.get.ack"
    assert reply["selected"] == DEFAULT_HOUSEKEEPER_LLM_ID
    assert [o["id"] for o in reply["options"]] == list(HOUSEKEEPER_LLM_OPTIONS)  # type: ignore[union-attr]


async def test_housekeeper_llm_set_rejects_an_unknown_id(
    ws: aiohttp.ClientWebSocketResponse, titler_starts: list[tuple[Path, str]]
) -> None:
    reply = await _request(ws, "housekeeper_llm.set", id="no-such-model")
    assert reply["ok"] is False
    assert "no-such-model" in str(reply["error"])
    assert titler_starts == []


async def test_housekeeper_llm_set_persists_and_restarts_the_titler(
    ws: aiohttp.ClientWebSocketResponse,
    _temp_home: Path,
    titler_starts: list[tuple[Path, str]],
) -> None:
    choice = next(i for i in HOUSEKEEPER_LLM_OPTIONS if i != DEFAULT_HOUSEKEEPER_LLM_ID)
    _settings_path(_temp_home).write_text(json.dumps({"mode": "local"}), encoding="utf-8")
    reply = await _request(ws, "housekeeper_llm.set", id=choice)
    assert reply == {"type": "housekeeper_llm.set.ack", "ok": True, "selected": choice}
    assert _read_settings(_temp_home) == {"mode": "local", "housekeeper_llm": choice}

    read_back = await _request(ws, "housekeeper_llm.get")
    assert read_back["selected"] == choice
    await asyncio.sleep(0)
    assert [model for _, model in titler_starts] == [choice]


async def test_housekeeper_llm_set_rewrites_an_unreadable_settings_file(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _settings_path(_temp_home).write_text("{oops", encoding="utf-8")
    await _request(ws, "housekeeper_llm.set", id=DEFAULT_HOUSEKEEPER_LLM_ID)
    assert _read_settings(_temp_home) == {"housekeeper_llm": DEFAULT_HOUSEKEEPER_LLM_ID}


async def test_default_agent_set_rewrites_an_unreadable_settings_file(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _settings_path(_temp_home).write_text("{oops", encoding="utf-8")
    reply = await _request(ws, "default_agent.set", name="")
    assert reply["ok"] is True
    assert _read_settings(_temp_home) == {"default_agent": ""}


# ---------------------------------------------------------------------------
# agents.* — user-installed agent management
# ---------------------------------------------------------------------------


async def test_agents_list_reports_installed_agents_and_the_root(
    server: TestServer, _temp_home: Path
) -> None:
    _write_user_agent(_temp_home, "helper")
    client = aiohttp.ClientSession()
    conn = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        await _request(conn, "agents.reload")
        listing = await _request(conn, "agents.list")
    finally:
        await conn.close()
        await client.close()

    assert listing["ok"] is True
    assert listing["root"] == str(_temp_home / ".kodo" / "agents")
    row = next(r for r in listing["agents"] if r["name"] == "helper")  # type: ignore[union-attr]
    assert row["kind"] == KIND_AGENT
    assert row["version"] == "1.0.0"
    assert row["description"] == "The helper agent."
    assert row["error"] == ""


async def test_agents_delete_removes_an_installed_agent(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    bundle = _write_user_agent(_temp_home, "helper")
    await _request(ws, "agents.reload")

    reply = await _request(ws, "agents.delete", name="helper", kind=KIND_AGENT)
    assert reply["type"] == "agents.delete.ack"
    assert reply["ok"] is True
    assert reply["error"] == ""
    assert not bundle.exists()
    assert "helper" not in {r["name"] for r in reply["agents"]}  # type: ignore[union-attr]


async def test_agents_delete_of_something_not_installed_still_lists(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    reply = await _request(ws, "agents.delete", name="ghost", kind=KIND_SUBAGENT)
    assert reply["ok"] is False
    assert reply["error"]
    assert isinstance(reply["agents"], list)


async def test_agents_install_scan_reports_the_candidates(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    reply = await _request(ws, "agents.install_scan", source=str(_agent_source(tmp_path)))
    assert reply["ok"] is True
    assert reply["candidates"] == [
        {
            "name": "reviewer",
            "kind": KIND_AGENT,
            "version": "2.0.0",
            "installed_version": "",
            "error": "",
        }
    ]
    assert reply["conflicts"] == ""


async def test_agents_install_scan_of_a_missing_source_fails(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    reply = await _request(ws, "agents.install_scan", source=str(tmp_path / "nope"))
    assert reply["ok"] is False
    assert reply["error"]


async def test_agents_install_installs_and_lists_the_new_agent(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path, _temp_home: Path
) -> None:
    reply = await _request(
        ws,
        "agents.install",
        source=str(_agent_source(tmp_path)),
        replace=False,
        names=["reviewer"],
    )
    assert reply["ok"] is True
    assert reply["installed"] == ["reviewer"]
    assert reply["missing"] == []
    assert (_temp_home / ".kodo" / "agents" / "reviewer" / "agent_reviewer.md").is_file()
    assert "reviewer" in {r["name"] for r in reply["agents"]}  # type: ignore[union-attr]

    picker = await _request(ws, "top_agents.list")
    assert "reviewer" in {a["name"] for a in picker["agents"]}  # type: ignore[union-attr]


async def test_agents_install_of_a_missing_source_fails(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    reply = await _request(ws, "agents.install", source=str(tmp_path / "nope"))
    assert reply == {
        "type": "agents.install.ack",
        "ok": False,
        "error": reply["error"],
    }
    assert reply["error"]


async def test_agents_reload_failure_keeps_the_previous_set_and_reports_it(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await _request(ws, "top_agents.list")

    def _fail(self: AgentRegistry) -> object:
        raise AgentLoadError("packaged agent is broken")

    monkeypatch.setattr(AgentRegistry, "reload", _fail)
    reply = await _request(ws, "agents.reload")
    assert reply["ok"] is False
    assert "packaged agent is broken" in str(reply["error"])

    after = await _request(ws, "top_agents.list")
    assert after["agents"] == before["agents"]


# ---------------------------------------------------------------------------
# skills.install — failure path
# ---------------------------------------------------------------------------


async def test_skills_install_reports_git_missing_with_the_listing(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch, _temp_home: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    reply = await _request(
        ws,
        "skills.install",
        repo_url="https://example.invalid/skills.git",
        install=[{"name": "x", "overwrite": True}, "junk"],
    )
    assert reply["type"] == "skills.install.ack"
    assert reply["ok"] is False
    assert reply["error"]
    assert reply["root"] == str(_temp_home / ".kodo" / "skills")
    assert reply["skills"] == []


# ---------------------------------------------------------------------------
# config.reload
# ---------------------------------------------------------------------------


async def test_config_reload_notifies_live_sessions_and_acks(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _open_session(ws)
    reply = await _request(ws, "config.reload")
    assert reply == {"type": "config.reload.ack"}


async def test_config_reload_failure_is_a_recoverable_error(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self: Config) -> dict[str, object]:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(Config, "reload_settings", _boom)
    reply = await _request(ws, "config.reload")
    assert reply == {
        "type": "error",
        "code": "config_reload_failed",
        "message": "settings exploded",
        "recoverable": True,
    }
