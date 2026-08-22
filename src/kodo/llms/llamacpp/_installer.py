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
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import certifi

__all__ = [
    "LlamaInstall",
    "build_exists",
    "check_llamacpp_update",
    "fetch_latest_build_number",
    "find_installed",
    "install_llamacpp",
    "server_executable",
    "uninstall_llamacpp",
    "update_llamacpp",
]

_log = logging.getLogger(__name__)

_GITHUB_RELEASES_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_RELEASE_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"
_USER_AGENT = "kodo-llm-utils/0.1 (github.com/thehiddenone/kodo)"
_META_FILE = "llama-meta.json"

# Some Windows Python builds (notably uv-managed interpreters) don't wire the
# stdlib ssl module up to the Windows certificate store, so the default
# context finds no trusted CAs at all. Pin it to certifi's bundle explicitly
# rather than relying on OS trust-store discovery.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Asset filename templates per platform. {N} is replaced with the build number.
_ASSET_NAMES: dict[str, str] = {
    "win-x64": "llama-b{N}-bin-win-cuda-13.3-x64.zip",
    "macos-arm64": "llama-b{N}-bin-macos-arm64.tar.gz",
    "macos-x64": "llama-b{N}-bin-macos-x64.tar.gz",
    "linux-x64": "llama-b{N}-bin-ubuntu-x64.tar.gz",
}

_WINDOWS_CUDA_DLLS_URL = "https://github.com/ggml-org/llama.cpp/releases/download/b{N}/cudart-llama-bin-win-cuda-13.3-x64.zip"

# Backoff between attempts to delete a build directory. Stopping a process is
# asynchronous on Windows — TerminateProcess returns before the kernel has
# released the handles it held on its mapped .exe/.dll images — so a delete
# issued right after stopping llama-server can still hit a sharing violation
# for a moment even though nothing really uses the files any more.
_RMTREE_RETRY_DELAYS_S: tuple[float, ...] = (0.0, 0.5, 1.5, 3.0)

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


