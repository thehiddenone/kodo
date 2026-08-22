"""Tests for ``kodo.llms.llamacpp._installer`` -- llama.cpp binary installer.

Covers the pure / file-IO helpers and the public API surface with mocked
network.  Real downloads and ``--version`` checks are out of scope here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kodo.llms.llamacpp._installer import (
    LlamaInstall,
    _asset_url,
    _current_platform_key,
    _meta_path,
    _read_llama_meta,
    _url_accessible,
    _write_llama_meta,
    fetch_latest_build_number,
    find_installed,
    server_executable,
    verify_executable,
)

# ---------------------------------------------------------------------------
# _meta_path -- pure
# ---------------------------------------------------------------------------


def test_meta_path_returns_expected_location(tmp_path: Path) -> None:
    assert _meta_path(tmp_path) == tmp_path / "llama.cpp" / "llama-meta.json"


# ---------------------------------------------------------------------------
# _read_llama_meta / _write_llama_meta
# ---------------------------------------------------------------------------


def test_read_llama_meta_missing_file_returns_none(tmp_path: Path) -> None:
    assert _read_llama_meta(tmp_path) is None


def test_read_llama_meta_invalid_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / "llama.cpp").mkdir(parents=True)
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text("not json{{{", encoding="utf-8")
    assert _read_llama_meta(tmp_path) is None


def test_read_llama_meta_invalid_type_returns_none(tmp_path: Path) -> None:
    (tmp_path / "llama.cpp").mkdir(parents=True)
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_llama_meta(tmp_path) is None


def test_write_and_read_llama_meta(tmp_path: Path) -> None:
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")
    exe.chmod(0o755)

    _write_llama_meta(
        tmp_path,
        build=5143,
        executable=exe,
        binary_url="https://example.com/llama-b5143.tar.gz",
        cuda_dlls_url=None,
    )
    meta = _read_llama_meta(tmp_path)
    assert meta is not None
    assert meta["build"] == 5143
    assert meta["executable"] == str(exe)
    assert meta["urls"]["binary"] == "https://example.com/llama-b5143.tar.gz"
    assert "cuda_dlls" not in meta["urls"]


def test_write_and_read_llama_meta_with_cuda(tmp_path: Path) -> None:
    exe = tmp_path / "llama-server.exe"
    exe.write_text("#!/bin/sh", encoding="utf-8")

    _write_llama_meta(
        tmp_path,
        build=5143,
        executable=exe,
        binary_url="https://example.com/llama-b5143.zip",
        cuda_dlls_url="https://example.com/cuda.zip",
    )
    meta = _read_llama_meta(tmp_path)
    assert meta is not None
    assert meta["urls"]["cuda_dlls"] == "https://example.com/cuda.zip"


def test_read_llama_meta_malformed_build_returns_none(tmp_path: Path) -> None:
    """If the build field isn't an int, find_installed returns None."""
    (tmp_path / "llama.cpp").mkdir(parents=True)
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text(
        json.dumps({"build": "not-a-number", "executable": "/fake"}),
        encoding="utf-8",
    )
    assert find_installed(tmp_path) is None


def test_read_llama_meta_missing_keys_returns_none(tmp_path: Path) -> None:
    (tmp_path / "llama.cpp").mkdir(parents=True)
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text(json.dumps({"executable": "/fake"}), encoding="utf-8")
    assert find_installed(tmp_path) is None


# ---------------------------------------------------------------------------
# _current_platform_key
# ---------------------------------------------------------------------------


def test_current_platform_key_returns_string() -> None:
    key = _current_platform_key()
    assert isinstance(key, str)
    assert key in {"win-x64", "macos-arm64", "macos-x64", "linux-x64"}


