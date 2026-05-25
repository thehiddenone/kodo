"""Project filesystem path conventions (kodo.md, src/, gen/, .kodo/)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ProjectLayoutError(Exception):
    """Raised when the project directory does not conform to the expected layout."""


@dataclass(frozen=True)
class ProjectLayout:
    """Canonical path conventions for a single Kodo project.

    All paths are derived from ``root``; this dataclass is a thin helper
    that avoids string concatenation scattered throughout the codebase.

    Attributes:
        root: Absolute path to the Kodo project root.
    """

    root: Path

    # ------------------------------------------------------------------
    # Computed paths
    # ------------------------------------------------------------------

    @property
    def kodo_md(self) -> Path:
        """``<root>/kodo.md`` — project manifest."""
        return self.root / "kodo.md"

    @property
    def src_dir(self) -> Path:
        """``<root>/src/`` — specification source files."""
        return self.root / "src"

    @property
    def gen_dir(self) -> Path:
        """``<root>/gen/`` — generated artifacts."""
        return self.root / "gen"

    @property
    def kodo_dir(self) -> Path:
        """``<root>/.kodo/`` — server state, settings, mirror."""
        return self.root / ".kodo"

    @property
    def settings_json(self) -> Path:
        """``<root>/.kodo/settings.json`` — project-scoped settings."""
        return self.kodo_dir / "settings.json"

    @property
    def security_json(self) -> Path:
        """``<root>/.kodo/security.json`` — project-scoped security rules."""
        return self.kodo_dir / "security.json"

    @property
    def server_pid(self) -> Path:
        """``<root>/.kodo/server.pid`` — running server PID."""
        return self.kodo_dir / "server.pid"

    @property
    def logs_dir(self) -> Path:
        """``<root>/.kodo/logs/`` — server log directory."""
        return self.kodo_dir / "logs"

    @property
    def server_log(self) -> Path:
        """``<root>/.kodo/logs/server.log``."""
        return self.logs_dir / "server.log"

    @property
    def checkpoints_dir(self) -> Path:
        """``<root>/.kodo/checkpoints/`` — git mirror repository."""
        return self.kodo_dir / "checkpoints"

    @property
    def sessions_dir(self) -> Path:
        """``<root>/.kodo/sessions/`` — per-session metadata files."""
        return self.kodo_dir / "sessions"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Assert that this directory looks like a Kodo project.

        Checks that ``kodo.md`` is present and contains the required
        ``# Kodo Project`` heading.  Does *not* require ``src/``, ``gen/``,
        or ``.kodo/`` to exist (they are created by :meth:`init`).

        Raises:
            ProjectLayoutError: ``kodo.md`` is absent or missing the marker
                heading.
        """
        if not self.kodo_md.exists():
            raise ProjectLayoutError(
                f"Not a Kodo project — {self.kodo_md} not found. "
                "Run 'Kōdo: Init Project' to initialise."
            )
        text = self.kodo_md.read_text(encoding="utf-8")
        if "# Kodo Project" not in text:
            raise ProjectLayoutError(
                f"{self.kodo_md} exists but is missing the '# Kodo Project' heading."
            )

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init(self, *, force: bool = False) -> None:
        """Create the standard Kodo project layout under ``root``.

        Creates ``kodo.md``, ``src/``, ``gen/``, and ``.kodo/`` if absent.
        Refuses to overwrite an existing ``kodo.md`` unless ``force=True``.

        Args:
            force (bool): Overwrite an existing project layout.

        Raises:
            ProjectLayoutError: ``kodo.md`` already exists and ``force`` is
                ``False``.
        """
        if self.kodo_md.exists() and not force:
            raise ProjectLayoutError(
                f"{self.kodo_md} already exists. Pass force=True to overwrite."
            )

        self.root.mkdir(parents=True, exist_ok=True)
        self.src_dir.mkdir(exist_ok=True)
        self.gen_dir.mkdir(exist_ok=True)
        self.kodo_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.sessions_dir.mkdir(exist_ok=True)

        if not self.kodo_md.exists() or force:
            self.kodo_md.write_text(_KODO_MD_TEMPLATE, encoding="utf-8")


_KODO_MD_TEMPLATE = """\
# Kodo Project

> Project marker. Required.

## Toolchain

- python

## Components

(empty until Architect runs; agents append entries)

## Settings overrides

(optional inline overrides; structured-but-prose)
"""
