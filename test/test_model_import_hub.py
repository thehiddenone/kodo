"""The network half of the Model Importer: ``HuggingFaceHub`` and ``read_gguf_metadata``.

Nothing here reaches huggingface.co. ``huggingface_hub``'s metadata calls are
replaced with fakes, and every HTTP byte — the model card, the GGUF header
ranges — is served by a local aiohttp server, so the real request code runs
end to end: Range handling, a server that ignores Range, error statuses, and
the mapping of every failure onto the one error type each caller expects.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import httpx
import huggingface_hub
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

from kodo.llms import local as local_pkg
from kodo.llms.local import GgufHeaderError, ResolvedFile, read_gguf_metadata
from kodo.llms.model_import import HuggingFaceHub, ModelImportError


def _gguf(arch: str, context: int) -> bytes:
    def s(text: str) -> bytes:
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw

    kvs = (
        s("general.architecture") + struct.pack("<I", 8) + s(arch),
        s(f"{arch}.context_length") + struct.pack("<II", 4, context),
    )
    return b"GGUF" + struct.pack("<IQQ", 3, 1, len(kvs)) + b"".join(kvs) + b"\0" * 32


@dataclass
class _Files:
    """What the local server serves, and what it was asked."""

    body: dict[str, bytes] = field(default_factory=dict)
    honour_range: bool = True
    status: int = 200
    ranges: list[str] = field(default_factory=list)


@pytest.fixture
async def files() -> AsyncGenerator[tuple[_Files, str], None]:
    served = _Files()

    async def handler(request: web.Request) -> web.StreamResponse:
        if served.status != 200:
            return web.Response(status=served.status)
        data = served.body.get(request.match_info["path"])
        if data is None:
            return web.Response(status=404)
        header = request.headers.get("Range", "")
        served.ranges.append(header)
        if header and served.honour_range:
            start, end = (int(x) for x in header.removeprefix("bytes=").split("-"))
            return web.Response(status=206, body=data[start : end + 1])
        return web.Response(status=200, body=data)

    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    yield served, f"http://127.0.0.1:{server.port}"
    await server.close()


def _resolve_to(base: str, monkeypatch: pytest.MonkeyPatch, *, size: int | None) -> None:
    """Point read_gguf_metadata's HF resolution at the local server."""

    def fake_resolve(repo_id: str, filename: str, **_: object) -> ResolvedFile:
        return ResolvedFile(
            filename=filename,
            url=f"{base}/{filename}",
            headers={},
            etag="e",
            size=size,
            commit_hash="c",
        )

    monkeypatch.setattr("kodo.llms.local._gguf.resolve_file", fake_resolve)


# ---------------------------------------------------------------------------
# read_gguf_metadata over HTTP
# ---------------------------------------------------------------------------


