"""Standalone llama-server state files (:class:`kodo.llamaserver.StandaloneState`).

Pure filesystem tests under ``tmp_path``: the per-port record's path, its
atomic write and tolerant read (including llama-server's own bare runtime
record), the port-ordered directory scan, and the client-facing base URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.llamaserver import StandaloneState


def _state(port: int, *, supervisor: int = 1, host: str = "127.0.0.1") -> StandaloneState:
    return StandaloneState(
        supervisor_pid=supervisor,
        llama_pid=2,
        host=host,
        port=port,
        model="fake-model",
        profile_id="fast",
        kodo_version="9.9.9",
    )


def test_path_for_is_a_per_port_file_under_standalone(tmp_path: Path) -> None:
    assert StandaloneState.path_for(tmp_path, 8080) == (
        tmp_path / "llama.cpp" / "standalone" / "8080.json"
    )


def test_write_then_read_round_trips_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = StandaloneState.path_for(tmp_path, 8081)
    state = _state(8081)
    state.write(path)
    assert StandaloneState.read(path) == state
    assert [p.name for p in path.parent.iterdir()] == ["8081.json"]


def test_read_accepts_llama_servers_bare_runtime_record_as_unstamped(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"pid": 55, "port": 8082}), encoding="utf-8")
    state = StandaloneState.read(path)
    assert state == StandaloneState(
        supervisor_pid=0, llama_pid=55, host="127.0.0.1", port=8082, model=""
    )
    assert state is not None and not state.is_stamped


def test_a_supervisor_record_is_stamped() -> None:
    assert _state(8083, supervisor=12).is_stamped


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps([1, 2, 3]),
        json.dumps({"llama_pid": 5}),
        json.dumps({"port": 8084}),
        json.dumps({"llama_pid": 5, "port": "eighty"}),
    ],
    ids=["invalid-json", "not-an-object", "no-port", "no-pid", "bad-port"],
)
def test_read_rejects_an_unusable_record(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert StandaloneState.read(path) is None


def test_read_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert StandaloneState.read(tmp_path / "absent.json") is None


def test_read_all_without_a_standalone_directory_is_empty(tmp_path: Path) -> None:
    assert StandaloneState.read_all(tmp_path) == []


def test_read_all_orders_by_port_and_skips_unparseable_files(tmp_path: Path) -> None:
    for port in (9000, 10000, 8000):
        _state(port).write(StandaloneState.path_for(tmp_path, port))
    StandaloneState.path_for(tmp_path, 7000).write_text("garbage", encoding="utf-8")

    found = StandaloneState.read_all(tmp_path)
    assert [state.port for _, state in found] == [8000, 9000, 10000]
    assert [path.name for path, _ in found] == ["8000.json", "9000.json", "10000.json"]


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("", "http://127.0.0.1:8085"),
        ("0.0.0.0", "http://127.0.0.1:8085"),
        ("::", "http://127.0.0.1:8085"),
        ("127.0.0.1", "http://127.0.0.1:8085"),
        ("172.17.0.1", "http://172.17.0.1:8085"),
        ("::1", "http://[::1]:8085"),
    ],
)
def test_base_url_maps_wildcards_to_loopback_and_brackets_ipv6(host: str, url: str) -> None:
    assert _state(8085, host=host).base_url == url
