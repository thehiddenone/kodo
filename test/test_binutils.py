"""Tests for the portable third-party util manager (``kodo.binutils``).

Downloads are faked by patching ``urllib.request.urlopen`` with an in-memory
release archive, so no test touches the network; every install lands under
``tmp_path`` standing in for ``~/.kodo``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import platform
import tarfile
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from kodo.binutils import UTIL_SPECS, UtilInstall, ensure_all_utils, ensure_util, find_util

_IS_WINDOWS = platform.system() == "Windows"

pytestmark = pytest.mark.skipif(
    _IS_WINDOWS, reason="fake release archives are built as tar.gz (the non-Windows format)"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tar_gz(members: dict[str, bytes]) -> bytes:
    """An in-memory ``.tar.gz`` holding *members* (archive path → content)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release(binary: str) -> bytes:
    """A release archive with *binary* nested in a versioned directory, as upstream ships it."""
    return _tar_gz(
        {f"release-1.2.3/{binary}": b"#!/bin/sh\necho hi\n", "release-1.2.3/README": b"x"}
    )


class _FakeNetwork:
    """Stands in for ``urlopen``: serves canned bytes per URL and records requests."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requested: list[str] = []

    def urlopen(self, req: urllib.request.Request, **kwargs: object) -> object:
        url = req.full_url
        self.requested.append(url)
        if url not in self.payloads:
            raise OSError(f"404 for {url}")

        @contextmanager
        def _resp() -> Iterator[io.BytesIO]:
            yield io.BytesIO(self.payloads[url])

        return _resp()


def _url_for(name: str) -> str:
    """The download URL :func:`ensure_util` will request for *name* on this host."""
    spec = UTIL_SPECS[name]
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    os_key = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}[platform.system()]
    target = spec.targets[f"{os_key}-{arch}"]
    archive = spec.archive_template.format(version=spec.version, target=target, ext="tar.gz")
    return spec.url_template.format(version=spec.version, archive=archive)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> _FakeNetwork:
    """A fake network serving a valid release for every util."""
    fake = _FakeNetwork(
        {_url_for(name): _release(spec.binary) for name, spec in UTIL_SPECS.items()}
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def _write_manifest(kodo_dir: Path, name: str, payload: object) -> None:
    path = kodo_dir / "bin" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# find_util
# ---------------------------------------------------------------------------


def test_find_util_without_a_manifest_is_none(tmp_path: Path) -> None:
    assert find_util(tmp_path, "uv") is None


def test_find_util_with_unparseable_manifest_is_none(tmp_path: Path) -> None:
    path = tmp_path / "bin" / "uv.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert find_util(tmp_path, "uv") is None


def test_find_util_with_non_object_manifest_is_none(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "uv", ["uv", "0.1"])
    assert find_util(tmp_path, "uv") is None


def test_find_util_with_manifest_missing_keys_is_none(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "uv", {"name": "uv", "version": "0.1"})
    assert find_util(tmp_path, "uv") is None


def test_find_util_whose_binary_vanished_is_none(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "uv", {"version": "0.1", "path": str(tmp_path / "gone" / "uv")})
    assert find_util(tmp_path, "uv") is None


def test_find_util_reports_the_recorded_install(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "uv" / "uv"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"x")
    _write_manifest(tmp_path, "uv", {"name": "uv", "version": "0.1", "path": str(binary)})
    assert find_util(tmp_path, "uv") == UtilInstall(name="uv", version="0.1", path=binary)


# ---------------------------------------------------------------------------
# ensure_util
# ---------------------------------------------------------------------------


def test_ensure_util_downloads_extracts_and_records_the_install(
    tmp_path: Path, network: _FakeNetwork
) -> None:
    spec = UTIL_SPECS["ripgrep"]
    install = ensure_util(tmp_path, "ripgrep")

    assert install.name == "ripgrep"
    assert install.version == spec.version
    assert install.path == tmp_path / "bin" / "ripgrep" / spec.binary
    assert install.path.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert os.access(install.path, os.X_OK)
    # The archive is a transient download, not left lying in bin/.
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["ripgrep", "ripgrep.json"]

    manifest = json.loads((tmp_path / "bin" / "ripgrep.json").read_text(encoding="utf-8"))
    assert manifest == {
        "name": "ripgrep",
        "version": spec.version,
        "path": str(install.path),
        "download_url": _url_for("ripgrep"),
    }
    assert find_util(tmp_path, "ripgrep") == install


def test_ensure_util_is_a_noop_when_the_pinned_version_is_present(
    tmp_path: Path, network: _FakeNetwork
) -> None:
    first = ensure_util(tmp_path, "fd")
    network.payloads.clear()  # any second download would now fail
    assert ensure_util(tmp_path, "fd") == first


def test_ensure_util_reinstalls_when_the_manifest_records_another_version(
    tmp_path: Path, network: _FakeNetwork
) -> None:
    install = ensure_util(tmp_path, "uv")
    _write_manifest(tmp_path, "uv", {"name": "uv", "version": "0.0.1", "path": str(install.path)})

    assert ensure_util(tmp_path, "uv").version == UTIL_SPECS["uv"].version
    assert find_util(tmp_path, "uv") == install


def test_ensure_util_rejects_an_unknown_util(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        ensure_util(tmp_path, "not-a-util")


def test_ensure_util_fails_when_the_archive_lacks_the_binary(
    tmp_path: Path, network: _FakeNetwork
) -> None:
    network.payloads[_url_for("uv")] = _tar_gz({"release/README": b"no binary here"})

    with pytest.raises(RuntimeError, match="not found in downloaded archive"):
        ensure_util(tmp_path, "uv")

    assert find_util(tmp_path, "uv") is None
    assert not (tmp_path / "bin" / "uv.json").exists()
    assert not any(p.name.endswith(".tar.gz") for p in (tmp_path / "bin").iterdir())


def test_ensure_util_propagates_a_download_failure(tmp_path: Path, network: _FakeNetwork) -> None:
    network.payloads.clear()
    with pytest.raises(OSError, match="404"):
        ensure_util(tmp_path, "uv")
    assert find_util(tmp_path, "uv") is None


def test_ensure_util_on_an_unsupported_os_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: _FakeNetwork
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Plan9")
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        ensure_util(tmp_path, "uv")
    assert network.requested == []


def test_ensure_util_without_a_target_for_this_platform_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: _FakeNetwork
) -> None:
    monkeypatch.setitem(UTIL_SPECS, "uv", dataclasses.replace(UTIL_SPECS["uv"], targets={}))
    with pytest.raises(RuntimeError, match="no release target"):
        ensure_util(tmp_path, "uv")
    assert network.requested == []


@pytest.mark.parametrize(
    ("system", "machine", "target_key"),
    [
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Darwin", "arm64", "darwin-aarch64"),
        ("Darwin", "AMD64", "darwin-x86_64"),
        ("Windows", "ARM64", "windows-aarch64"),
    ],
)
def test_ensure_util_requests_the_release_for_the_host_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: _FakeNetwork,
    system: str,
    machine: str,
    target_key: str,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    spec = UTIL_SPECS["fd"]
    target = spec.targets[target_key]
    network.payloads.clear()  # only the requested URL matters here

    with pytest.raises(OSError):
        ensure_util(tmp_path, "fd")

    assert len(network.requested) == 1
    assert target in network.requested[0]
    assert network.requested[0].startswith("https://github.com/sharkdp/fd/releases/download/")


# ---------------------------------------------------------------------------
# ensure_all_utils
# ---------------------------------------------------------------------------


def test_ensure_all_utils_installs_every_util(tmp_path: Path, network: _FakeNetwork) -> None:
    installed = ensure_all_utils(tmp_path)
    assert sorted(installed) == sorted(UTIL_SPECS)
    for name, install in installed.items():
        assert find_util(tmp_path, name) == install


def test_ensure_all_utils_skips_a_failing_util_and_keeps_the_rest(
    tmp_path: Path, network: _FakeNetwork
) -> None:
    del network.payloads[_url_for("ripgrep")]
    installed = ensure_all_utils(tmp_path)
    assert sorted(installed) == sorted(set(UTIL_SPECS) - {"ripgrep"})
    assert find_util(tmp_path, "ripgrep") is None
