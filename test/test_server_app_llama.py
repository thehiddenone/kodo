"""llama.cpp and local-model handlers of the WebSocket server app, plus its
startup/shutdown hooks, driven over a real socket.

Nothing here downloads, installs or launches anything: every llama.cpp
installer call, the llama-server process manager and the model manager's
transfer methods are replaced with in-memory fakes, and ``HOME`` points at a
temp dir. What is under test is the server's side of each exchange — which
frames it sends, in what order, and what it tells the other components to do.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import cast

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestServer
from huggingface_hub.errors import GatedRepoError

from kodo.common import Envelope
from kodo.llms import DEFAULT_BEDROCK_REGION, LocalLLMEntry
from kodo.llms.llamacpp import LlamaInstall, LlamaServer, LlamaServerConfig, RunningServer
from kodo.llms.local import LocalModelError, LocalModelManager, ModelRecord
from kodo.llms.local_registry import add_local_entry
from kodo.project import kodo_user_dir
from kodo.server import Config, create_app
from kodo.titling import DEFAULT_HOUSEKEEPER_LLM_ID, HOUSEKEEPER_LLM_OPTIONS

_RECV_TIMEOUT = 5.0
_APP = "kodo.server._app"


class _FakeLlamaServer:
    """Stands in for a managed llama-server: records stops, never touches a process."""

    def __init__(self, model_name: str = "", *, running: bool = True, port: int = 8042) -> None:
        self.model_name = model_name
        self.port = port
        self.is_running = running
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1
        self.is_running = False


class _Recorder:
    """Every ``start_titling`` call, and whatever fake is the active llama-server."""

    def __init__(self) -> None:
        self.titler_starts: list[str] = []
        self.active: _FakeLlamaServer | None = None


@pytest.fixture
def rec() -> _Recorder:
    return _Recorder()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    async def _record_start_titling(_kodo_dir: Path, model_id: str = "") -> None:
        rec.titler_starts.append(model_id)

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
    monkeypatch.setattr(LlamaServer, "get_active_llama_server", classmethod(lambda cls: rec.active))
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


async def _send(ws: aiohttp.ClientWebSocketResponse, msg_type: str, **payload: object) -> str:
    req = Envelope(kind="request", payload={"type": msg_type, **payload})
    await ws.send_str(req.to_json())
    return req.id


async def _request(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str, **payload: object
) -> dict[str, object]:
    """Send a request and return the payload of its correlated response."""
    req_id = await _send(ws, msg_type, **payload)
    while True:
        env = await _recv(ws)
        if env.kind == "response" and env.correlation_id == req_id:
            return env.payload


async def _event(
    ws: aiohttp.ClientWebSocketResponse, *, hf_token: str | None = "", hf_error: str = ""
) -> dict[str, object]:
    """Return the next event, answering any ``hf_token.request`` on the way."""
    while True:
        env = await _recv(ws)
        if env.kind == "request" and env.payload.get("type") == "hf_token.request":
            body: dict[str, object] = {"error": hf_error} if hf_error else {"hf_token": hf_token}
            await ws.send_str(
                Envelope(kind="response", correlation_id=env.id, payload=body).to_json()
            )
            continue
        assert env.kind == "event", env
        return env.payload


async def _assert_silent(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Nothing was sent for the previous request: the next frame answers this one."""
    assert (await _request(ws, "session.release")) == {"type": "session.release.ack"}


def _settings_path(home: Path) -> Path:
    return home / ".kodo" / "etc" / "settings.json"


def _select_local_model(home: Path, name: str) -> None:
    _settings_path(home).write_text(json.dumps({"models": {"local": name}}), encoding="utf-8")


def _entry(payload: dict[str, object], name: str) -> dict[str, object]:
    registry = cast("list[dict[str, object]]", payload["local_registry"])
    return next(e for e in registry if e["name"] == name)


def _names(payload: dict[str, object]) -> set[str]:
    return {str(e["name"]) for e in cast("list[dict[str, object]]", payload["local_registry"])}


async def _add_hf(ws: aiohttp.ClientWebSocketResponse, name: str = "hf-model") -> None:
    """Seed a ``custom_hf`` entry and re-read the registry.

    No message creates one any more (``local_llm.add_huggingface`` was removed
    for the Model Importer agent), but entries added before then still load,
    download and remove — which is what the tests using this exercise.
    """
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
    await _send(ws, "local_llm.registry_get")
    assert (await _event(ws))["type"] == "local_llm.registry_state"


