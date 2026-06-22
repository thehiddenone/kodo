"""Filesystem path conventions for Kodo.

Three concepts (see the ``project-kodo`` memory):

* :class:`WorkspaceLayout` — the **global home** tier.  Rooted at ``~/.kodo``
  and shared by the singleton server across every VS Code window.  Owns the
  global ``sessions/``, ``logs/``, ``settings.json`` and the ``kodo-server``
  discovery file.  It is window-agnostic — there is exactly one per machine.

* :class:`SessionWorkspace` — the **per-session window view**.  Holds the
  physical root (the parent directory of the window's first folder) plus the
  **logical root**: a mutable map of VS Code workspace-folder *names* to their
  real physical paths.  Pushed over the WS protocol per session and consumed by
  :func:`kodo.tools.resolve_logical` for Problem Solver path resolution.

* :class:`ProjectLayout` — the *single project* tier.  Rooted at a project
  folder containing ``kodo.md``, owning ``src/``, ``gen/`` and the per-project
  ``.kodo/`` directory (the Guided artifact workspace + git mirror checkpoints).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def kodo_user_dir() -> Path:
    """``~/.kodo/`` — the global Kodo home (config, cache, sessions, logs)."""
    return Path.home() / ".kodo"


class ProjectLayoutError(Exception):
    """Raised when the project directory does not conform to the expected layout."""


class WorkspaceLayout:
    """Global home-tier path conventions for the singleton server.

    Rooted at ``~/.kodo`` (``kodo_user_dir()``).  There is one instance per
    machine, shared by every VS Code window's session.  It owns the global
    ``sessions/``/``logs/``/``settings.json`` plus the ``kodo-server`` discovery
    file.  Per-window state (physical root, the logical folder map, the bound
    project) lives in :class:`SessionWorkspace`, not here.

    Args:
        root: Home directory; defaults to ``~/.kodo``.
    """

    __root: Path

    def __init__(self, root: Path | None = None) -> None:
        self.__root = (root or kodo_user_dir()).resolve()

    @property
    def kodo_dir(self) -> Path:
        """``~/.kodo/`` — the global home directory."""
        return self.__root

    @property
    def sessions_dir(self) -> Path:
        """``~/.kodo/sessions/`` — per-session stores (mode-agnostic)."""
        return self.__root / "sessions"

    @property
    def logs_dir(self) -> Path:
        """``~/.kodo/logs/`` — server log directory."""
        return self.__root / "logs"

    @property
    def server_log(self) -> Path:
        """``~/.kodo/logs/server.log``."""
        return self.logs_dir / "server.log"

    @property
    def llm_requests_dir(self) -> Path:
        """``~/.kodo/logs/llm_requests/`` — per-call LLM logs."""
        return self.logs_dir / "llm_requests"

    @property
    def settings_json(self) -> Path:
        """``~/.kodo/settings.json`` — the single global settings file."""
        return self.__root / "settings.json"

    @property
    def server_discovery(self) -> Path:
        """``~/.kodo/kodo-server`` — discovery file holding ``{pid, port}``."""
        return self.__root / "kodo-server"

    def init(self) -> None:
        """Create the global ``~/.kodo/`` skeleton (sessions + logs)."""
        self.kodo_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)


class SessionWorkspace:
    """Per-session view of one VS Code window: physical root + logical folders.

    Each session is driven from exactly one window; this object mirrors that
    window's workspace shape.  The **physical root** is the parent directory of
    the window's first folder (the default cwd for unscoped Problem Solver
    work).  The **logical root** is the set of open workspace folders keyed by
    their (VS-Code-disambiguated) display names — a logical path begins with one
    of those names, anchoring the remainder to that folder's real physical path
    (see :func:`kodo.tools.resolve_logical`).

    Args:
        physical_root: Parent of the window's first folder; defaults to ``~``
            until the client pushes ``workspace.folders``.
        folders: Logical name → physical path of every open workspace folder.
    """

    __physical_root: Path
    __folders: dict[str, Path]

    def __init__(
        self, physical_root: Path | None = None, folders: dict[str, Path] | None = None
    ) -> None:
        self.__physical_root = (physical_root or Path.home()).resolve()
        self.__folders = {name: Path(p).resolve() for name, p in (folders or {}).items()}

    @property
    def physical_root(self) -> Path:
        """Parent directory of the window's first folder; the default cwd."""
        return self.__physical_root

    @property
    def folders(self) -> dict[str, Path]:
        """Copy of the logical-root map (name → physical path)."""
        return dict(self.__folders)

    def set_physical_root(self, root: Path) -> None:
        """Replace the physical root (pushed over the WS protocol)."""
        self.__physical_root = root.resolve()

    def set_folders(self, folders: dict[str, Path]) -> None:
        """Replace the logical-root folder map (pushed over the WS protocol)."""
        self.__folders = {name: Path(p).resolve() for name, p in folders.items()}


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
