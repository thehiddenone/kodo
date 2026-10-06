"""End-to-end behavioral tests for the llama.cpp installer's public API.

Drives :func:`install_llamacpp`, :func:`check_llamacpp_update`,
:func:`build_exists`, :func:`fetch_latest_build_number` and
:func:`uninstall_llamacpp` against a fake GitHub: ``urllib.request.urlopen``
is monkeypatched to serve real (tiny) ``.tar.gz``/``.zip`` release archives
built in memory, ``platform.system``/``platform.machine`` pick the platform,
and ``subprocess.run`` stands in for ``llama-server --version``. Nothing is
downloaded and no process is spawned.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodo.llms.llamacpp import (
    build_exists,
    check_llamacpp_update,
    fetch_latest_build_number,
    find_installed,
    install_llamacpp,
    uninstall_llamacpp,
)

_DOWNLOAD_RE = re.compile(r"/releases/download/b(\d+)/(.+)$")
_WIN_ASSETS = ("llama-b{N}-bin-win-cuda-13.3-x64.zip", "cudart-llama-bin-win-cuda-13.3-x64.zip")


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Length": str(len(body))}
        self.__body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self.__body.read(n)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclass
class _GitHub:
    """Fake GitHub releases API + release-asset host."""

    builds: set[int] = field(default_factory=set)
    releases: list[dict[str, object]] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)
    ship_server_binary: bool = True
    broken_archive: bool = False
    fetched: list[str] = field(default_factory=list)

    def urlopen(
        self, req: urllib.request.Request, timeout: float = 0, context: object = None
    ) -> _Response:
        url = req.full_url
        if "api.github.com" in url:
            page = int(url.rsplit("page=", 1)[1])
            return _Response(json.dumps(self.releases if page == 1 else []).encode())
        match = _DOWNLOAD_RE.search(url)
        if match is None or int(match.group(1)) not in self.builds:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        name = match.group(2)
        if any(m in name for m in self.missing):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        if req.get_method() == "HEAD":
            return _Response(b"")
        self.fetched.append(name)
        return _Response(self.__archive(name))

    def __archive(self, name: str) -> bytes:
        if self.broken_archive:
            return b"definitely not an archive"
        if name.startswith("cudart"):
            return _zip({"cudart64_13.dll": b"dll"})
        exe = {"build/bin/llama-server": b"#!bin"} if self.ship_server_binary else {}
        if name.endswith(".tar.gz"):
            return _tar_gz({"build/bin/README": b"hi", **exe})
        return _zip({k + ".exe": v for k, v in exe.items()} or {"README": b"hi"})


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> _GitHub:
    fake = _GitHub()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def version_ok(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """``llama-server --version`` outcome, settable per test (exit 0 by default)."""
    outcome = SimpleNamespace(returncode=0)

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        assert argv[1:] == ["--version"]
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    yield outcome


def _on_platform(monkeypatch: pytest.MonkeyPatch, system: str, machine: str = "x86_64") -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)


# ---------------------------------------------------------------------------
# install_llamacpp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "asset"),
    [
        ("Linux", "x86_64", "llama-b7001-bin-ubuntu-x64.tar.gz"),
        ("Darwin", "x86_64", "llama-b7001-bin-macos-x64.tar.gz"),
        ("Darwin", "arm64", "llama-b7001-bin-macos-arm64.tar.gz"),
    ],
)
def test_install_pinned_version_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: _GitHub,
    version_ok: SimpleNamespace,
    system: str,
    machine: str,
    asset: str,
) -> None:
    _on_platform(monkeypatch, system, machine)
    github.builds = {7001}
    progress: list[tuple[int, str]] = []

    install = install_llamacpp(
        tmp_path, version=7001, progress_cb=lambda p, m: progress.append((p, m))
    )

    assert install.build == 7001
    assert install.executable.name == "llama-server"
    assert install.executable.is_file()
    assert github.fetched == [asset]
    assert not (install.install_dir / asset).exists()  # archive cleaned up
    assert progress[0] == (0, "Installing llama.cpp b7001…")
    assert progress[-1][0] == 100
    assert find_installed(tmp_path) == install


def test_install_on_windows_also_fetches_cuda_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github: _GitHub, version_ok: SimpleNamespace
) -> None:
    _on_platform(monkeypatch, "Windows")
    github.builds = {7001}

    install = install_llamacpp(tmp_path, version=7001)

    assert install.executable.name == "llama-server.exe"
    assert (install.install_dir / "cudart64_13.dll").is_file()
    assert github.fetched == [_WIN_ASSETS[0].format(N=7001), _WIN_ASSETS[1]]
    meta = json.loads((tmp_path / "llama.cpp" / "llama-meta.json").read_text(encoding="utf-8"))
    assert meta["urls"]["cuda_dlls"].endswith("/b7001/" + _WIN_ASSETS[1])


def test_install_fails_when_the_archive_has_no_server_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github: _GitHub, version_ok: SimpleNamespace
) -> None:
    _on_platform(monkeypatch, "Linux")
    github.builds = {7001}
    github.ship_server_binary = False
    progress: list[tuple[int, str]] = []

    with pytest.raises(RuntimeError, match="executable not found"):
        install_llamacpp(tmp_path, version=7001, progress_cb=lambda p, m: progress.append((p, m)))

    assert progress[-1] == (-1, "llama-server executable not found after extraction")
    assert find_installed(tmp_path) is None


def test_install_fails_when_the_binary_does_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github: _GitHub, version_ok: SimpleNamespace
) -> None:
    _on_platform(monkeypatch, "Linux")
    github.builds = {7001}
    version_ok.returncode = 1

    with pytest.raises(RuntimeError, match="--version returned non-zero"):
        install_llamacpp(tmp_path, version=7001)
    assert find_installed(tmp_path) is None


@pytest.mark.parametrize("problem", ["missing-release", "corrupt-archive"])
def test_install_wraps_unexpected_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: _GitHub,
    version_ok: SimpleNamespace,
    problem: str,
) -> None:
    _on_platform(monkeypatch, "Linux")
    if problem == "corrupt-archive":
        github.builds = {7001}
        github.broken_archive = True
    progress: list[tuple[int, str]] = []

    with pytest.raises(RuntimeError, match="Installation failed"):
        install_llamacpp(tmp_path, version=7001, progress_cb=lambda p, m: progress.append((p, m)))

    assert progress[-1][0] == -1
    assert find_installed(tmp_path) is None


# ---------------------------------------------------------------------------
# fetch_latest_build_number / build_exists / check_llamacpp_update
# ---------------------------------------------------------------------------


def test_latest_build_on_windows_requires_the_cuda_runtime_asset(
    monkeypatch: pytest.MonkeyPatch, github: _GitHub
) -> None:
    _on_platform(monkeypatch, "Windows")
    github.releases = [
        # Newest build only has the binary — its CUDA runtime is not uploaded yet.
        {"tag_name": "b7002", "assets": [{"name": _WIN_ASSETS[0].format(N=7002)}]},
        {"tag_name": "b7001", "assets": [{"name": a.format(N=7001)} for a in _WIN_ASSETS]},
    ]

    assert fetch_latest_build_number() == 7001


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_build_exists_probes_every_required_asset(
    monkeypatch: pytest.MonkeyPatch, github: _GitHub, system: str
) -> None:
    _on_platform(monkeypatch, system)
    github.builds = {7001}

    assert build_exists(7001) is True
    assert build_exists(7002) is False
    github.missing = {"cudart"}
    assert build_exists(7001) is (system != "Windows")


def _seed_install(kodo_dir: Path, build: int) -> None:
    exe = kodo_dir / "llama.cpp" / f"b{build}" / "llama-server"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!bin", encoding="utf-8")
    (kodo_dir / "llama.cpp" / "llama-meta.json").write_text(
        json.dumps({"build": build, "executable": str(exe), "urls": {}}), encoding="utf-8"
    )


@pytest.mark.parametrize(("cuda_missing", "expected"), [(False, True), (True, False)])
def test_update_check_on_windows_requires_both_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: _GitHub,
    cuda_missing: bool,
    expected: bool,
) -> None:
    _on_platform(monkeypatch, "Windows")
    _seed_install(tmp_path, 7000)
    github.builds = {7001}
    github.releases = [
        {"tag_name": "b7001", "assets": [{"name": a.format(N=7001)} for a in _WIN_ASSETS]}
    ]
    if cuda_missing:
        github.missing = {"cudart"}

    assert check_llamacpp_update(tmp_path) is expected


# ---------------------------------------------------------------------------
# uninstall_llamacpp
# ---------------------------------------------------------------------------


def test_uninstall_clears_a_read_only_refusal_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose first unlink is refused (a read-only bit on Windows) gets its
    write bit restored and is deleted on the spot; the uninstall completes."""
    _seed_install(tmp_path, 7000)
    real_unlink = os.unlink
    refusals = {"left": 1}

    def flaky_unlink(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if os.path.basename(os.fspath(path)) == "llama-server" and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(13, "read-only", os.fspath(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", flaky_unlink)

    uninstall_llamacpp(tmp_path)

    assert find_installed(tmp_path) is None
    assert not (tmp_path / "llama.cpp" / "b7000").exists()
    assert not (tmp_path / "llama.cpp" / "llama-meta.json").exists()


def test_uninstall_recovers_from_a_file_refused_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file refused on both the first unlink and the in-place retry fails that
    attempt; the backoff retry then deletes it and the uninstall completes."""
    _seed_install(tmp_path, 7000)
    real_unlink = os.unlink
    refusals = {"left": 2}

    def flaky_unlink(path: str | os.PathLike[str], *args: object, **kwargs: object) -> None:
        if os.path.basename(os.fspath(path)) == "llama-server" and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(13, "read-only", os.fspath(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", flaky_unlink)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    uninstall_llamacpp(tmp_path)

    assert find_installed(tmp_path) is None
    assert not (tmp_path / "llama.cpp" / "b7000").exists()


@pytest.mark.skipif(not shutil.rmtree.avoids_symlink_attacks, reason="fd-based rmtree only")
def test_uninstall_unopenable_subdirectory_fails_cleanly_and_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdirectory rmtree can never open surfaces as the OSError it is (after
    the backoff retries), leaves the directory's permissions untouched, and keeps
    ``llama-meta.json`` in place."""
    _seed_install(tmp_path, 7000)
    lib = tmp_path / "llama.cpp" / "b7000" / "lib"
    lib.mkdir()
    mode_before = lib.stat().st_mode
    real_open = os.open

    def refusing_open(path: str | os.PathLike[str], *args: object, **kwargs: object) -> int:
        if os.path.basename(os.fspath(path)) == "lib":
            raise PermissionError(13, "denied", os.fspath(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", refusing_open)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        uninstall_llamacpp(tmp_path)
    monkeypatch.undo()

    assert lib.stat().st_mode == mode_before
    assert (tmp_path / "llama.cpp" / "llama-meta.json").is_file()


def test_uninstall_backs_off_and_retries_a_briefly_locked_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows can hold a just-stopped server's handles for a moment: the first
    whole-directory delete fails, a later one (after a backoff) succeeds."""
    _seed_install(tmp_path, 7000)
    real_rmtree = shutil.rmtree
    waits: list[float] = []
    refusals = {"left": 1}

    def flaky_rmtree(
        path: str | os.PathLike[str],
        *,
        onexc: Callable[[Callable[..., object], str, BaseException], object] | None = None,
    ) -> None:
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(32, "sharing violation", os.fspath(path))
        real_rmtree(path, onexc=onexc)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(time, "sleep", waits.append)

    uninstall_llamacpp(tmp_path)

    assert find_installed(tmp_path) is None
    assert not (tmp_path / "llama.cpp" / "b7000").exists()
    assert waits and waits[0] > 0
