"""Portable third-party util manager.

Kōdo bundles three external CLI utils — **uv**, **ripgrep**, and **fd** — under
``~/.kodo/bin/``.  Each util lives in its own directory with the binary placed
directly inside it, alongside a sibling JSON manifest recording the pinned
version, the absolute binary path, and the URL it was downloaded from::

    ~/.kodo/bin/
        uv.json        uv/uv          (uv\\uv.exe on Windows)
        ripgrep.json   ripgrep/rg
        fd.json        fd/fd

These are called **utils** (not "tools") to avoid colliding with the agent-facing
tool catalog (``kodo.toolspecs.ToolSpec`` and friends), which is an unrelated
concept.

The manifest schema (``{name, version, path, download_url}``) is shared with the
VS Code extension (``kodo-vsix`` ``src/uv-setup.ts``), which installs **uv** the
same way before the Python backend exists.  Both sides read the manifest and
only download when the pinned version is missing, so whichever runs first wins
and the other is a no-op.

The extension installs only uv (it needs uv to build the venv before any Python
runs); this module installs **all three**, so a future console-only build of
Kōdo works without the extension.  Versions are pinned — bumping one is a code
change here (and in the extension, for uv).
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

__all__ = [
    "UtilInstall",
    "UtilSpec",
    "UTIL_SPECS",
    "ensure_all_utils",
    "ensure_util",
    "find_util",
]

_log = logging.getLogger(__name__)

_USER_AGENT = "kodo-util-manager/0.1 (github.com/thehiddenone/kodo)"

_IS_WINDOWS = platform.system() == "Windows"


# ---------------------------------------------------------------------------
# Util specs (pinned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UtilSpec:
    """A pinned third-party util installable into ``~/.kodo/bin/<name>/``.

    Attributes:
        name: Util / manifest name (``"uv"``, ``"ripgrep"``, ``"fd"``).
        version: Pinned release version.
        binary: Unix binary name (``.exe`` is appended on Windows).
        targets: ``"<os>-<arch>"`` platform key → release-target token embedded
            in the asset filename.  ``os`` ∈ ``darwin``/``linux``/``windows``;
            ``arch`` ∈ ``x86_64``/``aarch64``.
        archive_template: Asset filename template using ``{version}``,
            ``{target}``, ``{ext}``.
        url_template: Release download URL template using ``{version}``,
            ``{archive}`` (``{version}`` is the git tag, so embed any ``v``
            prefix here).
    """

    name: str
    version: str
    binary: str
    targets: dict[str, str]
    archive_template: str
    url_template: str


# x64-linux ripgrep ships only a musl build; arm64-linux ships gnu.  All three
# utils now publish a native Windows-arm64 (aarch64-pc-windows-msvc) build.
UTIL_SPECS: dict[str, UtilSpec] = {
    "uv": UtilSpec(
        name="uv",
        version="0.11.24",
        binary="uv",
        targets={
            "darwin-x86_64": "x86_64-apple-darwin",
            "darwin-aarch64": "aarch64-apple-darwin",
            "linux-x86_64": "x86_64-unknown-linux-gnu",
            "linux-aarch64": "aarch64-unknown-linux-gnu",
            "windows-x86_64": "x86_64-pc-windows-msvc",
            "windows-aarch64": "aarch64-pc-windows-msvc",
        },
        archive_template="uv-{target}.{ext}",
        url_template="https://github.com/astral-sh/uv/releases/download/{version}/{archive}",
    ),
    "ripgrep": UtilSpec(
        name="ripgrep",
        version="15.1.0",
        binary="rg",
        targets={
            "darwin-x86_64": "x86_64-apple-darwin",
            "darwin-aarch64": "aarch64-apple-darwin",
            "linux-x86_64": "x86_64-unknown-linux-musl",
            "linux-aarch64": "aarch64-unknown-linux-gnu",
            "windows-x86_64": "x86_64-pc-windows-msvc",
            "windows-aarch64": "aarch64-pc-windows-msvc",
        },
        archive_template="ripgrep-{version}-{target}.{ext}",
        url_template="https://github.com/BurntSushi/ripgrep/releases/download/{version}/{archive}",
    ),
    "fd": UtilSpec(
        name="fd",
        version="10.4.2",
        binary="fd",
        targets={
            "darwin-x86_64": "x86_64-apple-darwin",
            "darwin-aarch64": "aarch64-apple-darwin",
            "linux-x86_64": "x86_64-unknown-linux-gnu",
            "linux-aarch64": "aarch64-unknown-linux-gnu",
            "windows-x86_64": "x86_64-pc-windows-msvc",
            "windows-aarch64": "aarch64-pc-windows-msvc",
        },
        archive_template="fd-v{version}-{target}.{ext}",
        url_template="https://github.com/sharkdp/fd/releases/download/v{version}/{archive}",
    ),
}


@dataclass(frozen=True)
class UtilInstall:
    """Metadata for an installed util (mirrors the on-disk manifest).

    Attributes:
        name: Util name.
        version: Installed version.
        path: Absolute path to the binary.
    """

    name: str
    version: str
    path: Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _bin_root(kodo_dir: Path) -> Path:
    return kodo_dir / "bin"


def _util_dir(kodo_dir: Path, name: str) -> Path:
    return _bin_root(kodo_dir) / name


def _manifest_path(kodo_dir: Path, name: str) -> Path:
    return _bin_root(kodo_dir) / f"{name}.json"


def _binary_name(spec: UtilSpec) -> str:
    return f"{spec.binary}.exe" if _IS_WINDOWS else spec.binary


def _binary_path(kodo_dir: Path, spec: UtilSpec) -> Path:
    return _util_dir(kodo_dir, spec.name) / _binary_name(spec)


# ---------------------------------------------------------------------------
# Platform → release target
# ---------------------------------------------------------------------------


def _platform_key() -> str:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    system = platform.system()
    if system == "Windows":
        os_key = "windows"
    elif system == "Darwin":
        os_key = "darwin"
    elif system == "Linux":
        os_key = "linux"
    else:
        raise RuntimeError(f"Unsupported platform: {system!r}")
    return f"{os_key}-{arch}"


def _resolve_asset(spec: UtilSpec) -> tuple[str, str]:
    """Return ``(archive_name, download_url)`` for the current platform."""
    key = _platform_key()
    target = spec.targets.get(key)
    if target is None:
        raise RuntimeError(f"{spec.name}: no release target for platform {key}")
    ext = "zip" if _IS_WINDOWS else "tar.gz"
    archive = spec.archive_template.format(version=spec.version, target=target, ext=ext)
    url = spec.url_template.format(version=spec.version, archive=archive)
    return archive, url


# ---------------------------------------------------------------------------
# Manifest I/O (schema shared with kodo-vsix src/uv-setup.ts)
# ---------------------------------------------------------------------------


def _read_manifest(kodo_dir: Path, name: str) -> dict[str, object] | None:
    p = _manifest_path(kodo_dir, name)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return cast(dict[str, object], data)
    except Exception:
        pass
    return None


def _write_manifest(kodo_dir: Path, name: str, version: str, path: Path, url: str) -> None:
    data: dict[str, object] = {
        "name": name,
        "version": version,
        "path": str(path),
        "download_url": url,
    }
    p = _manifest_path(kodo_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------


def _download(url: str, dest: Path) -> None:
    _log.info("Downloading %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_binary(archive: Path, ext: str, binary_name: str, dest_bin: Path) -> None:
    """Extract *archive* and copy the binary named *binary_name* to *dest_bin*.

    The binary is nested in a versioned subdirectory inside the archive, so the
    whole tree is searched for it.
    """
    with tempfile.TemporaryDirectory(dir=str(dest_bin.parent)) as tmp:
        tmp_dir = Path(tmp)
        if ext == "zip":
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(tmp_dir)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(tmp_dir)

        src = next((p for p in tmp_dir.rglob(binary_name) if p.is_file()), None)
        if src is None:
            raise RuntimeError(f"{binary_name} not found in downloaded archive {archive.name}")

        dest_bin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_bin)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_util(kodo_dir: Path, name: str) -> UtilInstall | None:
    """Return the installed util recorded in ``~/.kodo/bin/<name>.json``.

    Reads the manifest only — no filesystem scanning.  Returns ``None`` if the
    manifest is absent/unreadable or the recorded binary no longer exists.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        name (str): Util name (``"uv"``, ``"ripgrep"``, ``"fd"``).

    Returns:
        UtilInstall | None: Install metadata, or ``None`` if not installed.
    """
    meta = _read_manifest(kodo_dir, name)
    if meta is None:
        return None
    try:
        version = str(meta["version"])
        path = Path(str(meta["path"]))
    except KeyError:
        return None
    if not path.exists():
        return None
    return UtilInstall(name=name, version=version, path=path)


def ensure_util(kodo_dir: Path, name: str) -> UtilInstall:
    """Ensure *name* is installed at the pinned version and return its metadata.

    No-op when the manifest already records the pinned version and the binary is
    present on disk.  Otherwise downloads the pinned release for the current
    platform, extracts the binary into ``~/.kodo/bin/<name>/``, and writes the
    manifest.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        name (str): Util name; must be a key of :data:`UTIL_SPECS`.

    Returns:
        UtilInstall: Metadata for the installed util.

    Raises:
        KeyError: If *name* is not a known util.
        RuntimeError: If the platform is unsupported, or download/extraction
            fails.
    """
    spec = UTIL_SPECS[name]
    bin_path = _binary_path(kodo_dir, spec)

    meta = _read_manifest(kodo_dir, name)
    if meta is not None and str(meta.get("version")) == spec.version and bin_path.exists():
        _log.debug("%s %s already present", name, spec.version)
        return UtilInstall(name=name, version=spec.version, path=bin_path)

    archive_name, url = _resolve_asset(spec)
    ext = "zip" if _IS_WINDOWS else "tar.gz"
    util_dir = _util_dir(kodo_dir, name)
    util_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _bin_root(kodo_dir) / archive_name

    _log.info("Installing %s %s (%s)", name, spec.version, archive_name)
    try:
        _download(url, archive_path)
        _extract_binary(archive_path, ext, _binary_name(spec), bin_path)
    finally:
        archive_path.unlink(missing_ok=True)

    if not _IS_WINDOWS:
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _write_manifest(kodo_dir, name, spec.version, bin_path, url)
    _log.info("%s %s installed at %s", name, spec.version, bin_path)
    return UtilInstall(name=name, version=spec.version, path=bin_path)


def ensure_all_utils(kodo_dir: Path) -> dict[str, UtilInstall]:
    """Ensure every util in :data:`UTIL_SPECS` is installed (best-effort).

    Each util is installed independently; a failure on one is logged and does
    not prevent the others.  Intended to be called once at server startup.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        dict[str, UtilInstall]: Successfully installed utils keyed by name.
    """
    installed: dict[str, UtilInstall] = {}
    for name in UTIL_SPECS:
        try:
            installed[name] = ensure_util(kodo_dir, name)
        except Exception as exc:
            _log.warning("Failed to ensure util %r: %s", name, exc)
    return installed
