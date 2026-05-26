"""Component registry: codename-to-display-name mapping parsed from architecture content.

The architecture artifact declares each component as a table row:

    | Codename | Display name         |
    | -------- | -------------------- |
    | AUTH     | User Authentication  |
    | TRADE    | Trade Execution      |

:class:`ComponentRegistry` parses that table and provides the
``component_dir`` used by :func:`~._materialization.materialization_path`
to place per-component artifacts under ``src/design/<component_dir>/``,
``gen/src/<component_dir>/``, etc.
"""

from __future__ import annotations

import re

__all__ = ["ComponentRegistry"]

# Match a markdown table row with exactly two non-empty pipe-delimited cells.
_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
# Separator row like | --- | --- |
_SEP_RE = re.compile(r"^\|\s*[-:]+\s*\|\s*[-:]+\s*\|")
# Valid codename per workspace rules
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")


def _to_snake(display_name: str) -> str:
    """Normalise a display name to snake_case directory name."""
    lowered = display_name.strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")


class ComponentRegistry:
    """Maps responsibility_codes to display names and component directories.

    Built from the text content of the accepted architecture artifact.
    For projects that have not yet produced an architecture artifact the
    registry is empty; callers fall back to using the raw responsibility_code.

    Args:
        architecture_content (str): Full text content of the architecture
            artifact. May be empty or None for a fresh project.
    """

    __entries: dict[str, str]  # codename -> display_name

    def __init__(self, architecture_content: str | None = None) -> None:
        """Parse the architecture content and build the internal mapping.

        Args:
            architecture_content (str | None): Text of the architecture
                artifact, or ``None`` / empty string for an empty registry.
        """
        self.__entries = {}
        if architecture_content:
            self.__parse(architecture_content)

    @classmethod
    def empty(cls) -> ComponentRegistry:
        """Return an empty registry (no components declared yet).

        Returns:
            ComponentRegistry: Registry with no entries.
        """
        return cls(None)

    def component_dir(self, responsibility_code: str) -> str:
        """Return the directory name for the given component.

        When the codename has a registered display name the return value is
        the snake_case normalisation of that display name
        (e.g. ``"User Authentication"`` → ``"user_authentication"``).
        When the codename is not registered (architecture not yet accepted,
        or project-wide artifact) the codename itself is returned as-is.

        Args:
            responsibility_code (str): Component codename (e.g. ``"AUTH"``).

        Returns:
            str: Directory name safe for use in a filesystem path.
        """
        display = self.__entries.get(responsibility_code)
        if display is None:
            return responsibility_code
        return _to_snake(display)

    def display_name(self, responsibility_code: str) -> str | None:
        """Return the display name for a codename, or None if not registered.

        Args:
            responsibility_code (str): Component codename.

        Returns:
            str | None: Human-readable display name, or ``None``.
        """
        return self.__entries.get(responsibility_code)

    def all_codenames(self) -> list[str]:
        """Return all registered codenames in parse order.

        Returns:
            list[str]: Codenames declared in the architecture artifact.
        """
        return list(self.__entries)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def __parse(self, content: str) -> None:
        in_table = False
        header_seen = False
        for line in content.splitlines():
            if _SEP_RE.match(line):
                in_table = True
                header_seen = True
                continue
            m = _ROW_RE.match(line)
            if not m:
                if in_table:
                    # A non-table line after the table ends this section.
                    in_table = False
                    header_seen = False
                continue
            if not header_seen:
                # First row before separator = header; skip it.
                continue
            code = m.group(1).strip()
            display = m.group(2).strip()
            if _CODE_RE.match(code) and display:
                self.__entries[code] = display