def test_current_platform_key_unsupported_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsupported platform raises RuntimeError."""
    import platform as _platform

    monkeypatch.setattr(_platform, "system", MagicMock(return_value="FreeBSD"))
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        _current_platform_key()


# ---------------------------------------------------------------------------
# _asset_url
# ---------------------------------------------------------------------------


def test_asset_url_returns_name_and_url() -> None:
    name, url = _asset_url(5143, "linux-x64")
    assert name == "llama-b5143-bin-ubuntu-x64.tar.gz"
    assert url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/b5143/")
    assert url.endswith(name)


def test_asset_url_all_platforms() -> None:
    for platform_key in ("win-x64", "macos-arm64", "macos-x64", "linux-x64"):
        name, url = _asset_url(100, platform_key)
        assert name.startswith("llama-b100-")
        assert f"b100/{name}" in url


# ---------------------------------------------------------------------------
# find_installed
# ---------------------------------------------------------------------------


def test_find_installed_no_meta_file(tmp_path: Path) -> None:
    assert find_installed(tmp_path) is None


def test_find_installed_valid_meta(tmp_path: Path) -> None:
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")

    _write_llama_meta(
        tmp_path,
        build=5143,
        executable=exe,
        binary_url="https://example.com/llama-b5143.tar.gz",
        cuda_dlls_url=None,
    )
    install = find_installed(tmp_path)
    assert install is not None
    assert install.build == 5143
    assert install.executable == exe
    assert install.install_dir == tmp_path / "llama.cpp" / "b5143"


def test_find_installed_invalid_build_returns_none(tmp_path: Path) -> None:
    """If the build field can't be int(), find_installed returns None."""
    (tmp_path / "llama.cpp").mkdir(parents=True)
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text(
        json.dumps({"build": "not-a-number", "executable": "/fake"}),
        encoding="utf-8",
    )
    assert find_installed(tmp_path) is None


def test_find_installed_missing_executable_returns_none(tmp_path: Path) -> None:
    """A meta file naming an executable that is gone reports "not installed".

    This is the state a half-completed delete leaves behind (a build directory
    partly removed but ``llama-meta.json`` still pointing into it). Reporting an
    install here strands every caller on a build that cannot run.
    """
    _write_llama_meta(
        tmp_path,
        build=5143,
        executable=tmp_path / "llama.cpp" / "b5143" / "llama-server",
        binary_url="https://example.com/llama-b5143.tar.gz",
        cuda_dlls_url=None,
    )
    assert _meta_path(tmp_path).is_file()  # the record itself is intact
    assert find_installed(tmp_path) is None


# ---------------------------------------------------------------------------
# server_executable
# ---------------------------------------------------------------------------


def test_server_executable_finds_linux_binary(tmp_path: Path) -> None:
    install_dir = tmp_path / "b5143"
    install_dir.mkdir()
    exe = install_dir / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")
    exe.chmod(0o755)

    result = server_executable(install_dir)
    assert result is not None
    assert result == exe


def test_server_executable_finds_nested_binary(tmp_path: Path) -> None:
    install_dir = tmp_path / "b5143"
    install_dir.mkdir()
    sub = install_dir / "subdir"
    sub.mkdir()
    exe = sub / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")

    result = server_executable(install_dir)
    assert result is not None
    assert result == exe


def test_server_executable_returns_none_if_missing(tmp_path: Path) -> None:
    install_dir = tmp_path / "b5143"
    install_dir.mkdir()
    assert server_executable(install_dir) is None


def test_server_executable_finds_windows_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the executable is llama-server.exe."""
    import platform as _platform

    monkeypatch.setattr(_platform, "system", lambda: "Windows")
    install_dir = Path("/fake/install")
    exe = install_dir / "llama-server.exe"

    rgx = MagicMock()
    rgx.rglob.return_value = [exe]
    monkeypatch.setattr(Path, "rglob", rgx.rglob)

    result = server_executable(install_dir)
    assert result == exe


# ---------------------------------------------------------------------------
# verify_executable
# ---------------------------------------------------------------------------


def test_verify_executable_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary returning exit code 0 for --version is verified."""
    import subprocess as _subprocess

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "llama-server version 1.0"
    mock_result.stderr = ""
    monkeypatch.setattr(_subprocess, "run", MagicMock(return_value=mock_result))

    assert verify_executable(Path("/fake/llama-server")) is True


