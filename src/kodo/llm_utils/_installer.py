"""llama.cpp binary installer.

Downloads the latest llama.cpp release from GitHub and installs it into
``~/.kodo/llama.cpp/b{N}/`` for the running platform.  Supports Windows
(x64), macOS (arm64 and x64), and Linux (all glibc-based distros via the
Ubuntu build).
"""

from __future__ import annotations

import json
import platform
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import cast

__all__ = ["find_installed", "install", "server_executable"]

_GITHUB_RELEASES_LATEST = "https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
_USER_AGENT = "kodo-llm-utils/0.1 (github.com/kodo-ai/kodo)"

# Priority-ordered asset-name substrings for each platform key.
# The first substring that appears anywhere in an asset's filename wins.
_ASSET_CANDIDATES: dict[str, list[str]] = {
    "win-x64": ["win-cuda-cu12", "win-avx2-x64", "win-avx-x64", "win-x64"],
    "macos-arm64": ["macos-arm64"],
    "macos-x64": ["macos-x64"],
    "linux-x64": ["ubuntu-x64", "linux-x64"],
}


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


def _fetch_latest_release() -> dict[str, object]:
    req = urllib.request.Request(
        _GITHUB_RELEASES_LATEST,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: object = json.loads(resp.read())
        return cast(dict[str, object], data)


def _parse_build_number(tag_name: str) -> int:
    match = re.match(r"^b(\d+)$", tag_name)
    if not match:
        raise ValueError(f"Cannot parse build number from tag {tag_name!r}")
    return int(match.group(1))


def _select_asset(assets: list[object], platform_key: str) -> tuple[str, str]:
    candidates = _ASSET_CANDIDATES[platform_key]
    zip_assets: dict[str, str] = {}
    for item in assets:
        asset = cast(dict[str, object], item)
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if name.endswith(".zip") and url:
            zip_assets[name] = url

    for candidate in candidates:
        for name, url in zip_assets.items():
            if candidate in name:
                return name, url

    raise RuntimeError(
        f"No suitable llama.cpp release asset for platform {platform_key!r}. "
        f"Available .zip assets: {sorted(zip_assets)}"
    )


def _download(url: str, dest: Path) -> None:
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
                if total:
                    pct = downloaded * 100 // total
                    mb_done = downloaded // 1_048_576
                    mb_total = total // 1_048_576
                    print(f"\r  {pct:3d}%  {mb_done} / {mb_total} MB", end="", flush=True)
    if total:
        print()


def find_installed(kodo_dir: Path) -> Path | None:
    """Return the most recently installed llama.cpp build directory.

    Scans ``kodo_dir/llama.cpp/`` for directories whose names match the
    ``b{N}`` pattern and returns the one with the highest build number.

    Args:
        kodo_dir (Path): The ``~/.kodo`` base directory.

    Returns:
        Path | None: Build directory (e.g. ``~/.kodo/llama.cpp/b4000``),
        or ``None`` if nothing is installed yet.
    """
    base = kodo_dir / "llama.cpp"
    if not base.is_dir():
        return None
    builds = sorted(
        (d for d in base.iterdir() if d.is_dir() and re.match(r"^b\d+$", d.name)),
        key=lambda d: int(d.name[1:]),
        reverse=True,
    )
    return builds[0] if builds else None


def server_executable(install_dir: Path) -> Path | None:
    """Find the ``llama-server`` executable inside an install directory.

    Uses :meth:`Path.rglob` so the executable is found regardless of whether
    the archive placed it at the top level or inside a subdirectory.

    Args:
        install_dir (Path): A llama.cpp build directory.

    Returns:
        Path | None: Absolute path to the executable, or ``None`` if the
        binary is not present in the directory tree.
    """
    exe_name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    for candidate in install_dir.rglob(exe_name):
        return candidate
    return None


def install(kodo_dir: Path, *, force: bool = False) -> Path:
    """Download and install the latest llama.cpp release for the current platform.

    Fetches release metadata from the GitHub API, selects the most suitable
    pre-built binary archive, downloads it, and extracts it into
    ``kodo_dir/llama.cpp/b{N}/``.

    If the same build is already installed and *force* is ``False``, the
    network download is skipped and the existing directory is returned.

    Args:
        kodo_dir (Path): The ``~/.kodo`` base directory.
        force (bool): Re-download and re-extract even if the build already exists.

    Returns:
        Path: The build directory (e.g. ``~/.kodo/llama.cpp/b4000``).
    """
    print("Fetching llama.cpp release info…")
    release = _fetch_latest_release()
    tag = str(release["tag_name"])
    build_num = _parse_build_number(tag)
    install_dir = kodo_dir / "llama.cpp" / tag

    if install_dir.exists() and not force:
        print(f"  Already installed: {install_dir}")
        return install_dir

    platform_key = _current_platform_key()
    assets = cast(list[object], release["assets"])
    asset_name, download_url = _select_asset(assets, platform_key)

    print(f"  Build:    b{build_num}")
    print(f"  Platform: {platform_key}")
    print(f"  Asset:    {asset_name}")

    install_dir.mkdir(parents=True, exist_ok=True)
    zip_path = install_dir / asset_name

    print("Downloading…")
    _download(download_url, zip_path)

    print("Extracting…")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(install_dir)
    zip_path.unlink()

    print(f"  Done: {install_dir}")
    return install_dir
