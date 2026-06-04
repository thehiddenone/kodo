"""llama.cpp binary installer.

Downloads the latest llama.cpp release from GitHub and installs it into
``~/.kodo/llama.cpp/b{N}/`` for the running platform.  Supports Windows
(x64), macOS (arm64 and x64), and Linux (x64 via the Ubuntu build).

Installation state is recorded in ``~/.kodo/llama.cpp/llama-meta.json``
which is the single source of truth for installed build, executable path,
and download URLs.  :func:`find_installed` reads this file; filesystem
directory scanning is not used.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

__all__ = [
    "LlamaInstall",
    "check_llamacpp_update",
    "find_installed",
    "install_llamacpp",
    "server_executable",
    "uninstall_llamacpp",
    "update_llamacpp",
]

_log = logging.getLogger(__name__)

_GITHUB_RELEASES_LATEST = "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
_RELEASE_BASE = "https://github.com/ggerganov/llama.cpp/releases/download"
_USER_AGENT = "kodo-llm-utils/0.1 (github.com/thehiddenone/kodo)"
_META_FILE = "llama-meta.json"

# Asset filename templates per platform. {N} is replaced with the build number.
_ASSET_NAMES: dict[str, str] = {
    "win-x64": "llama-b{N}-bin-win-cuda-13.3-x64.zip",
    "macos-arm64": "llama-b{N}-bin-macos-arm64.zip",
    "macos-x64": "llama-b{N}-bin-macos-x64.zip",
    "linux-x64": "llama-b{N}-bin-ubuntu-x64.zip",
}

_WINDOWS_CUDA_DLLS_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b{N}/cudart-llama-bin-win-cuda-13.3-x64.zip"

ProgressCb = Callable[[int, str], None]


@dataclass(frozen=True)
class LlamaInstall:
    """Metadata for an installed llama.cpp build.

    Attributes:
        build: Build number (e.g. ``5143`` for tag ``b5143``).
        install_dir: Directory containing the extracted build.
        executable: Path to the ``llama-server`` binary.
    """

    build: int
    install_dir: Path
    executable: Path


# ---------------------------------------------------------------------------
# Meta-file I/O
# ---------------------------------------------------------------------------


def _meta_path(kodo_dir: Path) -> Path:
    return kodo_dir / "llama.cpp" / _META_FILE


def _read_llama_meta(kodo_dir: Path) -> dict[str, object] | None:
    p = _meta_path(kodo_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return cast(dict[str, object], data)
    except Exception:
        pass
    return None


def _write_llama_meta(
    kodo_dir: Path,
    build: int,
    executable: Path,
    binary_url: str,
    cuda_dlls_url: str | None,
) -> None:
    urls: dict[str, str] = {"binary": binary_url}
    if cuda_dlls_url is not None:
        urls["cuda_dlls"] = cuda_dlls_url
    data: dict[str, object] = {
        "build": build,
        "executable": str(executable),
        "urls": urls,
    }
    p = _meta_path(kodo_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _current_platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return "win-x64"
    if system == "Darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x64"
    if system == "Linux":
        return "linux-x64"
    raise RuntimeError(f"Unsupported platform: {system!r}")


def _asset_url(build_number: int, platform_key: str) -> tuple[str, str]:
    asset_name = _ASSET_NAMES[platform_key].format(N=build_number)
    url = f"{_RELEASE_BASE}/b{build_number}/{asset_name}"
    return asset_name, url


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------


def _fetch_latest_build_number() -> int:
    """Fetch the latest llama.cpp build number from GitHub Releases.

    Returns:
        int: Build number (e.g. ``5143`` for tag ``b5143``).

    Raises:
        RuntimeError: If the tag name cannot be parsed.
    """
    req = urllib.request.Request(
        _GITHUB_RELEASES_LATEST,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: object = json.loads(resp.read())
    tag = str(cast(dict[str, object], data)["tag_name"])
    match = re.match(r"^b(\d+)$", tag)
    if not match:
        raise RuntimeError(f"Cannot parse build number from GitHub tag {tag!r}")
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _url_accessible(url: str) -> bool:
    """Return ``True`` if *url* responds to an HTTP HEAD request with 2xx status.

    Args:
        url (str): URL to probe.

    Returns:
        bool: ``True`` if the server returns a 2xx response, ``False`` otherwise.
    """
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(200 <= int(resp.status) < 300)
    except Exception:
        return False


def _download(
    url: str,
    dest: Path,
    progress_cb: ProgressCb | None = None,
    pct_start: int = 0,
    pct_end: int = 100,
) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as resp:
        content_length = resp.headers.get("Content-Length")
        total = int(content_length) if content_length else 0
        downloaded = 0
        with dest.open("wb") as f:
            while True:
                chunk: bytes = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total and progress_cb:
                    raw_pct = downloaded * 100 // total
                    scaled = pct_start + (pct_end - pct_start) * raw_pct // 100
                    mb_done = downloaded // 1_048_576
                    mb_total = total // 1_048_576
                    progress_cb(scaled, f"{mb_done} / {mb_total} MB")
    _log.info("Downloaded %d bytes to %s", downloaded, dest)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_installed(kodo_dir: Path) -> LlamaInstall | None:
    """Return metadata for the currently installed llama.cpp build.

    Reads ``~/.kodo/llama.cpp/llama-meta.json``; does not scan the filesystem.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        LlamaInstall | None: Install metadata, or ``None`` if not installed.
    """
    meta = _read_llama_meta(kodo_dir)
    if meta is None:
        return None
    try:
        build = int(cast(int, meta["build"]))
        executable = Path(str(meta["executable"]))
        install_dir = kodo_dir / "llama.cpp" / f"b{build}"
        return LlamaInstall(build=build, install_dir=install_dir, executable=executable)
    except (KeyError, ValueError):
        return None


def server_executable(install_dir: Path) -> Path | None:
    """Find the ``llama-server`` executable inside an install directory.

    Used during installation to locate the binary before writing the meta file.

    Args:
        install_dir (Path): A llama.cpp build directory.

    Returns:
        Path | None: Absolute path to the executable, or ``None`` if not found.
    """
    exe_name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    for candidate in install_dir.rglob(exe_name):
        return candidate
    return None


def verify_executable(executable: Path) -> bool:
    """Verify that a ``llama-server`` binary is functional.

    Runs ``llama-server --version`` and checks for exit code 0.

    Args:
        executable (Path): Path to the ``llama-server`` binary.

    Returns:
        bool: ``True`` if the binary runs successfully.
    """
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_llamacpp_update(kodo_dir: Path) -> bool:
    """Check whether a newer llama.cpp build is available on GitHub.

    Fetches the latest build number, compares it to the installed build, and —
    only when a newer build is found — validates that all platform-specific
    download URLs are accessible via HTTP HEAD.  Returns ``True`` only when
    both conditions hold: a newer build exists *and* every required URL
    responds with 2xx.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        bool: ``True`` if an update is available and all download URLs are
        reachable.  ``False`` if already up to date or any URL is unreachable.
    """
    latest = _fetch_latest_build_number()
    installed = find_installed(kodo_dir)
    installed_build = installed.build if installed is not None else None
    _log.info(
        "llama.cpp: latest=b%d  installed=%s",
        latest,
        f"b{installed_build}" if installed_build is not None else "none",
    )

    if installed_build is not None and installed_build >= latest:
        return False

    platform_key = _current_platform_key()
    _, binary_url = _asset_url(latest, platform_key)
    urls: list[str] = [binary_url]
    if platform_key == "win-x64":
        urls.append(_WINDOWS_CUDA_DLLS_URL.format(N=latest))

    for url in urls:
        if not _url_accessible(url):
            _log.warning("llama.cpp b%d URL not accessible: %s", latest, url)
            return False

    return True


def install_llamacpp(
    kodo_dir: Path,
    *,
    progress_cb: ProgressCb | None = None,
) -> LlamaInstall:
    """Download and install the latest llama.cpp release for the current platform.

    Fetches the latest build number from GitHub, downloads and extracts the
    platform binary, verifies it runs, then writes ``llama-meta.json``.  If
    the same build is already installed the download is skipped.

    Progress is reported via *progress_cb* as ``(percent: int, message: str)``
    calls.  ``percent == 100`` signals success; ``percent == -1`` signals an
    error (the message contains the reason).

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        progress_cb (ProgressCb | None): Optional progress callback.

    Returns:
        LlamaInstall: Metadata for the installed build.

    Raises:
        RuntimeError: If download, extraction, or verification fails.
    """

    def _progress(pct: int, msg: str) -> None:
        _log.info("[%3d%%] %s", pct, msg)
        if progress_cb is not None:
            progress_cb(pct, msg)

    def _fail(msg: str) -> RuntimeError:
        _progress(-1, msg)
        return RuntimeError(msg)

    try:
        _progress(0, "Fetching latest release info from GitHub…")
        build_number = _fetch_latest_build_number()

        existing = find_installed(kodo_dir)
        if existing is not None and existing.build == build_number:
            _progress(100, f"llama.cpp b{build_number} already installed")
            return existing

        platform_key = _current_platform_key()
        asset_name, binary_url = _asset_url(build_number, platform_key)
        _progress(5, f"Installing llama.cpp b{build_number} ({platform_key})")

        install_dir = kodo_dir / "llama.cpp" / f"b{build_number}"
        install_dir.mkdir(parents=True, exist_ok=True)
        zip_path = install_dir / asset_name

        _progress(10, f"Downloading {asset_name}…")
        _download(binary_url, zip_path, progress_cb, pct_start=10, pct_end=75)

        _progress(75, "Extracting binary archive…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(install_dir)
        zip_path.unlink()

        cuda_dlls_url: str | None = None
        if platform_key == "win-x64":
            cuda_dlls_url = _WINDOWS_CUDA_DLLS_URL.format(N=build_number)
            cuda_zip_name = cuda_dlls_url.rsplit("/", 1)[-1]
            cuda_zip_path = install_dir / cuda_zip_name
            _progress(80, "Downloading CUDA runtime DLLs…")
            _download(cuda_dlls_url, cuda_zip_path, progress_cb, pct_start=80, pct_end=88)
            _progress(88, "Extracting CUDA DLLs…")
            with zipfile.ZipFile(cuda_zip_path, "r") as zf:
                zf.extractall(install_dir)
            cuda_zip_path.unlink()

        _progress(90, "Locating llama-server executable…")
        exe = server_executable(install_dir)
        if exe is None:
            raise _fail("llama-server executable not found after extraction")

        _progress(95, "Verifying llama-server --version…")
        if not verify_executable(exe):
            raise _fail("llama-server --version returned non-zero exit code")

        _progress(98, "Writing installation metadata…")
        _write_llama_meta(kodo_dir, build_number, exe, binary_url, cuda_dlls_url)

        result = LlamaInstall(build=build_number, install_dir=install_dir, executable=exe)
        _progress(100, f"llama.cpp b{build_number} installed successfully")
        return result

    except RuntimeError:
        raise
    except Exception as exc:
        raise _fail(f"Installation failed: {exc}") from exc


def uninstall_llamacpp(kodo_dir: Path) -> None:
    """Remove the current llama.cpp installation from ``~/.kodo``.

    Deletes the build directory recorded in ``llama-meta.json`` and then
    removes ``llama-meta.json`` itself.  Does nothing if llama.cpp is not
    installed.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
    """
    installed = find_installed(kodo_dir)
    if installed is None:
        _log.info("llama.cpp is not installed — nothing to uninstall")
        return

    _log.info("Uninstalling llama.cpp b%d from %s", installed.build, installed.install_dir)
    if installed.install_dir.exists():
        import shutil

        shutil.rmtree(installed.install_dir)
        _log.info("Removed %s", installed.install_dir)

    meta = _meta_path(kodo_dir)
    if meta.exists():
        meta.unlink()
        _log.info("Removed %s", meta)


def update_llamacpp(
    kodo_dir: Path,
    *,
    progress_cb: ProgressCb | None = None,
) -> LlamaInstall:
    """Uninstall the current llama.cpp build and install the latest one.

    Equivalent to calling :func:`uninstall_llamacpp` followed by
    :func:`install_llamacpp`.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        progress_cb (ProgressCb | None): Optional progress callback forwarded
            to :func:`install_llamacpp`.

    Returns:
        LlamaInstall: Metadata for the newly installed build.

    Raises:
        RuntimeError: If the installation step fails.
    """
    uninstall_llamacpp(kodo_dir)
    return install_llamacpp(kodo_dir, progress_cb=progress_cb)