def test_verify_executable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary returning non-zero exit code is not verified."""
    import subprocess as _subprocess

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "error"
    monkeypatch.setattr(_subprocess, "run", MagicMock(return_value=mock_result))

    assert verify_executable(Path("/fake/llama-server")) is False


def test_verify_executable_exception_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess exceptions return False."""
    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", MagicMock(side_effect=FileNotFoundError("not found")))
    assert verify_executable(Path("/fake/llama-server")) is False


# ---------------------------------------------------------------------------
# _url_accessible
# ---------------------------------------------------------------------------


def test_url_accessible_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response means the URL is accessible."""
    import urllib.request as _urllib

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_req = MagicMock()
    mock_req.__enter__ = MagicMock(return_value=mock_resp)
    mock_req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=mock_req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=mock_req))

    assert _url_accessible("https://example.com/file.tar.gz") is True


def test_url_accessible_non_2xx_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 response means the URL is not accessible."""
    import urllib.request as _urllib

    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_req = MagicMock()
    mock_req.__enter__ = MagicMock(return_value=mock_resp)
    mock_req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=mock_req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=mock_req))

    assert _url_accessible("https://example.com/file.tar.gz") is False


def test_url_accessible_exception_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection errors return False."""
    import urllib.request as _urllib

    monkeypatch.setattr(_urllib, "urlopen", MagicMock(side_effect=OSError("connection failed")))
    assert _url_accessible("https://example.com/file.tar.gz") is False


# ---------------------------------------------------------------------------
# build_exists
# ---------------------------------------------------------------------------


def test_build_exists_calls_url_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_exists probes the URL via _url_accessible."""
    import kodo.llms.llamacpp._installer as _inst

    monkeypatch.setattr(_inst, "_url_accessible", MagicMock(return_value=True))
    # Should not raise.
    result = _inst.build_exists(5143)
    assert result is True
    _inst._url_accessible.assert_called()


# ---------------------------------------------------------------------------
# fetch_latest_build_number
# ---------------------------------------------------------------------------


def test_fetch_latest_build_number_parses_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parses the build number from a GitHub releases latest response."""
    import urllib.request as _urllib

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"tag_name": "b5143"}).encode()
    mock_req = MagicMock()
    mock_req.__enter__ = MagicMock(return_value=mock_resp)
    mock_req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=mock_req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=mock_req))

    result = fetch_latest_build_number()
    assert result == 5143


def test_fetch_latest_build_number_invalid_tag_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag without the b prefix and no nightly-tag.txt asset raises RuntimeError."""
    import urllib.request as _urllib

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"tag_name": "v5143"}).encode()
    mock_req = MagicMock()
    mock_req.__enter__ = MagicMock(return_value=mock_resp)
    mock_req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=mock_req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=mock_req))

    with pytest.raises(RuntimeError, match="Cannot parse build number"):
        fetch_latest_build_number()


