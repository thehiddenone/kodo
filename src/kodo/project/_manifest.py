"""``kodo.md`` manifest parser and validator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Manifest", "ManifestError", "parse_manifest"]

_REQUIRED_HEADINGS = ("# Kodo Project", "## Toolchain", "## Components", "## Settings overrides")
_TOOLCHAIN_RE = re.compile(r"^\s*-\s+(\w+)\s*$", re.MULTILINE)


class ManifestError(Exception):
    """Raised when ``kodo.md`` is absent or structurally invalid."""


@dataclass(frozen=True)
class Manifest:
    """Parsed representation of a ``kodo.md`` file.

    Attributes:
        toolchain: The active toolchain plugin name (e.g. ``'python'``).
        components: Component names listed under ``## Components``.
    """

    toolchain: str
    components: list[str] = field(default_factory=list)


def parse_manifest(path: Path) -> Manifest:
    """Parse and validate a ``kodo.md`` file.

    Args:
        path (Path): Absolute path to the ``kodo.md`` file.

    Returns:
        Manifest: Parsed manifest.

    Raises:
        ManifestError: File is absent or missing required headings / toolchain.
    """
    if not path.exists():
        raise ManifestError(f"kodo.md not found at {path}")

    text = path.read_text(encoding="utf-8")
    _validate_headings(text, path)
    toolchain = _extract_toolchain(text, path)
    components = _extract_components(text)
    return Manifest(toolchain=toolchain, components=components)


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _validate_headings(text: str, path: Path) -> None:
    for heading in _REQUIRED_HEADINGS:
        if heading not in text:
            raise ManifestError(
                f"{path}: missing required heading '{heading}'. "
                "Re-run 'Kōdo: Init Project' or add the heading manually."
            )


def _extract_toolchain(text: str, path: Path) -> str:
    # Grab text between ## Toolchain and the next ## heading
    section = _section_text(text, "## Toolchain")
    m = _TOOLCHAIN_RE.search(section)
    if not m:
        raise ManifestError(
            f"{path}: '## Toolchain' section must contain a toolchain name "
            "(e.g. '- python' or '- node')."
        )
    return m.group(1).lower()


def _extract_components(text: str) -> list[str]:
    section = _section_text(text, "## Components")
    # Each component is a list item: "- component_name"
    return [
        m.group(1)
        for m in re.finditer(r"^\s*-\s+(\w[\w-]*)\s*$", section, re.MULTILINE)
    ]


def _section_text(text: str, heading: str) -> str:
    """Return the body between ``heading`` and the next ``##`` heading."""
    start = text.find(heading)
    if start == -1:
        return ""
    start += len(heading)
    # Find the next ## heading
    next_heading = text.find("\n##", start)
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]
