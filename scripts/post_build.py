"""Post-increment build_number after a successful hatch build."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read_build_num() -> int:
    build_file = ROOT / "build_number"
    if not build_file.exists():
        return 0
    return int(build_file.read_text(encoding="utf-8").strip())


def main() -> None:
    num = _read_build_num()
    (ROOT / "build_number").write_text(f"{num + 1}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
