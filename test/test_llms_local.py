"""Behavioral tests for :mod:`kodo.llms.local`.

Network-free: a tiny local HTTP server (with real ``Range``/``206`` support)
stands in for HuggingFace's CDN, and ``resolve_file``/``list_repo_files`` (the
only two seams that would otherwise hit huggingface_hub) are monkeypatched to
point at it. Everything else — the real byte transfer, pause/resume-from-disk,
shard deduction, mmproj linkage, uninstall — runs for real through
:class:`~kodo.llms.local.LocalModelManager`'s public API.
"""

from __future__ import annotations

import http.server
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kodo.llms.local import (
    DownloadError,
    DownloadProgress,
    FileRole,
    FileStatus,
    LocalModelManager,
    ModelFile,
    ModelNotFoundError,
    ModelRecord,
    ResolvedFile,
    ShardResolutionError,
)
from kodo.llms.local._state import save_state

# ---------------------------------------------------------------------------
# A minimal HTTP server with real Range/206 support, standing in for HF's CDN.
# ---------------------------------------------------------------------------


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payloads: dict[str, bytes] = {}
    ignore_range: bool = False
    # Artificial per-request delay and concurrent-request high-water mark —
    # exist solely so a test can prove chunk requests really do overlap in
    # wall-clock time, rather than a parallel-looking API that's secretly
    # still fetching one chunk at a time.
    request_delay: float = 0.0
    concurrent = 0
    max_concurrent = 0
    _concurrency_lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = self.payloads.get(self.path.lstrip("/"))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return

        with _RangeHandler._concurrency_lock:
            _RangeHandler.concurrent += 1
            _RangeHandler.max_concurrent = max(
                _RangeHandler.max_concurrent, _RangeHandler.concurrent
            )
        if self.request_delay:
            time.sleep(self.request_delay)
        try:
            self._respond(body)
        finally:
            with _RangeHandler._concurrency_lock:
                _RangeHandler.concurrent -= 1

    def _respond(self, body: bytes) -> None:
        start, end = 0, len(body) - 1
        status = 200
        range_header = None if self.ignore_range else self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.end_headers()
                    return
                if match.group(2):
                    end = min(int(match.group(2)), len(body) - 1)
                status = 206

        chunk = body[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Length", str(len(chunk)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture
def http_server() -> Iterator[str]:
    _RangeHandler.payloads = {}
    _RangeHandler.ignore_range = False
    _RangeHandler.request_delay = 0.0
    _RangeHandler.concurrent = 0
    _RangeHandler.max_concurrent = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def manager(tmp_path: Path) -> LocalModelManager:
    return LocalModelManager(tmp_path / "models")


def _payload(n: int) -> bytes:
    return bytes(i % 256 for i in range(n))


def _resolve_file_fake(base_url: str, payloads: dict[str, bytes]) -> Callable[..., ResolvedFile]:
    def fake(repo_id: str, filename: str, **_kwargs: object) -> ResolvedFile:
        return ResolvedFile(
            filename=filename,
            url=f"{base_url}/{filename}",
            headers={},
            etag=f"etag-{filename}",
            size=len(payloads[filename]),
            commit_hash="deadbeef",
        )

    return fake


def _list_repo_files_fake(files: list[str]) -> Callable[..., list[str]]:
    def fake(repo_id: str, **_kwargs: object) -> list[str]:
        return list(files)

    return fake


def _patch_hf(
    monkeypatch: pytest.MonkeyPatch,
    http_server: str,
    payloads: dict[str, bytes],
    *,
    repo_files: list[str] | None = None,
) -> None:
    """Point the manager's HF metadata seams at the local test server."""
    _RangeHandler.payloads = payloads
    monkeypatch.setattr(
        "kodo.llms.local._manager.resolve_file", _resolve_file_fake(http_server, payloads)
    )
    monkeypatch.setattr(
        "kodo.llms.local._manager.list_repo_files",
        _list_repo_files_fake(repo_files if repo_files is not None else list(payloads)),
    )


# ---------------------------------------------------------------------------
# Single-file download
# ---------------------------------------------------------------------------


async def test_download_single_file(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(500)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})

    record = await manager.download_model("m1", "org/repo", "model.gguf")

    assert record.is_installed
    path = manager.get_model_path("m1")
    assert path is not None
    assert path.read_bytes() == payload

    # Idempotent: calling again doesn't error and reports the same install.
    record2 = await manager.download_model("m1", "org/repo", "model.gguf")
    assert record2.is_installed