async def test_a_header_is_read_with_range_requests(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    data = _gguf("qwen35", 262144)
    served.body["m.gguf"] = data
    _resolve_to(base, monkeypatch, size=len(data))
    meta = await read_gguf_metadata("acme/repo", "m.gguf")
    assert (meta.architecture, meta.context_length) == ("qwen35", 262144)
    assert served.ranges == [f"bytes=0-{len(data) - 1}"]


async def test_a_server_that_ignores_range_still_yields_the_header(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    served.honour_range = False
    served.body["m.gguf"] = _gguf("gemma4", 8192)
    _resolve_to(base, monkeypatch, size=None)
    meta = await read_gguf_metadata("acme/repo", "m.gguf")
    assert meta.context_length == 8192


async def test_an_error_status_is_a_header_error(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    served.status = 500
    _resolve_to(base, monkeypatch, size=None)
    with pytest.raises(GgufHeaderError, match="HTTP 500"):
        await read_gguf_metadata("acme/repo", "m.gguf")


async def test_an_unreachable_server_is_a_header_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to("http://127.0.0.1:9", monkeypatch, size=None)
    with pytest.raises(GgufHeaderError, match="could not read the header"):
        await read_gguf_metadata("acme/repo", "m.gguf")


# ---------------------------------------------------------------------------
# HuggingFaceHub.snapshot / gguf_header
# ---------------------------------------------------------------------------


@dataclass
class _Sibling:
    rfilename: str
    size: int | None


@dataclass
class _Info:
    author: str | None = "unsloth"
    gated: object = False
    tags: list[str] | None = None
    pipeline_tag: str | None = "text-generation"
    siblings: list[_Sibling] | None = None
    card_data: dict[str, object] | None = None


def _hub_returns(info: _Info, base: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(huggingface_hub, "model_info", lambda *a, **k: info)
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_url", lambda repo_id, filename: f"{base}/{repo_id}/{filename}"
    )


async def test_snapshot_maps_the_card_and_files(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    served.body["acme/repo/README.md"] = b"# Model card"
    info = _Info(
        gated="manual",
        tags=["gguf"],
        siblings=[_Sibling("a.gguf", 10), _Sibling("b.gguf", None)],
        card_data={
            "license": "other",
            "license_name": "acme-1.0",
            "license_link": "LICENSE",
            "base_model": "acme/Base",
        },
    )
    _hub_returns(info, base, monkeypatch)
    snap = await HuggingFaceHub().snapshot("acme/repo")
    assert snap.author == "unsloth"
    assert snap.gated == "manual"
    assert (snap.license, snap.license_name) == ("other", "acme-1.0")
    # A repo-relative license link is made absolute.
    assert snap.license_link == "https://huggingface.co/acme/repo/blob/main/LICENSE"
    assert snap.base_models == ("acme/Base",)
    assert [(f.path, f.size) for f in snap.files] == [("a.gguf", 10), ("b.gguf", 0)]
    assert (snap.readme, snap.readme_truncated) == ("# Model card", False)


async def test_a_long_card_is_cut_and_flagged(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    served.body["acme/repo/README.md"] = b"x" * 100_000
    _hub_returns(_Info(card_data={"base_model": ["acme/A", "acme/B"]}), base, monkeypatch)
    snap = await HuggingFaceHub().snapshot("acme/repo")
    assert snap.readme_truncated is True
    assert len(snap.readme) == 40_000
    assert snap.base_models == ("acme/A", "acme/B")


async def test_a_repo_without_a_card_has_an_empty_readme(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, base = files
    _hub_returns(_Info(author=None, card_data=None), base, monkeypatch)
    snap = await HuggingFaceHub().snapshot("acme/repo")
    assert (snap.readme, snap.author, snap.license, snap.gated) == ("", "", "", "")


async def test_an_unreachable_card_is_an_empty_readme(monkeypatch: pytest.MonkeyPatch) -> None:
    _hub_returns(_Info(), "http://127.0.0.1:9", monkeypatch)
    assert (await HuggingFaceHub().snapshot("acme/repo")).readme == ""


def _http_error(cls: type[HfHubHTTPError], status: int) -> HfHubHTTPError:
    response = httpx.Response(status, request=httpx.Request("GET", "https://hf.invalid/x"))
    return cls("failed", response=response)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (_http_error(GatedRepoError, 403), "gated repository"),
        (_http_error(RepositoryNotFoundError, 404), "not found"),
        (_http_error(HfHubHTTPError, 500), "request for acme/repo failed"),
    ],
)
async def test_hub_failures_become_import_errors(
    monkeypatch: pytest.MonkeyPatch, error: HfHubHTTPError, message: str
) -> None:
    def fail(*a: object, **k: object) -> object:
        raise error

    monkeypatch.setattr(huggingface_hub, "model_info", fail)
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    with pytest.raises(ModelImportError, match=message):
        await HuggingFaceHub().snapshot("acme/repo")


async def test_gguf_header_failures_become_import_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*a: object, **k: object) -> object:
        raise local_pkg.ShardResolutionError("File not found in 'acme/repo': 'x.gguf'")

    monkeypatch.setattr("kodo.llms.model_import._hub.read_gguf_metadata", fail)
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    with pytest.raises(ModelImportError, match="acme/repo/x.gguf: File not found"):
        await HuggingFaceHub().gguf_header("acme/repo", "x.gguf")


async def test_gguf_header_reads_through_the_range_reader(
    files: tuple[_Files, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    served, base = files
    served.body["m.gguf"] = _gguf("laguna", 8192)
    _resolve_to(base, monkeypatch, size=None)
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    meta = await HuggingFaceHub().gguf_header("acme/repo", "m.gguf")
    assert meta.architecture == "laguna"