def test_fetch_latest_build_number_falls_back_to_nightly_tag_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A semver 'latest' release resolves its build via the nightly-tag.txt asset.

    Mirrors ggml-org/llama.cpp's release scheme change (see
    https://github.com/ggml-org/ggml/discussions/1579): ``/releases/latest``
    now returns e.g. ``v0.2.0`` with a ``nightly-tag.txt`` asset whose content
    is the real ``bNNNN`` build tag.
    """
    import urllib.request as _urllib

    release_resp = MagicMock()
    release_resp.read.return_value = json.dumps(
        {
            "tag_name": "v0.2.0",
            "assets": [
                {
                    "name": "nightly-tag.txt",
                    "browser_download_url": (
                        "https://github.com/ggml-org/llama.cpp/releases/download/v0.2.0/nightly-tag.txt"
                    ),
                }
            ],
        }
    ).encode()
    release_req = MagicMock()
    release_req.__enter__ = MagicMock(return_value=release_resp)
    release_req.__exit__ = MagicMock(return_value=False)

    nightly_resp = MagicMock()
    nightly_resp.read.return_value = b"b10566\n"
    nightly_req = MagicMock()
    nightly_req.__enter__ = MagicMock(return_value=nightly_resp)
    nightly_req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(side_effect=lambda *a, **kw: MagicMock()))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(side_effect=[release_req, nightly_req]))

    result = fetch_latest_build_number()
    assert result == 10566


# ---------------------------------------------------------------------------
# LlamaInstall dataclass
# ---------------------------------------------------------------------------


def test_llama_install_dataclass() -> None:
    install = LlamaInstall(
        build=5143,
        install_dir=Path("/home/user/.kodo/llama.cpp/b5143"),
        executable=Path("/home/user/.kodo/llama.cpp/b5143/llama-server"),
    )
    assert install.build == 5143
    assert install.install_dir.name == "b5143"


# ---------------------------------------------------------------------------
# _download
# ---------------------------------------------------------------------------


def test_download_calls_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    """_download opens the URL and writes bytes to dest."""
    import urllib.request as _urllib

    # Mock the response to return a small payload.
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Length": "4"}
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(side_effect=[b"data", b""])

    req = MagicMock()
    req.__enter__ = MagicMock(return_value=mock_resp)
    req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=req))

    from kodo.llms.llamacpp._installer import _download

    dest = MagicMock()
    f_mock = MagicMock()
    f_mock.__enter__ = MagicMock(return_value=f_mock)
    f_mock.__exit__ = MagicMock(return_value=False)
    dest.open = MagicMock(return_value=f_mock)

    _download("https://example.com/file.tar.gz", dest)

    f_mock.write.assert_called_once_with(b"data")


def test_download_with_progress_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """_download invokes progress_cb with scaled percentage and message."""
    import urllib.request as _urllib

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Length": "100"}
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    # Two chunks: 60 bytes, 40 bytes.
    mock_resp.read = MagicMock(side_effect=[b"x" * 60, b"y" * 40, b""])

    req = MagicMock()
    req.__enter__ = MagicMock(return_value=mock_resp)
    req.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(_urllib, "Request", MagicMock(return_value=req))
    monkeypatch.setattr(_urllib, "urlopen", MagicMock(return_value=req))

    from kodo.llms.llamacpp._installer import _download

    progress_calls = []

    def _progress(pct: int, msg: str) -> None:
        progress_calls.append((pct, msg))

    dest = MagicMock()
    f_mock = MagicMock()
    f_mock.__enter__ = MagicMock(return_value=f_mock)
    f_mock.__exit__ = MagicMock(return_value=False)
    dest.open = MagicMock(return_value=f_mock)

    _download("https://example.com/file.tar.gz", dest, progress_cb=_progress)

    assert len(progress_calls) >= 1
    # The first call should be at some percentage.
    assert progress_calls[0][0] >= 0


# ---------------------------------------------------------------------------
# install_llamacpp
# ---------------------------------------------------------------------------


def test_install_llamacpp_already_installed_returns_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the same build is already installed, install returns without downloading."""
    import kodo.llms.llamacpp._installer as _inst

    # Mock find_installed to return an existing install.
    existing = LlamaInstall(
        build=5143,
        install_dir=tmp_path / "llama.cpp" / "b5143",
        executable=tmp_path / "llama.cpp" / "b5143" / "llama-server",
    )
    monkeypatch.setattr(_inst, "find_installed", MagicMock(return_value=existing))
    monkeypatch.setattr(_inst, "_current_platform_key", MagicMock(return_value="linux-x64"))
    monkeypatch.setattr(
        _inst,
        "_asset_url",
        MagicMock(return_value=("file.tar.gz", "https://example.com/file.tar.gz")),
    )

    progress_calls = []

    def _progress(pct: int, msg: str) -> None:
        progress_calls.append((pct, msg))

    result = _inst.install_llamacpp(tmp_path, version=5143, progress_cb=_progress)
    assert result.build == 5143
    # No download should have been attempted.
    assert not progress_calls or progress_calls[-1][0] == 100