async def test_download_uses_multiple_concurrent_connections(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core ask behind this module: one file's transfer runs as several
    concurrent range requests, not a single stream dressed up to look
    parallel. Each request sleeps briefly server-side so overlapping ones are
    observable regardless of how fast loopback I/O actually is."""
    payload = _payload(4096)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})
    monkeypatch.setattr("kodo.llms.local._http._CHUNK_SIZE", 64)
    monkeypatch.setattr("kodo.llms.local._http._DEFAULT_PARALLELISM", 6)
    _RangeHandler.request_delay = 0.02

    await manager.download_model("m1", "org/repo", "model.gguf")

    path = manager.get_model_path("m1")
    assert path is not None
    assert path.read_bytes() == payload
    assert _RangeHandler.max_concurrent >= 4


async def test_download_missing_repo_raises(
    manager: LocalModelManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_list_repo_files(repo_id: str, **_kwargs: object) -> list[str]:
        raise ShardResolutionError(f"no such repo {repo_id!r}")

    monkeypatch.setattr("kodo.llms.local._manager.list_repo_files", fake_list_repo_files)
    with pytest.raises(ShardResolutionError):
        await manager.download_model("m1", "org/does-not-exist", "model.gguf")

    # The failure is still persisted — a bad repo_id raises before any shard
    # is even known, but kodo-vsix's manager-state.json poll needs *something*
    # to show instead of the download silently vanishing.
    record = manager.get_record("m1")
    assert record is not None
    (file,) = record.files
    assert file.status == FileStatus.FAILED
    assert "org/does-not-exist" in file.error


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


async def test_pause_then_resume_produces_identical_bytes(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(400)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})
    monkeypatch.setattr("kodo.llms.local._http._CHUNK_SIZE", 16)
    # Single stream here so the pause boundary below is deterministic — with
    # several concurrent chunk workers over instant loopback I/O, "pause after
    # >=100 bytes" would otherwise race the remaining ~300 bytes to
    # completion. Real multi-stream behavior is covered by
    # test_download_uses_multiple_concurrent_connections below.
    monkeypatch.setattr("kodo.llms.local._http._DEFAULT_PARALLELISM", 1)

    def progress_cb(update: DownloadProgress) -> None:
        if update.bytes_downloaded >= 100:
            manager.pause_download("m1")

    await manager.download_model("m1", "org/repo", "model.gguf", progress_cb=progress_cb)

    record = manager.get_record("m1")
    assert record is not None
    (file,) = record.files
    assert file.status == FileStatus.PAUSED
    assert 100 <= file.downloaded_bytes < len(payload)
    assert manager.get_model_path("m1") is None  # not installed while paused
    assert manager.list_resumable() == ["m1"]

    await manager.resume_download("m1")

    path = manager.get_model_path("m1")
    assert path is not None
    assert path.read_bytes() == payload
    assert manager.list_resumable() == []


async def test_resume_fails_clearly_when_server_stops_honoring_range(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that stops honoring ``Range`` mid-transfer can't be silently
    recovered from the way the old single-stream downloader recovered (by
    restarting the whole file from byte 0): a bare ``200`` response to one
    worker's *bounded* chunk request would hand back the whole file's bytes
    at the wrong offset for that chunk, and several other chunk workers may
    already be in flight. This must raise a clear ``DownloadError`` instead
    of silently writing the wrong bytes into the middle of the file."""
    payload = _payload(300)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})
    monkeypatch.setattr("kodo.llms.local._http._CHUNK_SIZE", 16)
    monkeypatch.setattr("kodo.llms.local._http._DEFAULT_PARALLELISM", 1)

    def progress_cb(update: DownloadProgress) -> None:
        if update.bytes_downloaded >= 80:
            manager.pause_download("m1")

    await manager.download_model("m1", "org/repo", "model.gguf", progress_cb=progress_cb)
    record = manager.get_record("m1")
    assert record is not None
    assert record.files[0].status == FileStatus.PAUSED

    _RangeHandler.ignore_range = True  # server now always sends the full body from byte 0
    with pytest.raises(DownloadError, match="Range"):
        await manager.resume_download("m1")