def fetch_latest_build_number() -> int:
    """Fetch the latest llama.cpp build number from GitHub Releases.

    ``/releases/latest`` used to always be a rolling ``bNNNN`` tag. ggml-org
    now publishes it as a semver wrapper (e.g. ``v0.2.0``) that points at the
    real nightly build via a ``nightly-tag.txt`` release asset instead — see
    https://github.com/ggml-org/ggml/discussions/1579. Handle both shapes:
    parse the tag directly when it still matches ``bNNNN``, otherwise fetch
    ``nightly-tag.txt`` and parse that.

    Returns:
        int: Build number (e.g. ``5143`` for tag ``b5143``).

    Raises:
        RuntimeError: If the build number cannot be determined either way.
    """
    req = urllib.request.Request(
        _GITHUB_RELEASES_LATEST,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        release = cast(dict[str, object], json.loads(resp.read()))
    tag = str(release["tag_name"])
    match = re.match(r"^b(\d+)$", tag)
    if match:
        return int(match.group(1))

    assets = cast("list[dict[str, object]]", release.get("assets") or [])
    nightly_url = next(
        (str(a["browser_download_url"]) for a in assets if a.get("name") == "nightly-tag.txt"),
        None,
    )
    if nightly_url is None:
        raise RuntimeError(f"Cannot parse build number from GitHub tag {tag!r}")

    req = urllib.request.Request(nightly_url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        nightly_tag = resp.read().decode().strip()
    match = re.match(r"^b(\d+)$", nightly_tag)
    if not match:
        raise RuntimeError(f"Cannot parse build number from nightly tag {nightly_tag!r}")
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
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
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
    _log.info(f"Starting download from {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=600, context=_SSL_CONTEXT) as resp:
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
# Build-directory removal
# ---------------------------------------------------------------------------


def _on_rmtree_error(func: Any, path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` ``onexc`` hook: clear the read-only bit and retry once.

    A read-only file makes ``os.unlink`` raise ``PermissionError`` on Windows
    no matter who else has it open, so this is worth trying before giving up.
    Re-raises whatever *func* raises the second time, which is what lets
    :func:`_rmtree_retrying` see a genuine sharing violation and back off.
    """
    with suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree_retrying(target: Path) -> None:
    """Recursively delete *target*, retrying while Windows still holds handles.

    A no-op if *target* doesn't exist. See :data:`_RMTREE_RETRY_DELAYS_S` for
    why the retries are needed at all — on POSIX the first attempt always
    wins, since unlinking a file some process still has open is legal there.

    Args:
        target (Path): Directory to remove.

    Raises:
        OSError: If *target* still could not be removed after the last retry.
    """
    if not target.exists():
        return

    last_error: OSError | None = None
    for attempt, delay in enumerate(_RMTREE_RETRY_DELAYS_S, start=1):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(target, onexc=_on_rmtree_error)
            return
        except OSError as exc:
            last_error = exc
            _log.warning(
                "Could not remove %s (attempt %d/%d): %s",
                target,
                attempt,
                len(_RMTREE_RETRY_DELAYS_S),
                exc,
            )

    assert last_error is not None  # the loop body always sets it before falling through
    raise last_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_installed(kodo_dir: Path) -> LlamaInstall | None:
    """Return metadata for the currently installed llama.cpp build.

    Reads ``~/.kodo/llama.cpp/llama-meta.json``; does not scan the filesystem,
    beyond confirming that the executable the meta file names is still there.

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
    except (KeyError, ValueError):
        return None

    if not executable.is_file():
        # The meta file outlived the build it points at — the fingerprint of
        # an update whose delete step ran partway and then failed. Every
        # caller (ensure_llama_running, start_titling, the settings panel's
        # version display, and the update handler's "already up to date"
        # short-circuit) treats a returned LlamaInstall as runnable, so
        # reporting one here strands the user on a build that cannot start
        # and that "Update llama.cpp" then refuses to touch. Report "not
        # installed" instead, which a plain Install repairs.
        _log.warning(
            "llama-meta.json records build b%d at %s, but that executable is gone — "
            "treating llama.cpp as not installed",
            build,
            executable,
        )
        return None

    return LlamaInstall(build=build, install_dir=install_dir, executable=executable)


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
    latest = fetch_latest_build_number()
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


def build_exists(build_number: int) -> bool:
    """Return ``True`` if a llama.cpp release exists for *build_number*.

    Probes the current platform's binary asset URL (and, on Windows, the CUDA
    runtime DLL asset) via HTTP HEAD — same accessibility check
    :func:`check_llamacpp_update` runs against the latest build, just against
    an arbitrary pinned one. Used to validate a "Install specific version"
    request *before* the current installation is touched.

    Args:
        build_number (int): Release build number, e.g. ``12345`` for tag ``b12345``.

    Returns:
        bool: ``True`` if every required asset responds with 2xx.
    """
    platform_key = _current_platform_key()
    _, binary_url = _asset_url(build_number, platform_key)
    urls = [binary_url]
    if platform_key == "win-x64":
        urls.append(_WINDOWS_CUDA_DLLS_URL.format(N=build_number))
    return all(_url_accessible(url) for url in urls)


def install_llamacpp(
    kodo_dir: Path,
    *,
    version: int | None = None,
    progress_cb: ProgressCb | None = None,
) -> LlamaInstall:
    """Download and install a llama.cpp release for the current platform.

    Fetches the latest build number from GitHub (unless *version* pins an
    explicit build), downloads and extracts the platform binary, verifies it
    runs, then writes ``llama-meta.json``.  If the same build is already
    installed the download is skipped.

    Progress is reported via *progress_cb* as ``(percent: int, message: str)``
    calls.  ``percent == 100`` signals success; ``percent == -1`` signals an
    error (the message contains the reason).

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        version (int | None): Explicit build number to install (e.g. ``12345``
            for ``b12345``). ``None`` (the default) installs the latest
            release.
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
        if version is not None:
            build_number = version
            _progress(0, f"Installing llama.cpp b{build_number}…")
        else:
            _progress(0, "Fetching latest release info from GitHub…")
            build_number = fetch_latest_build_number()

        existing = find_installed(kodo_dir)
        if existing is not None and existing.build == build_number:
            _progress(100, f"llama.cpp b{build_number} already installed")
            return existing

        platform_key = _current_platform_key()
        asset_name, binary_url = _asset_url(build_number, platform_key)
        _progress(5, f"Installing llama.cpp b{build_number} ({platform_key})")

        install_dir = kodo_dir / "llama.cpp" / f"b{build_number}"
        install_dir.mkdir(parents=True, exist_ok=True)
        archive_path = install_dir / asset_name

        _progress(10, f"Downloading {asset_name}…")
        _download(binary_url, archive_path, progress_cb, pct_start=10, pct_end=75)

        _progress(75, "Extracting binary archive…")
        if asset_name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(install_dir, filter="data")
        else:
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(install_dir)
        archive_path.unlink()

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

    Every ``llama-server`` process running off this install (kodo's chat
    server *and* the titler's — see ``server/_app.py``) must already be
    stopped: on Windows the files cannot be deleted while any of them has
    them mapped, and :func:`_rmtree_retrying`'s backoff only covers the brief
    lag between a process being signalled and the kernel releasing its
    handles, not a process that is still alive.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Raises:
        OSError: If the build directory could not be removed. ``llama-meta.json``
            is left in place in that case, so the install stays consistent
            (still recorded, still on disk) rather than becoming an
            unreferenced orphan.
    """
    installed = find_installed(kodo_dir)
    if installed is None:
        # Also covers "meta file present but its executable is gone" — clean
        # the stale record up so nothing keeps reporting a phantom install.
        meta = _meta_path(kodo_dir)
        if meta.exists():
            _log.info("Removing stale llama-meta.json with no runnable build behind it")
            meta.unlink()
        else:
            _log.info("llama.cpp is not installed — nothing to uninstall")
        return

    _log.info("Uninstalling llama.cpp b%d from %s", installed.build, installed.install_dir)
    _rmtree_retrying(installed.install_dir)
    _log.info("Removed %s", installed.install_dir)

    meta = _meta_path(kodo_dir)
    if meta.exists():
        meta.unlink()
        _log.info("Removed %s", meta)


def update_llamacpp(
    kodo_dir: Path,
    *,
    version: int | None = None,
    progress_cb: ProgressCb | None = None,
) -> LlamaInstall:
    """Install another llama.cpp build in place of the current one.

    **Installs first, deletes the old build afterwards.** Each build lives in
    its own ``b{N}`` directory, so the new one never needs the old one's
    space, and this ordering buys three things the old uninstall-then-install
    order could not:

    * A failed download/extraction/verification leaves the previous build
      installed and working, instead of leaving the user with nothing.
    * ``llama-meta.json`` only ever moves to the new build once that build is
      on disk and has passed ``--version``, so no window exists where the
      recorded install doesn't exist.
    * Deleting the old directory becomes pure cleanup — nothing references it
      any more — so a Windows sharing violation there costs disk space and a
      log line rather than failing the update.

    The cost is that both builds are on disk at once for the duration (~2 GB
    on the Windows CUDA build).

    **This never reinstalls a build in place.** Asking for the build that is
    already installed is a no-op — :func:`install_llamacpp` returns the
    existing install (reporting 100%) and there is no superseded directory to
    clean up, so nothing is deleted and nothing is re-downloaded. A genuine
    reinstall is the user's explicit :func:`uninstall_llamacpp`-then-
    :func:`install_llamacpp` pair, which in kodo-vsix means clicking
    "Uninstall llama.cpp" and then "Install llama.cpp"; ``server/_app.py``'s
    ``llamacpp.update`` handler short-circuits that case before it ever gets
    here, so a working install is never torn down to reproduce itself.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        version (int | None): Explicit build number to install. ``None``
            (the default) installs the latest release.
        progress_cb (ProgressCb | None): Optional progress callback forwarded
            to :func:`install_llamacpp`.

    Returns:
        LlamaInstall: Metadata for the installed build.

    Raises:
        RuntimeError: If the installation step fails.
    """
    existing = find_installed(kodo_dir)
    install = install_llamacpp(kodo_dir, version=version, progress_cb=progress_cb)

    if existing is not None and existing.install_dir != install.install_dir:
        try:
            _rmtree_retrying(existing.install_dir)
            _log.info("Removed superseded llama.cpp build at %s", existing.install_dir)
        except OSError as exc:
            _log.warning(
                "llama.cpp b%d is installed and active, but the superseded build at %s could "
                "not be removed (%s) — it is unreferenced and safe to delete by hand",
                install.build,
                existing.install_dir,
                exc,
            )

    return install