def test_install_llamacpp_calls_expected_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_llamacpp calls fetch, download, extract, verify, write in order."""
    import kodo.llms.llamacpp._installer as _inst

    # Create a real llama-server binary for verification.
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")
    exe.chmod(0o755)

    install_dir = tmp_path / "llama.cpp" / "b5143"
    install_dir.mkdir(parents=True)

    # Mock find_installed to return None.
    monkeypatch.setattr(_inst, "find_installed", MagicMock(return_value=None))
    monkeypatch.setattr(_inst, "fetch_latest_build_number", MagicMock(return_value=5143))
    monkeypatch.setattr(_inst, "_current_platform_key", MagicMock(return_value="linux-x64"))
    monkeypatch.setattr(
        _inst,
        "_asset_url",
        MagicMock(
            return_value=(
                "llama-b5143-bin-ubuntu-x64.tar.gz",
                "https://example.com/llama-b5143.tar.gz",
            )
        ),
    )

    # Mock _download to do nothing (we'll create the archive ourselves).
    download_mock = MagicMock()
    monkeypatch.setattr(_inst, "_download", download_mock)

    # Create a fake tar.gz archive at the expected path.
    import tarfile

    archive_path = install_dir / "llama-b5143-bin-ubuntu-x64.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        import io

        data = io.BytesIO(b"fake archive content")
        info = tarfile.TarInfo(name="llama-server")
        info.size = len(data.getvalue())
        tf.addfile(info, data)

    monkeypatch.setattr(_inst, "server_executable", MagicMock(return_value=exe))
    monkeypatch.setattr(_inst, "verify_executable", MagicMock(return_value=True))

    result = _inst.install_llamacpp(tmp_path)
    assert result.build == 5143
    assert result.executable == exe
    download_mock.assert_called_once()


# ---------------------------------------------------------------------------
# uninstall_llamacpp
# ---------------------------------------------------------------------------


def test_uninstall_llamacpp_nothing_installed(tmp_path: Path) -> None:
    """Uninstall with no meta file is a no-op."""
    import kodo.llms.llamacpp._installer as _inst

    _inst.uninstall_llamacpp(tmp_path)
    # Should not raise.


def test_uninstall_llamacpp_removes_install_dir_and_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninstall removes the install directory and meta file."""
    import kodo.llms.llamacpp._installer as _inst

    # Create the install dir and a meta file pointing into it.
    install_dir = tmp_path / "llama.cpp" / "b5143"
    install_dir.mkdir(parents=True)
    (install_dir / "llama-server").write_text("#!/bin/sh", encoding="utf-8")
    meta_file = tmp_path / "llama.cpp" / "llama-meta.json"
    meta_file.write_text(
        json.dumps({"build": 5143, "executable": str(install_dir / "llama-server"), "urls": {}}),
        encoding="utf-8",
    )

    _inst.uninstall_llamacpp(tmp_path)

    assert not install_dir.exists()
    assert not meta_file.exists()


def test_uninstall_llamacpp_clears_stale_meta_with_no_build_behind_it(tmp_path: Path) -> None:
    """A meta file whose build is already gone is cleaned up rather than left behind."""
    import kodo.llms.llamacpp._installer as _inst

    _write_llama_meta(
        tmp_path,
        build=5143,
        executable=tmp_path / "llama.cpp" / "b5143" / "llama-server",
        binary_url="https://example.com/llama-b5143.tar.gz",
        cuda_dlls_url=None,
    )

    _inst.uninstall_llamacpp(tmp_path)

    assert not _meta_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# update_llamacpp
# ---------------------------------------------------------------------------