async def test_resume_unknown_model_raises(manager: LocalModelManager) -> None:
    with pytest.raises(ModelNotFoundError):
        await manager.resume_download("nope")


def test_pause_is_noop_when_nothing_downloading(manager: LocalModelManager) -> None:
    manager.pause_download("nope")  # must not raise


# ---------------------------------------------------------------------------
# Multi-file (split GGUF) deduction
# ---------------------------------------------------------------------------


async def test_multi_file_shard_deduction_from_any_shard(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard_names = [
        "model-00001-of-00003.gguf",
        "model-00002-of-00003.gguf",
        "model-00003-of-00003.gguf",
    ]
    payloads = {name: _payload(50 + i) for i, name in enumerate(shard_names)}
    _patch_hf(monkeypatch, http_server, payloads)

    # Ask for the *second* shard — every sibling must still be deduced and fetched.
    record = await manager.download_model("m1", "org/repo", "model-00002-of-00003.gguf")

    assert record.is_installed
    assert [f.filename for f in record.main_files] == shard_names
    path = manager.get_model_path("m1")
    assert path is not None
    assert path.name == shard_names[0]  # llama-server only ever gets the first shard
    for name in shard_names:
        assert (path.parent / name).read_bytes() == payloads[name]


async def test_multi_shard_overall_total_known_from_first_progress_event(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for kodo-vsix's progress bar: it reads
    ``overall_bytes_total`` off ``DownloadProgress``/manager-state.json, which
    used to stay ``None`` until each shard's own turn in the transfer loop —
    rendering an empty bar for the whole download of a large split GGUF.
    Every shard's size must now be resolved before the first byte of shard 1
    is even requested.
    """
    shard_names = [
        "model-00001-of-00003.gguf",
        "model-00002-of-00003.gguf",
        "model-00003-of-00003.gguf",
    ]
    payloads = {name: _payload(50 + i) for i, name in enumerate(shard_names)}
    _patch_hf(monkeypatch, http_server, payloads)

    progress_events: list[DownloadProgress] = []
    await manager.download_model(
        "m1", "org/repo", shard_names[0], progress_cb=progress_events.append
    )

    assert progress_events
    first = progress_events[0]
    assert first.file_index == 1
    assert first.overall_bytes_total == sum(len(p) for p in payloads.values())


async def test_missing_sibling_shard_raises(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_hf(
        monkeypatch,
        http_server,
        {},
        repo_files=["model-00001-of-00003.gguf", "model-00002-of-00003.gguf"],
    )
    with pytest.raises(ShardResolutionError):
        await manager.download_model("m1", "org/repo", "model-00001-of-00003.gguf")


# ---------------------------------------------------------------------------
# mmproj
# ---------------------------------------------------------------------------


async def test_mmproj_requires_existing_model(manager: LocalModelManager) -> None:
    with pytest.raises(ModelNotFoundError):
        await manager.download_mmproj("nope", "org/repo", "mmproj.gguf")


async def test_mmproj_attaches_to_existing_model(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_payload = _payload(200)
    mmproj_payload = _payload(64)
    _patch_hf(
        monkeypatch,
        http_server,
        {"model.gguf": model_payload, "mmproj.gguf": mmproj_payload},
        repo_files=["model.gguf"],
    )

    await manager.download_model("m1", "org/repo", "model.gguf")
    mmproj_path = await manager.download_mmproj("m1", "org/repo", "mmproj.gguf")

    assert mmproj_path.read_bytes() == mmproj_payload
    assert manager.get_model_path("m1") is not None  # main model untouched
    assert manager.get_mmproj_path("m1") == mmproj_path


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


async def test_uninstall_removes_files_and_state(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(100)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})

    await manager.download_model("m1", "org/repo", "model.gguf")
    model_dir = manager.get_model_path("m1")
    assert model_dir is not None and model_dir.parent.is_dir()

    manager.uninstall("m1")

    assert manager.get_record("m1") is None
    assert manager.list_models() == []
    assert not model_dir.parent.exists()


# ---------------------------------------------------------------------------
# Restart reconciliation and live-progress persistence
# ---------------------------------------------------------------------------


def test_reconcile_marks_stuck_downloading_as_paused(tmp_path: Path) -> None:
    """A file stuck ``DOWNLOADING`` in the state file (the process died
    mid-transfer) must read back as ``PAUSED`` from a fresh instance — that's
    what lets kodo-vsix offer "resume" instead of showing progress that will
    never move again (doc/LOCAL_MODEL_MANAGER.md §11)."""
    root = tmp_path / "models"
    stuck = ModelRecord(
        model_id="m1",
        repo_id="org/repo",
        revision="main",
        commit_hash=None,
        files=[
            ModelFile(
                filename="model.gguf",
                role=FileRole.MAIN,
                repo_id="org/repo",
                status=FileStatus.DOWNLOADING,
                downloaded_bytes=50,
            )
        ],
    )
    save_state(root / "manager-state.json", {"m1": stuck})

    manager = LocalModelManager(root)  # simulates a fresh process after a restart

    record = manager.get_record("m1")
    assert record is not None
    assert record.files[0].status == FileStatus.PAUSED
    assert record.files[0].downloaded_bytes == 50  # byte count preserved, only status changes
    assert manager.list_resumable() == ["m1"]


def test_reconcile_is_noop_when_nothing_stale(tmp_path: Path) -> None:
    """No stale DOWNLOADING file anywhere — construction must not touch disk."""
    root = tmp_path / "models"
    LocalModelManager(root)
    assert not (root / "manager-state.json").exists()


async def test_in_flight_download_periodically_persists_progress(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manager-state.json`` must reflect live progress mid-transfer, not just
    at the start/end of a file — kodo-vsix polls it directly off disk rather
    than over a live WS push (doc/LOCAL_MODEL_MANAGER.md §11)."""
    payload = _payload(200)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})
    monkeypatch.setattr("kodo.llms.local._http._CHUNK_SIZE", 16)
    monkeypatch.setattr("kodo.llms.local._manager._FLUSH_INTERVAL_SECONDS", 0.0)

    seen_on_disk: list[int] = []

    def progress_cb(update: DownloadProgress) -> None:
        on_disk = manager.get_record("m1")
        assert on_disk is not None
        seen_on_disk.append(on_disk.files[0].downloaded_bytes)

    await manager.download_model("m1", "org/repo", "model.gguf", progress_cb=progress_cb)

    # With the flush interval forced to 0, every chunk flushes — the on-disk
    # byte count should have moved during the transfer, not just jumped from
    # 0 straight to "complete" once the loop finished.
    assert any(0 < n < len(payload) for n in seen_on_disk)

    manager.uninstall("m1")  # no-op the second time


async def test_download_speed_reported_then_cleared_on_completion(
    manager: LocalModelManager, http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bytes_per_second`` should turn non-``None`` once at least two chunk
    samples have landed (see ``_windowed_bytes_per_second``), then clear back
    to ``None`` once the file leaves ``DOWNLOADING`` — a completed/paused/
    failed file must never show a frozen, stale rate."""
    payload = _payload(200)
    _patch_hf(monkeypatch, http_server, {"model.gguf": payload})
    monkeypatch.setattr("kodo.llms.local._http._CHUNK_SIZE", 16)
    monkeypatch.setattr("kodo.llms.local._manager._FLUSH_INTERVAL_SECONDS", 0.0)

    seen_rates: list[float | None] = []

    def progress_cb(update: DownloadProgress) -> None:
        on_disk = manager.get_record("m1")
        assert on_disk is not None
        seen_rates.append(on_disk.files[0].bytes_per_second)

    await manager.download_model("m1", "org/repo", "model.gguf", progress_cb=progress_cb)

    assert any(rate is not None for rate in seen_rates)
    record = manager.get_record("m1")
    assert record is not None
    assert record.files[0].bytes_per_second is None
