"""Filesystem path conventions for Kodo.

Two tiers (see the ``project-kodo`` memory, WorkspaceLayout two-root model):

* :class:`WorkspaceLayout` — the *VS Code workspace* tier.  Rooted at the
  **physical root** (the parent directory of the first workspace folder) and
  owning the workspace-level ``.kodo-workspace/`` directory (sessions, logs,
  settings, the orchestrator session marker).  It also holds the **logical
  root**: a virtual directory whose immediate children are the *names* of the
  folders opened in the VS Code workspace, each mapping to that folder's real
  physical path (which may live anywhere on disk).

* :class:`ProjectLayout` — the *single project* tier.  Rooted at a project
  folder containing ``kodo.md``, owning ``src/``, ``gen/`` and the per-project
  ``.kodo/`` directory (the Guided artifact workspace + git mirror checkpoints).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def kodo_user_dir() -> Path:
    """``~/.kodo/`` — user-level Kodo configuration and cache directory."""
    return Path.home() / ".kodo"


class ProjectLayoutError(Exception):
    """Raised when the project directory does not conform to the expected layout."""


class WorkspaceLayout:
    """Workspace-tier path conventions and logical-root folder map.

    The **physical root** anchors ``.kodo-workspace/`` (sessions, logs,
    settings).  The **logical root** is the set of VS Code workspace folders,
    keyed by their (VS-Code-disambiguated) display names.  A logical path always
    begins with one of those names, which anchors the remainder to that folder's
    real physical path — see :func:`kodo.tools.resolve_logical`.

    Args:
        physical_root: Parent directory of the first workspace folder.
        folders: Logical name → physical path of every open workspace folder.
    """

    __physical_root: Path
    __folders: dict[str, Path]

    def __init__(self, physical_root: Path, folders: dict[str, Path] | None = None) -> None:
        self.__physical_root = physical_root.resolve()
        self.__folders = {name: Path(p).resolve() for name, p in (folders or {}).items()}

    # ------------------------------------------------------------------
    # Roots
    # ------------------------------------------------------------------

    @property
    def physical_root(self) -> Path:
        """Parent directory of the first workspace folder; the server root."""
        return self.__physical_root

    @property
    def folders(self) -> dict[str, Path]:
        """Copy of the logical-root map (name → physical path)."""
        return dict(self.__folders)

    def set_folders(self, folders: dict[str, Path]) -> None:
        """Replace the logical-root folder map (pushed over the WS protocol)."""
        self.__folders = {name: Path(p).resolve() for name, p in folders.items()}

    # ------------------------------------------------------------------
    # Workspace-level state directory (``.kodo-workspace/``)
    # ------------------------------------------------------------------

    @property
    def kodo_dir(self) -> Path:
        """``<physical_root>/.kodo-workspace/`` — workspace-level state."""
        return self.__physical_root / ".kodo-workspace"

    @property
    def marker_dir(self) -> Path:
        """Directory holding the orchestrator session marker (workspace-level)."""
        return self.kodo_dir

    @property
    def sessions_dir(self) -> Path:
        """``.kodo-workspace/sessions/`` — per-session stores (mode-agnostic)."""
        return self.kodo_dir / "sessions"

    @property
    def logs_dir(self) -> Path:
        """``.kodo-workspace/logs/`` — server log directory."""
        return self.kodo_dir / "logs"

    @property
    def server_log(self) -> Path:
        """``.kodo-workspace/logs/server.log``."""
        return self.logs_dir / "server.log"

    @property
    def llm_requests_dir(self) -> Path:
        """``.kodo-workspace/logs/llm_requests/`` — per-call LLM logs."""
        return self.logs_dir / "llm_requests"

    @property
    def settings_json(self) -> Path:
        """``.kodo-workspace/settings.json`` — workspace-scoped settings."""
        return self.kodo_dir / "settings.json"

    @property
    def server_pid(self) -> Path:
        """``.kodo-workspace/server.pid`` — running server PID."""
        return self.kodo_dir / "server.pid"

    def init(self) -> None:
        """Create the workspace-level ``.kodo-workspace/`` skeleton."""
        self.kodo_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)


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
        """``<root>/.kodo/`` — per-project Guided state (workspace, mirror)."""
        return self.root / ".kodo"

    @property
    def security_json(self) -> Path:
        """``<root>/.kodo/security.json`` — project-scoped security rules."""
        return self.kodo_dir / "security.json"

    @property
    def checkpoints_dir(self) -> Path:
        """``<root>/.kodo/checkpoints/`` — git mirror repository."""
        return self.kodo_dir / "checkpoints"

    @property
    def workspace_dir(self) -> Path:
        """``<root>/.kodo/workspace/`` — virtual artifact workspace."""
        return self.kodo_dir / "workspace"

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