async def _add_file(
    ws: aiohttp.ClientWebSocketResponse, path: Path, name: str = "file-model"
) -> dict[str, object]:
    path.write_bytes(b"GGUF")
    await _send(ws, "local_llm.add_file", name=name, path=str(path))
    state = await _event(ws)
    assert state["type"] == "local_llm.registry_state"
    return state


def _install(build: int) -> LlamaInstall:
    return LlamaInstall(
        build=build, install_dir=Path(f"/fake/b{build}"), executable=Path("/fake/llama-server")
    )


# ---------------------------------------------------------------------------
# llamacpp.install / update / uninstall / version_info
# ---------------------------------------------------------------------------


async def test_llamacpp_install_streams_progress_then_starts_the_titler(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    _temp_home: Path,
) -> None:
    choice = next(i for i in HOUSEKEEPER_LLM_OPTIONS if i != DEFAULT_HOUSEKEEPER_LLM_ID)
    _settings_path(_temp_home).write_text(json.dumps({"housekeeper_llm": choice}), "utf-8")

    def _install_llamacpp(_dir: Path, *, progress_cb: Callable[[int, str], None]) -> LlamaInstall:
        progress_cb(40, "downloading")
        progress_cb(100, "done")
        return _install(1)

    monkeypatch.setattr(f"{_APP}.install_llamacpp", _install_llamacpp)
    await _send(ws, "llamacpp.install")
    assert (await _event(ws))["percent"] == 40
    assert (await _event(ws))["percent"] == 100
    await _assert_silent(ws)
    assert rec.titler_starts == [choice]


@pytest.mark.parametrize("settings_text", ["{broken", json.dumps({"housekeeper_llm": "gone"})])
async def test_llamacpp_install_falls_back_to_the_default_titler_model(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    _temp_home: Path,
    settings_text: str,
) -> None:
    _settings_path(_temp_home).write_text(settings_text, encoding="utf-8")

    def _install_llamacpp(_dir: Path, *, progress_cb: Callable[[int, str], None]) -> LlamaInstall:
        progress_cb(100, "done")
        return _install(1)

    monkeypatch.setattr(f"{_APP}.install_llamacpp", _install_llamacpp)
    await _send(ws, "llamacpp.install")
    assert (await _event(ws))["percent"] == 100
    await _assert_silent(ws)
    assert rec.titler_starts == [DEFAULT_HOUSEKEEPER_LLM_ID]


async def test_llamacpp_update_rejects_a_malformed_version(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "llamacpp.update", version="latest-please")
    evt = await _event(ws)
    assert evt["type"] == "llamacpp.install.progress"
    assert evt["percent"] == -1
    assert "latest-please" in str(evt["message"])


