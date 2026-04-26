"""Stamp pyproject.toml + __init__.py with the current build number, clean dist/."""

from __future__ import annotations

import re
import shutil
import sys
import tomllib
from pathlib import Path

_BASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
ROOT = Path(__file__).parent.parent


def _read_build_num() -> int:
    build_file = ROOT / "build_number"
    if not build_file.exists():
        return 0
    return int(build_file.read_text(encoding="utf-8").strip())


def _base_from_toml() -> tuple[int, int, int]:
    toml_version: str = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    base_str = re.sub(r"b\d+$", "", toml_version)
    if not _BASE_RE.match(base_str):
        print(f"ERROR: pyproject.toml version must be major.minor.patch "
              f"(optionally with bN suffix), got: {toml_version!r}", file=sys.stderr)
        sys.exit(1)
    major, minor, patch = base_str.split(".")
    return int(major), int(minor), int(patch)


def main() -> None:
    num = _read_build_num()
    base = _base_from_toml()
    version = f"{base[0]}.{base[1]}.{base[2]}b{num}"

    # Clean old artifacts
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir()

    # Stamp pyproject.toml
    p = ROOT / "pyproject.toml"
    p.write_text(
        re.sub(r'^(version = ")[^"]+(")', rf"\g<1>{version}\g<2>",
               p.read_text(encoding="utf-8"), flags=re.MULTILINE),
        encoding="utf-8",
    )

    # Stamp __init__.py
    init = ROOT / "src" / "kodo" / "__init__.py"
    init.write_text(
        re.sub(r'(__version__ = ")[^"]+(")', rf"\g<1>{version}\g<2>",
               init.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    print(f"build: {version}")


if __name__ == "__main__":
    main()