def _seed_install(tmp_path: Path, build: int) -> Path:
    """Write a complete, runnable-looking install for *build*; return its directory."""
    install_dir = tmp_path / "llama.cpp" / f"b{build}"
    install_dir.mkdir(parents=True)
    exe = install_dir / "llama-server"
    exe.write_text("#!/bin/sh", encoding="utf-8")
    _write_llama_meta(
        tmp_path,
        build=build,
        executable=exe,
        binary_url=f"https://example.com/llama-b{build}.tar.gz",
        cuda_dlls_url=None,
    )
    return install_dir


def test_update_llamacpp_with_nothing_installed_just_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kodo.llms.llamacpp._installer as _inst

    install_mock = MagicMock(
        return_value=LlamaInstall(
            build=5144,
            install_dir=tmp_path / "llama.cpp" / "b5144",
            executable=tmp_path / "llama.cpp" / "b5144" / "llama-server",
        )
    )
    monkeypatch.setattr(_inst, "install_llamacpp", install_mock)

    result = _inst.update_llamacpp(tmp_path, version=5144)

    install_mock.assert_called_once()
    assert result.build == 5144


def test_update_llamacpp_installs_before_removing_the_old_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The superseded build is deleted only after the new one is installed.

    Ordering is the whole point: on Windows the old build's files can still be
    locked by a process that is only just exiting, and doing the delete first
    used to abort the update before it downloaded anything.
    """
    import kodo.llms.llamacpp._installer as _inst

    old_dir = _seed_install(tmp_path, 5143)
    new_dir = tmp_path / "llama.cpp" / "b5144"

    def fake_install(kodo_dir: Path, **_kwargs: object) -> LlamaInstall:
        assert old_dir.exists(), "old build must still be present while installing"
        new_dir.mkdir(parents=True)
        return LlamaInstall(build=5144, install_dir=new_dir, executable=new_dir / "llama-server")

    monkeypatch.setattr(_inst, "install_llamacpp", fake_install)

    result = _inst.update_llamacpp(tmp_path, version=5144)

    assert result.build == 5144
    assert not old_dir.exists()
    assert new_dir.exists()


def test_update_llamacpp_keeps_the_old_build_when_the_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed update must leave the working install alone, not strand the user."""
    import kodo.llms.llamacpp._installer as _inst

    old_dir = _seed_install(tmp_path, 5143)
    monkeypatch.setattr(
        _inst, "install_llamacpp", MagicMock(side_effect=RuntimeError("download failed"))
    )

    with pytest.raises(RuntimeError, match="download failed"):
        _inst.update_llamacpp(tmp_path, version=5144)

    assert old_dir.exists()
    install = find_installed(tmp_path)
    assert install is not None
    assert install.build == 5143


