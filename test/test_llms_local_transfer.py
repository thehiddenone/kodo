"""Behavioral tests for :class:`kodo.llms.local.LocalModelManager`'s HF and transfer edges.

Network-free. The only seams replaced are the third-party ones the manager
reaches through: ``huggingface_hub``'s metadata calls (``hf_hub_url``,
``get_hf_file_metadata``, ``model_info``) are monkeypatched to a fake hub, and
the "CDN" the resolved URLs point at is a real :mod:`aiohttp` server on
loopback with ``Range``/``206`` support plus a few failure modes. Everything
else — HF error mapping, token forwarding, sequential and parallel transfers,
the ``<file>.part`` / ``<file>.part.chunks`` resume bookkeeping, and the
corrupt-state-file tolerance — runs for real through the manager's public API.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import aiohttp
import httpx
import huggingface_hub
import pytest
from aiohttp import web
from huggingface_hub import HfFileMetadata
from huggingface_hub.errors import (
    EntryNotFoundError,
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

from kodo.llms.local import (
    DownloadError,
    DownloadProgress,
    FileStatus,
    LocalModelManager,
    ShardResolutionError,
)

# The parallel downloader's fixed range size, as documented in its module
# docstring ("4 MiB"). Payloads below are sized off it so a file really spans
# several ranges without reaching into any private module constant.
_CHUNK = 4 * 1024 * 1024
_STATE_FILE = "manager-state.json"


def _payload(n: int) -> bytes:
    return (bytes(range(256)) * (n // 256 + 1))[:n]


def _http_error(cls: type[HfHubHTTPError]) -> HfHubHTTPError:
    response = httpx.Response(404, request=httpx.Request("GET", "https://huggingface.co/x"))
    return cls("boom", response=response)


# ---------------------------------------------------------------------------
# Fake CDN (aiohttp on loopback)
# ---------------------------------------------------------------------------


@dataclass
class _Cdn:
    """State of the fake CDN; the test mutates it to pick a behavior."""

    base_url: str = ""
    payloads: dict[str, bytes] = field(default_factory=dict)
    ignore_range: bool = False
    short_body: bool = False
    # When set, an un-ranged/open-ended response sends `gate_after` bytes, then
    # waits for `gate` before sending the rest.
    gate: asyncio.Event | None = None
    gate_after: int = 0
    ranges: list[str] = field(default_factory=list)
    authorizations: list[str | None] = field(default_factory=list)


async def _handle(cdn: _Cdn, request: web.Request) -> web.StreamResponse:
    cdn.authorizations.append(request.headers.get("Authorization"))
    body = cdn.payloads.get(request.match_info["name"])
    if body is None:
        return web.Response(status=404)
    range_header = None if cdn.ignore_range else request.headers.get("Range")
    cdn.ranges.append(range_header or "")
    start, end, status = 0, len(body) - 1, 200
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        assert match is not None
        start = int(match.group(1))
        if start >= len(body):
            return web.Response(status=416)
        if match.group(2):
            end = min(int(match.group(2)), len(body) - 1)
        status = 206
    chunk = body[start : end + 1]
    if cdn.short_body:
        chunk = chunk[:-1]

    gate = cdn.gate
    if gate is None or gate.is_set():
        return web.Response(status=status, body=chunk)

    response = web.StreamResponse(status=status)
    response.content_length = len(chunk)
    await response.prepare(request)
    await response.write(chunk[: cdn.gate_after])
    await gate.wait()
    try:
        await response.write(chunk[cdn.gate_after :])
        await response.write_eof()
    except (ConnectionError, RuntimeError):
        pass  # the client already hung up after pausing
    return response


@pytest.fixture
async def cdn() -> AsyncIterator[_Cdn]:
    state = _Cdn()

    async def handler(request: web.Request) -> web.StreamResponse:
        return await _handle(state, request)

    app = web.Application()
    app.router.add_get("/{name}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = cast(tuple[str, int], runner.addresses[0])[:2]
    state.base_url = f"http://{host}:{port}"
    try:
        yield state
    finally:
        if state.gate is not None:
            state.gate.set()
        await runner.cleanup()


def _closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = cast(int, s.getsockname()[1])
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Fake HF hub (monkeypatched huggingface_hub entry points)
# ---------------------------------------------------------------------------


@dataclass
class _Hub:
    """Fake ``huggingface_hub`` metadata surface pointing at the fake CDN."""

    cdn: _Cdn
    hub_base: str = ""
    repo_files: list[str] | None = None
    size_known: bool = True
    etag: str | None = "etag-1"
    location_base: str = ""
    meta_error: Exception | None = None
    info_error: Exception | None = None

    def hf_hub_url(self, repo_id: str, filename: str, *, revision: str = "main") -> str:
        base = self.hub_base or self.cdn.base_url
        return f"{base}/{repo_id}/resolve/{revision}/{filename}"

    def get_hf_file_metadata(self, url: str, token: str | None = None) -> HfFileMetadata:
        if self.meta_error is not None:
            raise self.meta_error
        filename = url.rsplit("/", 1)[-1]
        payload = self.cdn.payloads.get(filename)
        return HfFileMetadata(
            commit_hash="c0ffee",
            etag=self.etag,
            location=f"{self.location_base or self.cdn.base_url}/{filename}",
            size=len(payload) if self.size_known and payload is not None else None,
            xet_file_data=None,
        )

    def model_info(
        self, repo_id: str, *, revision: str | None = None, token: str | None = None
    ) -> SimpleNamespace:
        if self.info_error is not None:
            raise self.info_error
        files = self.repo_files
        siblings = None if files is None else [SimpleNamespace(rfilename=f) for f in files]
        return SimpleNamespace(siblings=siblings)


@pytest.fixture
def hub(cdn: _Cdn, monkeypatch: pytest.MonkeyPatch) -> _Hub:
    fake = _Hub(cdn=cdn, repo_files=["model.gguf"])
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", fake.hf_hub_url)
    monkeypatch.setattr(huggingface_hub, "get_hf_file_metadata", fake.get_hf_file_metadata)
    monkeypatch.setattr(huggingface_hub, "model_info", fake.model_info)
    return fake


@pytest.fixture
def manager(tmp_path: Path) -> LocalModelManager:
    return LocalModelManager(tmp_path / "models")


def _part_path(manager: LocalModelManager, filename: str = "model.gguf") -> Path:
    path = manager.root_dir / "m1" / f"{filename}.part"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _installed_bytes(manager: LocalModelManager) -> bytes:
    path = manager.get_model_path("m1")
    assert path is not None, "model is not installed"
    return path.read_bytes()


def _file_status(manager: LocalModelManager) -> tuple[FileStatus, str]:
    record = manager.get_record("m1")
    assert record is not None
    file = record.files[0]
    return file.status, file.error


# ---------------------------------------------------------------------------
# HF metadata resolution
# ---------------------------------------------------------------------------


async def test_token_is_sent_when_the_file_stays_on_the_hub_host(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(300)

    await manager.download_model("m1", "org/repo", "model.gguf", token="hf_secret")

    assert _installed_bytes(manager) == cdn.payloads["model.gguf"]
    assert cdn.authorizations == ["Bearer hf_secret"]


async def test_token_is_withheld_from_a_cdn_on_another_host(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(300)
    hub.hub_base = "https://huggingface.co"

    await manager.download_model("m1", "org/repo", "model.gguf", token="hf_secret")

    assert _installed_bytes(manager) == cdn.payloads["model.gguf"]
    assert cdn.authorizations == [None]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (lambda: _http_error(GatedRepoError), "gated repository"),
        (lambda: _http_error(RepositoryNotFoundError), "repository not found"),
        (lambda: _http_error(RevisionNotFoundError), "Revision 'main' not found"),
        (lambda: EntryNotFoundError("gone"), "File not found"),
        (lambda: _http_error(HfHubHTTPError), "Could not resolve 'model.gguf'"),
    ],
)
async def test_file_metadata_errors_become_shard_resolution_errors(
    manager: LocalModelManager,
    cdn: _Cdn,
    hub: _Hub,
    error: Callable[[], Exception],
    message: str,
) -> None:
    cdn.payloads["model.gguf"] = _payload(10)
    hub.meta_error = error()

    with pytest.raises(ShardResolutionError, match=message):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, recorded = _file_status(manager)
    assert status == FileStatus.FAILED
    assert message in recorded


async def test_file_without_an_etag_is_refused(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(10)
    hub.etag = None

    with pytest.raises(ShardResolutionError, match="no ETag"):
        await manager.download_model("m1", "org/repo", "model.gguf")
    assert cdn.ranges == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (lambda: _http_error(GatedRepoError), "gated repository"),
        (lambda: _http_error(RepositoryNotFoundError), "Could not find 'org/repo'"),
        (lambda: _http_error(RevisionNotFoundError), "at revision 'main'"),
        (lambda: _http_error(HfHubHTTPError), "Could not list files"),
    ],
)
async def test_repo_listing_errors_become_shard_resolution_errors(
    manager: LocalModelManager,
    hub: _Hub,
    error: Callable[[], Exception],
    message: str,
) -> None:
    hub.info_error = error()

    with pytest.raises(ShardResolutionError, match=message):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, _ = _file_status(manager)
    assert status == FileStatus.FAILED


async def test_split_gguf_cannot_be_satisfied_by_an_empty_repo_listing(
    manager: LocalModelManager, hub: _Hub
) -> None:
    hub.repo_files = None  # HF reported no siblings at all

    with pytest.raises(ShardResolutionError, match="2-way split GGUF"):
        await manager.download_model("m1", "org/repo", "model-00001-of-00002.gguf")


async def test_check_for_update_compares_against_the_live_etag(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(50)
    await manager.download_model("m1", "org/repo", "model.gguf")

    assert await manager.check_for_update("m1") is False
    hub.etag = "etag-2"
    assert await manager.check_for_update("m1") is True
    hub.meta_error = _http_error(RepositoryNotFoundError)
    assert await manager.check_for_update("m1") is False


# ---------------------------------------------------------------------------
# Sequential transfer (size unknown upfront)
# ---------------------------------------------------------------------------


async def test_unknown_size_file_downloads_as_a_single_stream(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(5000)
    hub.size_known = False

    record = await manager.download_model("m1", "org/repo", "model.gguf")

    assert record.is_installed
    assert _installed_bytes(manager) == cdn.payloads["model.gguf"]
    assert cdn.ranges == [""]


async def _pause_unknown_size_download(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub, payload: bytes, pause_at: int
) -> None:
    """Start a single-stream download and pause it after exactly *pause_at* bytes."""
    cdn.payloads["model.gguf"] = payload
    hub.size_known = False
    gate = asyncio.Event()
    cdn.gate = gate
    cdn.gate_after = pause_at

    def progress_cb(update: DownloadProgress) -> None:
        if update.bytes_downloaded >= pause_at:
            manager.pause_download("m1")
            gate.set()

    await manager.download_model("m1", "org/repo", "model.gguf", progress_cb=progress_cb)

    record = manager.get_record("m1")
    assert record is not None
    assert record.files[0].status == FileStatus.PAUSED
    assert record.files[0].downloaded_bytes == pause_at
    assert manager.get_model_path("m1") is None


async def test_unknown_size_download_pauses_and_resumes_with_a_range_request(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    payload = _payload(3000)
    await _pause_unknown_size_download(manager, cdn, hub, payload, pause_at=1000)

    await manager.resume_download("m1")

    assert _installed_bytes(manager) == payload
    assert cdn.ranges[-1] == "bytes=1000-"


async def test_unknown_size_resume_restarts_when_the_server_ignores_range(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    payload = _payload(3000)
    await _pause_unknown_size_download(manager, cdn, hub, payload, pause_at=1000)

    cdn.ignore_range = True
    await manager.resume_download("m1")

    # The full body came back from byte 0 — it must replace, not extend, the part file.
    assert _installed_bytes(manager) == payload


async def test_unknown_size_resume_treats_an_unsatisfiable_range_as_complete(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    payload = _payload(3000)
    await _pause_unknown_size_download(manager, cdn, hub, payload, pause_at=1000)

    cdn.payloads["model.gguf"] = payload[:1000]  # remote now ends where we stopped
    await manager.resume_download("m1")

    assert _installed_bytes(manager) == payload[:1000]


async def test_unknown_size_http_error_fails_the_download(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    hub.size_known = False  # and the CDN has no such file → 404

    with pytest.raises(DownloadError, match="HTTP 404"):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, error = _file_status(manager)
    assert status == FileStatus.FAILED
    assert "HTTP 404" in error


@pytest.mark.parametrize("size_known", [False, True])
async def test_unreachable_cdn_is_a_network_error(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub, size_known: bool
) -> None:
    cdn.payloads["model.gguf"] = _payload(100)
    hub.size_known = size_known
    hub.location_base = _closed_port_url()

    with pytest.raises(DownloadError, match="Network error"):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, _ = _file_status(manager)
    assert status == FileStatus.FAILED


@pytest.mark.parametrize("size_known", [False, True])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TimeoutError("too slow"), "Timed out"),
        (ConnectionResetError("reset"), "Local I/O error"),
    ],
)
async def test_transport_level_failures_become_download_errors(
    manager: LocalModelManager,
    cdn: _Cdn,
    hub: _Hub,
    monkeypatch: pytest.MonkeyPatch,
    size_known: bool,
    error: Exception,
    message: str,
) -> None:
    cdn.payloads["model.gguf"] = _payload(100)
    hub.size_known = size_known

    def failing_get(self: aiohttp.ClientSession, *args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(aiohttp.ClientSession, "get", failing_get)

    with pytest.raises(DownloadError, match=message):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, _ = _file_status(manager)
    assert status == FileStatus.FAILED


# ---------------------------------------------------------------------------
# Parallel chunked transfer (size known upfront)
# ---------------------------------------------------------------------------


async def test_zero_byte_file_installs_without_any_request(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = b""

    record = await manager.download_model("m1", "org/repo", "model.gguf")

    assert record.is_installed
    assert _installed_bytes(manager) == b""
    assert cdn.ranges == []


async def test_full_size_part_file_without_bookkeeping_is_trusted(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    """A ``.part`` already at full size with no sidecar is a finished transfer
    (or a pre-chunking download) — it is installed as-is, no request made."""
    payload = _payload(700)
    cdn.payloads["model.gguf"] = payload
    _part_path(manager).write_bytes(payload)

    await manager.download_model("m1", "org/repo", "model.gguf")

    assert _installed_bytes(manager) == payload
    assert cdn.ranges == []


async def test_sidecar_recording_every_chunk_finishes_without_any_request(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    payload = _payload(2 * _CHUNK + 10)
    cdn.payloads["model.gguf"] = payload
    part = _part_path(manager)
    part.write_bytes(payload)
    sidecar = part.with_name(part.name + ".chunks")
    sidecar.write_text(
        json.dumps({"chunk_size": _CHUNK, "total_size": len(payload), "completed": [0, 1, 2]}),
        encoding="utf-8",
    )

    await manager.download_model("m1", "org/repo", "model.gguf")

    assert _installed_bytes(manager) == payload
    assert cdn.ranges == []
    assert not sidecar.exists()


@pytest.mark.parametrize(
    "sidecar_text",
    [
        "{not json",
        "[]",
        json.dumps({"chunk_size": 1, "total_size": 1, "completed": [0]}),
        json.dumps({"chunk_size": _CHUNK, "total_size": 2 * _CHUNK + 10, "completed": "0"}),
        json.dumps({"chunk_size": _CHUNK, "total_size": 2 * _CHUNK + 10, "completed": ["x"]}),
    ],
    ids=["corrupt-json", "not-an-object", "other-layout", "completed-not-a-list", "bad-index"],
)
async def test_unusable_sidecar_refetches_every_chunk(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub, sidecar_text: str
) -> None:
    """A sidecar that can't be used means the out-of-order, already full-size
    ``.part`` holds no trustworthy range: every chunk is fetched again, so the
    never-written (zero) regions are not installed as the model."""
    payload = _payload(2 * _CHUNK + 10)
    cdn.payloads["model.gguf"] = payload
    part = _part_path(manager)
    part.write_bytes(payload[:_CHUNK] + bytes(_CHUNK + 10))  # chunk 0 written, rest zeros
    part.with_name(part.name + ".chunks").write_text(sidecar_text, encoding="utf-8")

    await manager.download_model("m1", "org/repo", "model.gguf")

    assert _installed_bytes(manager) == payload
    assert sorted(cdn.ranges) == [
        f"bytes=0-{_CHUNK - 1}",
        f"bytes={_CHUNK}-{2 * _CHUNK - 1}",
        f"bytes={2 * _CHUNK}-{2 * _CHUNK + 9}",
    ]


async def test_part_prefix_without_sidecar_reuses_whole_chunks(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    """A ``.part`` with no sidecar is a contiguous prefix from a single-stream
    download: whole chunks inside it are reused, the partial one is refetched."""
    payload = _payload(2 * _CHUNK + 10)
    cdn.payloads["model.gguf"] = payload
    _part_path(manager).write_bytes(payload[: _CHUNK + 5])

    await manager.download_model("m1", "org/repo", "model.gguf")

    assert _installed_bytes(manager) == payload
    assert sorted(cdn.ranges) == [
        f"bytes={_CHUNK}-{2 * _CHUNK - 1}",
        f"bytes={2 * _CHUNK}-{2 * _CHUNK + 9}",
    ]


@pytest.mark.parametrize("size_known", [False, True])
async def test_unwritable_part_file_fails_the_download(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub, size_known: bool
) -> None:
    """A local I/O error while setting up the part file is a DownloadError like
    any other, so the file is marked failed instead of stuck downloading."""
    cdn.payloads["model.gguf"] = _payload(2 * _CHUNK + 10)
    hub.size_known = size_known
    _part_path(manager).mkdir(parents=True)

    with pytest.raises(DownloadError, match="Local I/O error"):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, _ = _file_status(manager)
    assert status == FileStatus.FAILED


async def test_short_range_response_fails_the_download(
    manager: LocalModelManager, cdn: _Cdn, hub: _Hub
) -> None:
    cdn.payloads["model.gguf"] = _payload(500)
    cdn.short_body = True

    with pytest.raises(DownloadError, match="Short read"):
        await manager.download_model("m1", "org/repo", "model.gguf")

    status, error = _file_status(manager)
    assert status == FileStatus.FAILED
    assert "expected 500 bytes, got 499" in error


# ---------------------------------------------------------------------------
# State-file tolerance
# ---------------------------------------------------------------------------


def _write_state(root: Path, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / _STATE_FILE).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("text", ["{not json", "[1, 2, 3]"], ids=["corrupt", "not-an-object"])
def test_unreadable_state_file_reads_as_empty(tmp_path: Path, text: str) -> None:
    root = tmp_path / "models"
    _write_state(root, text)

    manager = LocalModelManager(root)

    assert manager.list_models() == []
    assert manager.get_record("m1") is None


def test_corrupt_state_entries_are_dropped_individually(tmp_path: Path) -> None:
    root = tmp_path / "models"
    good_file = {
        "filename": "model.gguf",
        "role": "main",
        "repo_id": "org/repo",
        "status": "completed",
        "size": 10,
        "downloaded_bytes": 10,
    }
    _write_state(
        root,
        json.dumps(
            {
                "good": {"repo_id": "org/repo", "files": [good_file, "not-a-file"]},
                "not-a-dict": 42,
                "missing-repo": {"files": []},
                "bad-role": {"repo_id": "org/repo", "files": [{**good_file, "role": "bogus"}]},
            }
        ),
    )

    manager = LocalModelManager(root)

    records = manager.list_models()
    assert [r.model_id for r in records] == ["good"]
    (file,) = records[0].files
    assert file.status == FileStatus.COMPLETED
    assert file.revision == "main"
