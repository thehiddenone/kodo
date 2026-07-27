"""Post-build steps: bump build_number, and sync kodo-vsix's pinned version."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read_build_num() -> int:
    build_file = ROOT / "build_number"
    if not build_file.exists():
        return 0
    return int(build_file.read_text(encoding="utf-8").strip())


def _bump_build_num() -> None:
    num = _read_build_num()
    (ROOT / "build_number").write_text(f"{num + 1}\n", encoding="utf-8")


def _sync_kodo_vsix_version() -> None:
    """Pin kodo-vsix's own version to the py-kodo version just built.

    kodo-vsix installs py-kodo from PyPI pinned to its own extension version
    (see uv-setup.ts's installKodo/getExtensionVersion), so the two must
    always match. No-op if the kodo-vsix checkout isn't present alongside
    this repo (e.g. a CI job building kodo standalone).
    """
    package_json_path = ROOT.parent / "kodo-vsix" / "package.json"
    if not package_json_path.exists():
        return

    version: str = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]

    data = json.loads(package_json_path.read_text(encoding="utf-8"))
    data["version"] = version
    package_json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"kodo-vsix: pinned package.json version to {version}")


def main() -> None:
    _bump_build_num()
    _sync_kodo_vsix_version()


if __name__ == "__main__":
    main()