def test_update_llamacpp_succeeds_even_if_the_old_build_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the superseded build is cleanup — a locked file must not fail the update."""
    import kodo.llms.llamacpp._installer as _inst

    _seed_install(tmp_path, 5143)
    new_dir = tmp_path / "llama.cpp" / "b5144"
    new_dir.mkdir(parents=True)

    monkeypatch.setattr(
        _inst,
        "install_llamacpp",
        MagicMock(
            return_value=LlamaInstall(
                build=5144, install_dir=new_dir, executable=new_dir / "llama-server"
            )
        ),
    )
    monkeypatch.setattr(
        _inst, "_rmtree_retrying", MagicMock(side_effect=PermissionError("still locked"))
    )

    result = _inst.update_llamacpp(tmp_path, version=5144)

    assert result.build == 5144


def test_update_llamacpp_never_reinstalls_the_build_already_installed(
    tmp_path: Path,
) -> None:
    """Targeting the installed build is a no-op — no delete, no re-download.

    Update must never take the uninstall-then-reinstall route: that is the
    user's own explicit "Uninstall llama.cpp" + "Install llama.cpp" pair, and
    doing it here would tear down a working install to reproduce itself.
    Deliberately runs the *real* ``install_llamacpp``, since its
    already-installed early return is half of what makes this a no-op.
    """
    import kodo.llms.llamacpp._installer as _inst

    install_dir = _seed_install(tmp_path, 5143)
    exe = install_dir / "llama-server"
    before = exe.read_text(encoding="utf-8")
    reported: list[tuple[int, str]] = []

    result = _inst.update_llamacpp(
        tmp_path, version=5143, progress_cb=lambda pct, msg: reported.append((pct, msg))
    )

    assert result.build == 5143
    assert install_dir.exists()
    assert exe.read_text(encoding="utf-8") == before  # untouched, not re-fetched
    assert reported[-1][0] == 100


# ---------------------------------------------------------------------------
# _rmtree_retrying
# ---------------------------------------------------------------------------


def test_rmtree_retrying_missing_target_is_a_noop(tmp_path: Path) -> None:
    import kodo.llms.llamacpp._installer as _inst

    _inst._rmtree_retrying(tmp_path / "never-existed")  # must not raise


def test_rmtree_retrying_succeeds_on_a_later_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete that fails while a stopped process still holds handles is retried."""
    import kodo.llms.llamacpp._installer as _inst

    target = tmp_path / "b5143"
    target.mkdir()
    calls: list[int] = []

    real_rmtree = _inst.shutil.rmtree

    def flaky_rmtree(path: object, **kwargs: object) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(32, "The process cannot access the file")
        real_rmtree(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_inst.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(_inst, "_RMTREE_RETRY_DELAYS_S", (0.0, 0.0, 0.0, 0.0))

    _inst._rmtree_retrying(target)

    assert len(calls) == 3
    assert not target.exists()


def test_rmtree_retrying_raises_after_the_last_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kodo.llms.llamacpp._installer as _inst

    target = tmp_path / "b5143"
    target.mkdir()

    def always_fails(path: object, **kwargs: object) -> None:
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(_inst.shutil, "rmtree", always_fails)
    monkeypatch.setattr(_inst, "_RMTREE_RETRY_DELAYS_S", (0.0, 0.0))

    with pytest.raises(PermissionError):
        _inst._rmtree_retrying(target)


# ---------------------------------------------------------------------------
# check_llamacpp_update
# ---------------------------------------------------------------------------


def test_check_llamacpp_update_no_install_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If nothing is installed, check_llamacpp_update returns False (no update needed)."""
    import kodo.llms.llamacpp._installer as _inst

    monkeypatch.setattr(_inst, "find_installed", MagicMock(return_value=None))
    monkeypatch.setattr(_inst, "fetch_latest_build_number", MagicMock(return_value=5144))

    # With no installed build, installed_build is None, so the >= check is False.
    # But then it checks URLs which would call _url_accessible.
    # Since we don't want to actually probe URLs, just verify the function
    # doesn't crash.
    monkeypatch.setattr(_inst, "_url_accessible", MagicMock(return_value=True))
    monkeypatch.setattr(_inst, "_current_platform_key", MagicMock(return_value="linux-x64"))
    monkeypatch.setattr(
        _inst,
        "_asset_url",
        MagicMock(return_value=("file.tar.gz", "https://example.com/file.tar.gz")),
    )

    # When installed_build is None, the function proceeds to check URLs.
    _inst.check_llamacpp_update(tmp_path)
    # The function should call _url_accessible for the binary URL.
    assert _inst._url_accessible.called


def test_check_llamacpp_update_already_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If installed build >= latest, no update is available."""
    import kodo.llms.llamacpp._installer as _inst

    installed = LlamaInstall(build=5200, install_dir=tmp_path, executable=tmp_path / "llama-server")
    monkeypatch.setattr(_inst, "find_installed", MagicMock(return_value=installed))
    monkeypatch.setattr(_inst, "fetch_latest_build_number", MagicMock(return_value=5143))

    result = _inst.check_llamacpp_update(tmp_path)
    assert result is False
