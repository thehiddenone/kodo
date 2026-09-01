from __future__ import annotations

import json
import re
import time
import tomllib
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SESSION_TTL = 60  # seconds before a session is considered stale


class BuildInfo:
    current: int
    next: int
    base: tuple[int, int, int]
    incremented: bool
    timestamp: float

    def __init__(
        self,
        current: int,
        next: int,
        base: tuple[int, int, int],
        incremented: bool,
        timestamp: float,
    ) -> None:
        self.current = current
        self.next = next
        self.base = base
        self.incremented = incremented
        self.timestamp = timestamp

    def __str__(self) -> str:
        return f'BuildInfo({self.current}, {self.next}, {self.base}, {self.incremented}, {self.timestamp})'

    @property
    def version(self) -> str:
        return f"{self.base[0]}.{self.base[1]}.{self.base[2]}b{self.current}"

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.timestamp) > _SESSION_TTL


def _base_from_toml(root: Path) -> tuple[int, int, int]:
    toml_version: str = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    base_str = re.sub(r"b\d+$", "", toml_version)
    if not _BASE_RE.match(base_str):
        raise ValueError(
            f"pyproject.toml version must be major.minor.patch "
            f"(optionally with bN suffix), got: {toml_version!r}"
        )
    major, minor, patch = base_str.split(".")
    return int(major), int(minor), int(patch)


def _load(root: Path) -> BuildInfo:
    """Load BuildInfo from BUILD; return a stale default on missing or corrupt data."""
    build_file = root / "BUILD"
    if build_file.exists():
        try:
            data = json.loads(build_file.read_text(encoding="utf-8"))
            return BuildInfo(
                current=int(data["current"]),
                next=int(data["next"]),
                base=tuple(int(x) for x in data["base"]),  # type: ignore[arg-type]
                incremented=bool(data["incremented"]),
                timestamp=float(data["timestamp"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Corrupted or old plain-integer format — try to salvage a number
            content = build_file.read_text(encoding="utf-8")
            m = re.search(r"\d+", content)
            current = int(m.group()) if m else 0
            return BuildInfo(current, current + 1, (0, 0, 0), False, 0.0)

    return BuildInfo(0, 1, (0, 0, 0), False, 0.0)


def _save(root: Path, info: BuildInfo) -> None:
    d = {
        "current": info.current,
        "next": info.next,
        "base": list(info.base),
        "incremented": info.incremented,
        "timestamp": info.timestamp,
    }
    (root / "BUILD").write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        info = _load(root)

        if info.is_stale:
            print(f'is stale, {info.incremented}')
            # No active session (or expired) — start a new one.
            # If the previous session completed, advance to next; otherwise retry current.
            current = info.next if info.incremented else info.current
            info = BuildInfo(current, current + 1, _base_from_toml(root), False, time.time())
            _save(root, info)

            init = root / "src" / "kodo" / "__init__.py"
            init.write_text(
                re.sub(r'(__version__ = ")[^"]+(")', rf"\g<1>{info.version}\g<2>",
                       init.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
        # else: active session already in progress (e.g. wheel after sdist) — reuse as-is

        build_data["version"] = info.version
        print(f'initialize: {info}')

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:  # noqa: ARG002
        root = Path(self.root)
        info = _load(root)

        if info.is_stale:
            return  # session expired between initialize and finalize — nothing to do

        if not info.incremented:
            print('not incremented')
            # First finalize: update pyproject.toml and mark session done
            p = root / "pyproject.toml"
            p.write_text(
                re.sub(r'^(version = ")[^"]+(")', rf"\g<1>{info.version}\g<2>",
                       p.read_text(encoding="utf-8"), flags=re.MULTILINE),
                encoding="utf-8",
            )
            info.incremented = True
            _save(root, info)
        else:
            print('already incremented')
            # Subsequent finalize: already done — write next build number and close session
            info.current = info.next
            info.next = info.next + 1
            info.incremented = False
            info.timestamp = 0.0  # mark stale so next build starts fresh
            _save(root, info)
        print(f'finalize: {info}')