async def test_llamacpp_update_reports_a_failed_latest_version_check(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _offline() -> int:
        raise ConnectionError("github unreachable")

    monkeypatch.setattr(f"{_APP}.fetch_latest_build_number", _offline)
    await _send(ws, "llamacpp.update")
    evt = await _event(ws)
    assert evt["percent"] == -1
    assert "github unreachable" in str(evt["message"])


async def test_llamacpp_update_when_already_latest_is_a_no_op(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    def _must_not_run(*_a: object, **_k: object) -> object:
        raise AssertionError("an up-to-date install must be left alone")

    monkeypatch.setattr(f"{_APP}.find_installed", lambda _dir: _install(500))
    monkeypatch.setattr(f"{_APP}.fetch_latest_build_number", lambda: 400)
    monkeypatch.setattr(f"{_APP}.update_llamacpp", _must_not_run)
    await _send(ws, "llamacpp.update")
    evt = await _event(ws)
    assert evt["percent"] == 100
    assert evt["up_to_date"] is True
    assert "b500" in str(evt["message"])


async def test_llamacpp_update_to_a_build_github_does_not_have_fails_early(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{_APP}.build_exists", lambda _build: False)
    await _send(ws, "llamacpp.update", version=12345)
    evt = await _event(ws)
    assert evt["percent"] == -1
    assert "b12345" in str(evt["message"])


async def test_llamacpp_update_to_a_pinned_build_installs_it(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    versions: list[int | None] = []

    def _update(
        _dir: Path, *, version: int | None, progress_cb: Callable[[int, str], None]
    ) -> LlamaInstall:
        versions.append(version)
        progress_cb(100, "installed")
        return _install(version or 0)

    monkeypatch.setattr(f"{_APP}.find_installed", lambda _dir: _install(500))
    monkeypatch.setattr(f"{_APP}.build_exists", lambda _build: True)
    monkeypatch.setattr(f"{_APP}.update_llamacpp", _update)
    await _send(ws, "llamacpp.update", version="b400")
    assert (await _event(ws))["percent"] == 100
    await _assert_silent(ws)
    assert versions == [400]
    assert rec.titler_starts == [DEFAULT_HOUSEKEEPER_LLM_ID]


async def test_llamacpp_uninstall_stops_the_chat_server_and_acks(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch, rec: _Recorder
) -> None:
    removed: list[Path] = []
    rec.active = _FakeLlamaServer("m")
    monkeypatch.setattr(f"{_APP}.uninstall_llamacpp", removed.append)

    req_id = await _send(ws, "llamacpp.uninstall")
    state = await _recv(ws)
    assert state.payload == {"type": "llama.state", "running": False, "model": None}
    ack = await _recv(ws)
    assert ack.correlation_id == req_id
    assert ack.payload == {
        "type": "llamacpp.uninstall.ack",
        "llama_installed": False,
        "llama_version": None,
    }
    assert rec.active.stops == 1
    assert len(removed) == 1


async def test_llamacpp_uninstall_failure_reports_what_is_still_on_disk(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _locked(_dir: Path) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr(f"{_APP}.uninstall_llamacpp", _locked)
    monkeypatch.setattr(f"{_APP}.find_installed", lambda _dir: _install(777))

    req_id = await _send(ws, "llamacpp.uninstall")
    error = await _recv(ws)
    assert error.payload["type"] == "error"
    assert error.payload["code"] == "llamacpp_uninstall_failed"
    assert "file in use" in str(error.payload["message"])
    assert (await _recv(ws)).payload["type"] == "llama.state"
    ack = await _recv(ws)
    assert ack.correlation_id == req_id
    assert ack.payload["llama_installed"] is True
    assert ack.payload["llama_version"] == "b777"


async def test_llamacpp_version_info_reports_installed_and_latest(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{_APP}.find_installed", lambda _dir: _install(100))
    monkeypatch.setattr(f"{_APP}.fetch_latest_build_number", lambda: 200)
    reply = await _request(ws, "llamacpp.version_info")
    assert reply == {
        "type": "llamacpp.version_info.ack",
        "installed_version": "b100",
        "latest_version": "b200",
        "error": None,
    }


async def test_llamacpp_version_info_reports_a_lookup_failure(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _offline() -> int:
        raise ConnectionError("offline")

    monkeypatch.setattr(f"{_APP}.fetch_latest_build_number", _offline)
    reply = await _request(ws, "llamacpp.version_info")
    assert reply["installed_version"] is None
    assert reply["latest_version"] is None
    assert reply["error"] == "offline"


# ---------------------------------------------------------------------------
# HF token round trip for background downloads
# ---------------------------------------------------------------------------


class _TokenRecordingDownloads:
    """Records the token every ``download_model`` call received."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tokens: list[str | None] = []
        self.done = asyncio.Event()

        async def _download(
            _self: LocalModelManager,
            model_id: str,
            *a: object,
            token: str | None = None,
            **k: object,
        ) -> None:
            self.tokens.append(token)
            self.done.set()

        monkeypatch.setattr(LocalModelManager, "download_model", _download)


async def test_download_uses_the_token_the_extension_supplies(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = _TokenRecordingDownloads(monkeypatch)
    await _add_hf(ws)
    await _send(ws, "local_llm.install", name="hf-model")
    assert (await _event(ws, hf_token="hf_secret"))["type"] == "local_llm.registry_state"
    assert (await _event(ws, hf_token="hf_secret"))["type"] == "local_llm.registry_state"
    assert downloads.tokens == ["hf_secret"]


async def test_download_proceeds_without_a_token_when_the_extension_declines(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = _TokenRecordingDownloads(monkeypatch)
    await _add_hf(ws)
    await _send(ws, "local_llm.install", name="hf-model")
    await _event(ws, hf_error="cancelled")
    await _event(ws, hf_error="cancelled")
    assert downloads.tokens == [None]


async def test_download_on_a_session_connection_never_asks_for_a_token(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = _TokenRecordingDownloads(monkeypatch)
    await _add_hf(ws)
    ack = await _request(ws, "hello", client="test", window_id="w1")
    sid = str(ack["session_id"])

    await _send(ws, "local_llm.install", name="hf-model", session_id=sid)
    await asyncio.wait_for(downloads.done.wait(), timeout=_RECV_TIMEOUT)
    downloads.done.clear()
    # Same again without the explicit session_id: the connection itself is bound.
    await _send(ws, "local_llm.install", name="hf-model")
    await asyncio.wait_for(downloads.done.wait(), timeout=_RECV_TIMEOUT)
    assert downloads.tokens == [None, None]


async def test_download_survives_the_requesting_window_closing_mid_token_request(
    server: TestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = _TokenRecordingDownloads(monkeypatch)
    client = aiohttp.ClientSession()
    conn = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    try:
        await _add_hf(conn)
        await _send(conn, "local_llm.install", name="hf-model")
        while True:
            env = await _recv(conn)
            if env.kind == "request" and env.payload.get("type") == "hf_token.request":
                break
        await conn.close()
        await asyncio.wait_for(downloads.done.wait(), timeout=_RECV_TIMEOUT)
    finally:
        await client.close()
    assert downloads.tokens == [None]


class _ImpatientAsyncio:
    """The real :mod:`asyncio`, except every ``wait_for`` gives up after 50 ms."""

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)

    @staticmethod
    async def wait_for(awaitable: Awaitable[object], timeout: float | None) -> object:
        return await asyncio.wait_for(awaitable, timeout=0.05)


async def test_download_proceeds_without_a_token_when_the_extension_never_answers(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = _TokenRecordingDownloads(monkeypatch)
    await _add_hf(ws)
    monkeypatch.setattr(f"{_APP}.asyncio", _ImpatientAsyncio())
    await _send(ws, "local_llm.install", name="hf-model")
    # The token request goes unanswered; the download starts anyway.
    await asyncio.wait_for(downloads.done.wait(), timeout=_RECV_TIMEOUT)
    assert downloads.tokens == [None]


async def test_a_gated_repo_rejection_revokes_the_token_and_reports_the_error(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _gated(_self: LocalModelManager, *a: object, **k: object) -> None:
        response = httpx.Response(403, request=httpx.Request("GET", "https://hf.invalid/x"))
        try:
            raise GatedRepoError("gated", response=response)
        except GatedRepoError as exc:
            raise LocalModelError("access denied") from exc

    monkeypatch.setattr(LocalModelManager, "download_model", _gated)
    await _add_hf(ws)
    await _send(ws, "local_llm.install", name="hf-model")
    assert (await _event(ws))["type"] == "local_llm.registry_state"  # kickoff
    assert (await _event(ws))["type"] == "hf_token.revoke"
    error = await _event(ws)
    assert error["code"] == "local_llm_error"
    assert "access denied" in str(error["message"])
    assert (await _event(ws))["type"] == "local_llm.registry_state"


# ---------------------------------------------------------------------------
# local_llm.install / resume / pause / update / check_updates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg_type", ["local_llm.install", "local_llm.resume", "local_llm.update"])
async def test_download_commands_without_a_name_do_nothing(
    ws: aiohttp.ClientWebSocketResponse, msg_type: str
) -> None:
    await _send(ws, msg_type, name="  ")
    await _assert_silent(ws)


async def test_check_updates_without_names_does_nothing(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "local_llm.check_updates", names="not-a-list")
    await _assert_silent(ws)


async def test_install_of_an_unknown_model_is_an_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "local_llm.install", name="ghost")
    error = await _event(ws)
    assert error["type"] == "error"
    assert "ghost" in str(error["message"])


async def test_resume_without_a_download_record_is_an_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "local_llm.resume", name="ghost")
    error = await _event(ws)
    assert error["code"] == "local_llm_error"
    assert "nothing to resume" in str(error["message"])


async def test_resume_continues_a_recorded_download(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumed: list[str] = []
    record = ModelRecord(
        model_id="hf-model", repo_id="acme/hf-model", revision="main", commit_hash=None
    )

    async def _resume(_self: LocalModelManager, name: str, *, token: str | None = None) -> None:
        resumed.append(name)

    monkeypatch.setattr(LocalModelManager, "get_record", lambda _self, name: record)
    monkeypatch.setattr(LocalModelManager, "resume_download", _resume)
    monkeypatch.setattr(
        LocalModelManager,
        "get_model_path",
        lambda _self, name: Path("/fake/hf.gguf") if resumed else None,
    )
    await _add_hf(ws)
    await _send(ws, "local_llm.resume", name="hf-model")
    assert _entry(await _event(ws), "hf-model")["installed"] is False
    assert _entry(await _event(ws), "hf-model")["installed"] is True
    assert resumed == ["hf-model"]


async def test_pause_signals_the_manager_and_pushes_registry_state(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused: list[str] = []
    monkeypatch.setattr(LocalModelManager, "pause_download", lambda _self, n: paused.append(n))
    await _send(ws, "local_llm.pause", name="hf-model")
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    await _send(ws, "local_llm.pause", name="")
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    assert paused == ["hf-model"]


# ---------------------------------------------------------------------------
# Catalog refreshes
# ---------------------------------------------------------------------------


async def test_openrouter_refresh_replies_with_the_cached_catalog(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[Path] = []

    async def _refresh(kodo_dir: Path) -> None:
        refreshed.append(kodo_dir)

    monkeypatch.setattr(f"{_APP}.refresh_openrouter_catalog", _refresh)
    reply = await _request(ws, "openrouter.models.refresh")
    assert reply == {"type": "openrouter.models.refresh.ack", "models": []}
    assert len(refreshed) == 1


async def test_bedrock_refresh_uses_the_request_credentials_and_default_region(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    async def _refresh(_kodo_dir: Path, api_key: str, region: str) -> None:
        calls.append((api_key, region))

    monkeypatch.setattr(f"{_APP}.refresh_bedrock_catalog", _refresh)
    reply = await _request(ws, "bedrock.models.refresh", api_key="k", region="")
    assert reply == {
        "type": "bedrock.models.refresh.ack",
        "models": [],
        "region": DEFAULT_BEDROCK_REGION,
    }
    assert calls == [("k", DEFAULT_BEDROCK_REGION)]


# ---------------------------------------------------------------------------
# Adding and removing custom entries
# ---------------------------------------------------------------------------


async def test_registry_get_replies_with_the_current_registry(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    add_local_entry(
        kodo_user_dir(),
        LocalLLMEntry(name="seeded", kind="custom_hf", repo_id="acme/a", filename="a.gguf"),
    )
    await _send(ws, "local_llm.registry_get")
    assert "seeded" in _names(await _event(ws))


async def test_add_file_treats_an_unparseable_context_window_as_unset(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    model = tmp_path / "a.gguf"
    model.write_bytes(b"GGUF")
    await _send(ws, "local_llm.add_file", name="file-model", path=str(model), context_window="lots")
    assert _entry(await _event(ws), "file-model")["context_window"] == 0


async def test_add_file_lists_an_installed_entry_at_its_path(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    model = tmp_path / "local.gguf"
    entry = _entry(await _add_file(ws, model), "file-model")
    assert entry["kind"] == "custom_file"
    assert entry["installed"] is True
    assert entry["installed_path"] == str(model)


async def test_add_file_whose_file_is_gone_is_not_installed(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    await _send(ws, "local_llm.add_file", name="file-model", path=str(tmp_path / "missing.gguf"))
    entry = _entry(await _event(ws), "file-model")
    assert entry["installed"] is False
    assert entry["installed_path"] is None


async def test_add_file_requires_name_and_path(ws: aiohttp.ClientWebSocketResponse) -> None:
    await _send(ws, "local_llm.add_file", name="file-model", path="")
    assert "required" in str((await _event(ws))["message"])


async def test_add_file_rejects_a_duplicate_name(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    await _add_file(ws, tmp_path / "a.gguf")
    await _send(ws, "local_llm.add_file", name="file-model", path=str(tmp_path / "a.gguf"))
    assert "already exists" in str((await _event(ws))["message"])


async def test_add_server_url_is_always_installed_and_has_no_local_path(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "local_llm.add_server_url", name="remote", url="http://10.0.0.2:8080")
    entry = _entry(await _event(ws), "remote")
    assert entry["kind"] == "custom_server_url"
    assert entry["installed"] is True
    assert entry["installed_path"] is None
    assert entry["default_profile_args"] == {}


async def test_add_server_url_requires_name_and_url(ws: aiohttp.ClientWebSocketResponse) -> None:
    await _send(ws, "local_llm.add_server_url", name="remote", url="")
    assert "required" in str((await _event(ws))["message"])


async def test_add_server_url_rejects_a_duplicate_name(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    for _ in range(2):
        await _send(ws, "local_llm.add_server_url", name="remote", url="http://h:1")
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    assert "already exists" in str((await _event(ws))["message"])


async def test_remove_drops_a_custom_entry(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    await _add_file(ws, tmp_path / "a.gguf")
    await _send(ws, "local_llm.remove", name="file-model")
    assert "file-model" not in _names(await _event(ws))


async def test_remove_of_a_downloadable_entry_also_deletes_its_download(
    ws: aiohttp.ClientWebSocketResponse, monkeypatch: pytest.MonkeyPatch
) -> None:
    uninstalled: list[str] = []
    record = ModelRecord(
        model_id="hf-model", repo_id="acme/hf-model", revision="main", commit_hash=None
    )
    monkeypatch.setattr(LocalModelManager, "get_record", lambda _self, name: record)
    monkeypatch.setattr(LocalModelManager, "uninstall", lambda _self, n: uninstalled.append(n))
    await _add_hf(ws)
    await _send(ws, "local_llm.remove", name="hf-model")
    assert "hf-model" not in _names(await _event(ws))
    assert uninstalled == ["hf-model"]


async def test_remove_of_an_unknown_entry_is_an_error(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    await _send(ws, "local_llm.remove", name="ghost")
    error = await _event(ws)
    assert error["type"] == "error"
    assert "ghost" in str(error["message"])


# ---------------------------------------------------------------------------
# llama-server override binary
# ---------------------------------------------------------------------------


async def test_llama_server_override_set_and_remove(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    await _send(ws, "llama_server_override.set", path=str(binary))
    assert (await _event(ws))["llama_server_override_path"] == str(binary)

    await _send(ws, "llama_server_override.remove")
    assert (await _event(ws))["llama_server_override_path"] is None


async def test_llama_server_override_set_rejects_a_missing_file(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path
) -> None:
    await _send(ws, "llama_server_override.set", path=str(tmp_path / "nope"))
    error = await _event(ws)
    assert error["type"] == "error"
    assert "nope" in str(error["message"])


# ---------------------------------------------------------------------------
# Restarting a running llama-server after a profile change
# ---------------------------------------------------------------------------


async def _file_model_with_profile(
    ws: aiohttp.ClientWebSocketResponse, tmp_path: Path, home: Path
) -> str:
    """Select a custom_file model, give it an active profile, return the profile id."""
    await _add_file(ws, tmp_path / "a.gguf")
    _select_local_model(home, "file-model")
    await _send(ws, "local_llm.add_profile", name="file-model", profile_name="Tight")
    profiles = cast("list[dict[str, object]]", _entry(await _event(ws), "file-model")["profiles"])
    profile_id = str(profiles[0]["id"])
    await _send(ws, "local_llm.set_active_profile", name="file-model", profile_id=profile_id)
    assert _entry(await _event(ws), "file-model")["active_profile"] == profile_id
    return profile_id


async def test_editing_the_active_profile_restarts_the_running_server(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    profile_id = await _file_model_with_profile(ws, tmp_path, _temp_home)
    rec.active = _FakeLlamaServer("file-model")
    relaunched = _FakeLlamaServer("file-model", port=9000)

    async def _ensure(_entry: object, _kodo_dir: Path) -> _FakeLlamaServer:
        return relaunched

    monkeypatch.setattr(f"{_APP}.ensure_llama_running", _ensure)
    await _send(
        ws,
        "local_llm.update_profile",
        name="file-model",
        profile_id=profile_id,
        profile_name="Tighter",
    )
    state = await _event(ws)
    assert state == {"type": "llama.state", "running": True, "model": "file-model", "port": 9000}
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    assert rec.active.stops == 1


async def test_a_failed_restart_is_reported_as_a_stopped_server(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    profile_id = await _file_model_with_profile(ws, tmp_path, _temp_home)
    rec.active = _FakeLlamaServer("file-model")

    async def _ensure(_entry: object, _kodo_dir: Path) -> _FakeLlamaServer:
        raise RuntimeError("out of VRAM")

    monkeypatch.setattr(f"{_APP}.ensure_llama_running", _ensure)
    await _send(ws, "local_llm.remove_profile", name="file-model", profile_id=profile_id)
    state = await _event(ws)
    assert state == {"type": "llama.state", "running": False, "model": None, "error": "out of VRAM"}
    entry = _entry(await _event(ws), "file-model")
    assert entry["active_profile"] == ""


async def test_a_server_running_another_model_is_not_restarted(
    ws: aiohttp.ClientWebSocketResponse,
    rec: _Recorder,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    profile_id = await _file_model_with_profile(ws, tmp_path, _temp_home)
    rec.active = _FakeLlamaServer("some-other-model")
    await _send(ws, "local_llm.remove_profile", name="file-model", profile_id=profile_id)
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    assert rec.active.stops == 0


async def test_an_unreadable_settings_file_means_no_model_is_selected(
    ws: aiohttp.ClientWebSocketResponse,
    rec: _Recorder,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    profile_id = await _file_model_with_profile(ws, tmp_path, _temp_home)
    rec.active = _FakeLlamaServer("file-model")
    _settings_path(_temp_home).write_text("{broken", encoding="utf-8")
    await _send(ws, "local_llm.set_active_profile", name="file-model", profile_id="")
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    await _send(ws, "local_llm.set_active_profile", name="file-model", profile_id=profile_id)
    assert (await _event(ws))["type"] == "local_llm.registry_state"
    assert rec.active.stops == 0


# ---------------------------------------------------------------------------
# llama.start / llama.stop
# ---------------------------------------------------------------------------


async def test_llama_start_without_a_selected_model_reports_an_error(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _settings_path(_temp_home).write_text(json.dumps({"models": {"local": ""}}), "utf-8")
    await _send(ws, "llama.start")
    state = await _event(ws)
    assert state == {
        "type": "llama.state",
        "running": False,
        "model": None,
        "error": "No local model selected",
    }


async def test_llama_start_with_an_unknown_model_reports_an_error(
    ws: aiohttp.ClientWebSocketResponse, _temp_home: Path
) -> None:
    _select_local_model(_temp_home, "ghost")
    await _send(ws, "llama.start")
    state = await _event(ws)
    assert state["running"] is False
    assert "ghost" in str(state["error"])


async def test_llama_start_on_a_server_url_entry_stops_the_managed_server(
    ws: aiohttp.ClientWebSocketResponse, rec: _Recorder, _temp_home: Path
) -> None:
    await _send(ws, "local_llm.add_server_url", name="remote", url="http://h:1")
    await _event(ws)
    _select_local_model(_temp_home, "remote")
    rec.active = _FakeLlamaServer("file-model")
    await _send(ws, "llama.start")
    assert (await _event(ws)) == {"type": "llama.state", "running": False, "model": None}
    assert rec.active.stops == 1


async def test_llama_start_launches_the_selected_model(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    launched: list[str] = []

    async def _ensure(entry: LocalLLMEntry, _kodo_dir: Path) -> _FakeLlamaServer:
        launched.append(entry.name)
        return _FakeLlamaServer(entry.name, port=8123)

    monkeypatch.setattr(f"{_APP}.ensure_llama_running", _ensure)
    await _add_file(ws, tmp_path / "a.gguf")
    _select_local_model(_temp_home, "file-model")
    await _send(ws, "llama.start")
    assert (await _event(ws)) == {
        "type": "llama.state",
        "running": True,
        "model": "file-model",
        "port": 8123,
    }
    assert launched == ["file-model"]


async def test_llama_start_failure_is_reported(
    ws: aiohttp.ClientWebSocketResponse,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    async def _ensure(_entry: object, _kodo_dir: Path) -> _FakeLlamaServer:
        raise RuntimeError("no llama.cpp installed")

    monkeypatch.setattr(f"{_APP}.ensure_llama_running", _ensure)
    await _add_file(ws, tmp_path / "a.gguf")
    _select_local_model(_temp_home, "file-model")
    await _send(ws, "llama.start")
    state = await _event(ws)
    assert state["running"] is False
    assert state["error"] == "no llama.cpp installed"


async def test_llama_stop_stops_the_active_server(
    ws: aiohttp.ClientWebSocketResponse, rec: _Recorder
) -> None:
    rec.active = _FakeLlamaServer("m")
    await _send(ws, "llama.stop")
    assert (await _event(ws)) == {"type": "llama.state", "running": False, "model": None}
    assert rec.active.stops == 1


# ---------------------------------------------------------------------------
# Startup / shutdown hooks
# ---------------------------------------------------------------------------


class _AdoptingLlamaServer:
    """Replaces the LlamaServer class at startup: records what it is asked to adopt."""

    adopted: list[tuple[str, int, int]] = []
    active: _FakeLlamaServer | None = None

    def __init__(self, config: LlamaServerConfig) -> None:
        self.__model = config.model_name

    def adopt(self, running: RunningServer) -> None:
        type(self).adopted.append((self.__model, running.pid, running.port))

    @classmethod
    def get_active_llama_server(cls) -> _FakeLlamaServer | None:
        return cls.active


async def test_startup_adopts_a_running_server_and_spares_its_model(
    monkeypatch: pytest.MonkeyPatch, rec: _Recorder, _temp_home: Path
) -> None:
    purged_keep: list[tuple[str, ...]] = []
    running = RunningServer(pid=424242, host="127.0.0.1", port=8042, model="kept-model")
    _AdoptingLlamaServer.adopted = []

    def _purge(_kodo_dir: Path, *, keep: tuple[str, ...]) -> None:
        purged_keep.append(keep)

    monkeypatch.setattr(f"{_APP}.LlamaServer", _AdoptingLlamaServer)
    monkeypatch.setattr(f"{_APP}.find_installed", lambda _dir: _install(1))
    monkeypatch.setattr(f"{_APP}.find_running_server", lambda _dir: running)
    monkeypatch.setattr(f"{_APP}.purge_unknown_local_models", _purge)
    monkeypatch.setattr(
        LocalModelManager, "get_model_path", lambda _self, name: Path(f"/fake/{name}.gguf")
    )

    srv = TestServer(create_app(Config()))
    await srv.start_server()
    await srv.close()

    assert _AdoptingLlamaServer.adopted == [("kept-model", 424242, 8042)]
    assert purged_keep == [("kept-model",)]
    assert rec.titler_starts == [DEFAULT_HOUSEKEEPER_LLM_ID]


async def test_startup_does_not_adopt_a_server_whose_model_is_not_downloaded(
    monkeypatch: pytest.MonkeyPatch, _temp_home: Path
) -> None:
    running = RunningServer(pid=424242, host="127.0.0.1", port=8042, model="")
    _AdoptingLlamaServer.adopted = []
    purged_keep: list[tuple[str, ...]] = []

    def _purge(_kodo_dir: Path, *, keep: tuple[str, ...]) -> None:
        purged_keep.append(keep)

    monkeypatch.setattr(f"{_APP}.LlamaServer", _AdoptingLlamaServer)
    monkeypatch.setattr(f"{_APP}.find_running_server", lambda _dir: running)
    monkeypatch.setattr(f"{_APP}.purge_unknown_local_models", _purge)

    srv = TestServer(create_app(Config()))
    await srv.start_server()
    await srv.close()

    assert _AdoptingLlamaServer.adopted == []
    assert purged_keep == [()]


async def test_shutdown_stops_a_running_llama_server(rec: _Recorder) -> None:
    srv = TestServer(create_app(Config()))
    await srv.start_server()
    rec.active = _FakeLlamaServer("m")
    await srv.close()
    assert rec.active.stops == 1


async def test_last_window_closing_releases_the_gpu(server: TestServer, rec: _Recorder) -> None:
    rec.active = _FakeLlamaServer("m")
    client = aiohttp.ClientSession()
    conn = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
    assert (await _request(conn, "session.release"))["type"] == "session.release.ack"
    await conn.close()
    await client.close()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RECV_TIMEOUT
    while rec.active.stops == 0 and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert rec.active.stops == 1
    assert rec.active.is_running is False
