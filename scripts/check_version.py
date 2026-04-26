"""Sync __version__ in src/kodo/__init__.py from pyproject.toml."""

import re
import tomllib
from pathlib import Path

root = Path(__file__).parent.parent

version: str = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

init_path = root / "src" / "kodo" / "__init__.py"
init_path.write_text(
    re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init_path.read_text(encoding="utf-8")),
    encoding="utf-8",
)
