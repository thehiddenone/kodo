"""Filesystem path conventions for Kodo.

Three concepts (see the ``project-kodo`` memory):

* :class:`WorkspaceLayout` — the **global home** tier.  Rooted at ``~/.kodo``
  and shared by the singleton server across every VS Code window.  Owns the
  global ``sessions/``, ``logs/``, ``etc/settings.json`` and the
  ``kodo-server`` discovery file.  It is window-agnostic — there is exactly
  one per machine.

* :class:`SessionWorkspace` — the **per-session window view**.  Holds the
  physical root (the parent directory of the window's first folder) plus the
  **logical root**: a mutable map of VS Code workspace-folder *names* to their
  real physical paths.  Pushed over the WS protocol per session and consumed by
  :func:`kodo.tools.resolve_logical` for Problem Solver path resolution.

* :class:`ProjectLayout` — the *single project* tier.  Rooted at a project
  folder whose ``.kodo/`` directory holds ``kodo.md`` (the project manifest) and
  owns ``specs/``, ``src/``, ``test/`` plus the git mirror checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def kodo_user_dir() -> Path:
    """``~/.kodo/`` — the global Kodo home (config, cache, sessions, logs)."""
    return Path.home() / ".kodo"


def session_attachments_dir(session_id: str) -> Path:
    """``~/.kodo/sessions/<session_id>/attachments`` — one session's stored prompt attachments.

    Mirrors the layout `kodo.state.TransientStore` builds internally (session
    root `kodo_dir/sessions/<id>`) but is exposed here as a T0 helper so
    `kodo.tools`'s `read_attachment` tool can resolve an attachment by ID
    straight from `ToolContext.session_id`, without importing `kodo.state`
    (same precedent as `session_temp_dir` for `Tool.resolve_path`).
    """
    return kodo_user_dir() / "sessions" / session_id / "attachments"


def session_temp_dir(session_id: str) -> Path:
    """``~/.kodo/sessions/<session_id>/tmp`` — one session's private scratch space.

    Lives outside every project root and workspace folder, so it is never
    reachable through the ordinary project-confined/logical path resolvers.
    The native file tools (`create_file`, `create_directory`, `edit_file`,
    `filesystem`, `find_files`, `find_text_in_files`) resolve here instead of
    the project root when called with `temporary: true` (see
    `kodo.tools.Tool.resolve_path`, doc/SECURITY.md). Changes made there never
    enter a project's checkpoint mirror — `kodo.runtime._checkpoints`'s
    `mutation_paths` is only ever asked to resolve *those same relative
    paths* against the project resolver, and the coordinator skips the call
    outright when `temporary` is set — and the security layer always allows
    them, regardless of Command Control posture.
    """
    return kodo_user_dir() / "sessions" / session_id / "tmp"


class ProjectLayoutError(Exception):
    """Raised when the project directory does not conform to the expected layout."""


class WorkspaceLayout:
    """Global home-tier path conventions for the singleton server.

    Rooted at ``~/.kodo`` (``kodo_user_dir()``).  There is one instance per
    machine, shared by every VS Code window's session.  It owns the global
    ``sessions/``/``logs/``/``etc/settings.json`` plus the ``kodo-server``
    discovery file.  Per-window state (physical root, the logical folder map,
    the bound project) lives in :class:`SessionWorkspace`, not here.

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
    def etc_dir(self) -> Path:
        """``~/.kodo/etc/`` — small owned-config files (global settings, the
        local LLM registry and download index, cloud API key settings; see
        :mod:`kodo.llms._local_registry`, :mod:`kodo.llms.llamacpp._manager`
        and kodo-vsix's ``cloud-credentials.ts``)."""
        return self.__root / "etc"

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
        """``~/.kodo/etc/settings.json`` — the single global settings file."""
        return self.etc_dir / "settings.json"

    @property
    def server_discovery(self) -> Path:
        """``~/.kodo/kodo-server`` — discovery file holding ``{pid, port}``."""
        return self.__root / "kodo-server"

    def init(self) -> None:
        """Create the global ``~/.kodo/`` skeleton (sessions + logs + etc)."""
        self.kodo_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.etc_dir.mkdir(parents=True, exist_ok=True)


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
        physical_root: Parent of the window's first folder; ``None`` until
            the client pushes ``workspace.folders`` or a project-bootstrap
            flow resolves one explicitly. Never silently defaulted to ``~``
            or any other directory — a homeless session genuinely has no
            anchor yet, and every consumer of :attr:`physical_root` must
            treat ``None`` as "nothing to work with" rather than guess.
        folders: Logical name → physical path of every open workspace folder.
    """

    __physical_root: Path | None
    __folders: dict[str, Path]

    def __init__(
        self, physical_root: Path | None = None, folders: dict[str, Path] | None = None
    ) -> None:
        self.__physical_root = physical_root.resolve() if physical_root is not None else None
        self.__folders = {name: Path(p).resolve() for name, p in (folders or {}).items()}

    @property
    def physical_root(self) -> Path | None:
        """Parent directory of the window's first folder; ``None`` if unknown."""
        return self.__physical_root

    @property
    def folders(self) -> dict[str, Path]:
        """Copy of the logical-root map (name → physical path)."""
        return dict(self.__folders)

    def set_physical_root(self, root: Path) -> None:
        """Replace the physical root (pushed over the WS protocol, or resolved
        explicitly by a project-bootstrap flow)."""
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
        """``<root>/.kodo/kodo.md`` — project manifest (lives under ``.kodo/``)."""
        return self.kodo_dir / "kodo.md"

    @property
    def specs_dir(self) -> Path:
        """``<root>/specs/`` — specification and documentation files."""
        return self.root / "specs"

    @property
    def src_dir(self) -> Path:
        """``<root>/src/`` — source code, excluding tests."""
        return self.root / "src"

    @property
    def test_dir(self) -> Path:
        """``<root>/test/`` — TDD and end-to-end test code."""
        return self.root / "test"

    @property
    def kodo_dir(self) -> Path:
        """``<root>/.kodo/`` — per-project Guided state (evolution logs, mirror)."""
        return self.root / ".kodo"

    @property
    def security_json(self) -> Path:
        """``<root>/.kodo/security.json`` — project-scoped security rules."""
        return self.kodo_dir / "security.json"

    @property
    def checkpoints_dir(self) -> Path:
        """``<root>/.kodo/checkpoints/`` — git mirror repository."""
        return self.kodo_dir / "checkpoints"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Assert that this directory looks like a Kodo project.

        Checks that ``kodo.md`` is present and contains the required
        ``# Kodo Project`` heading.  Does *not* require ``specs/``, ``src/``,
        ``test/``, or ``.kodo/`` to exist (they are created by :meth:`init`).

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

        Creates ``specs/``, ``src/``, ``test/``, ``.kodo/`` and
        ``.kodo/kodo.md`` if absent.  Refuses to overwrite an existing
        ``kodo.md`` unless ``force=True``.

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
        self.specs_dir.mkdir(exist_ok=True)
        self.src_dir.mkdir(exist_ok=True)
        self.test_dir.mkdir(exist_ok=True)
        self.kodo_dir.mkdir(exist_ok=True)

        if not self.kodo_md.exists() or force:
            self.kodo_md.write_text(_KODO_MD_TEMPLATE, encoding="utf-8")

    def init_existing(self) -> bool:
        """Augment an already-existing directory with the Kodo project layout.

        Unlike :meth:`init`, ``root`` is expected to already exist and may
        hold unrelated content: ``specs/``, ``src/`` and ``test/`` are only
        created when the directory is otherwise empty — no entries, or only
        entries whose name starts with a dot (e.g. ``.git/``,
        ``.gitignore``) — so pre-existing content is never touched.
        ``.kodo/`` and ``kodo.md`` are always created; the caller (via
        ``RootMirrorManager.prepare``) follows up with the checkpoint git
        mirror and its mandatory baseline commit.

        Returns:
            bool: ``True`` if the directory was judged empty and the
                standard ``specs/``/``src/``/``test/`` layout was created;
                ``False`` if it already had content and only ``.kodo/`` was
                added.

        Raises:
            ProjectLayoutError: ``root`` does not exist (or is not a
                directory), or ``.kodo/`` already exists under it.
        """
        if not self.root.is_dir():
            raise ProjectLayoutError(
                f"{self.root} does not exist or is not a directory — init_project "
                "augments an existing project directory, it does not create one."
            )
        if self.kodo_dir.exists():
            raise ProjectLayoutError(
                f"{self.kodo_dir} already exists — {self.root} is already a Kodo project."
            )

        is_empty = all(entry.name.startswith(".") for entry in self.root.iterdir())
        if is_empty:
            self.specs_dir.mkdir(exist_ok=True)
            self.src_dir.mkdir(exist_ok=True)
            self.test_dir.mkdir(exist_ok=True)

        self.kodo_dir.mkdir(exist_ok=True)
        self.kodo_md.write_text(_KODO_MD_TEMPLATE, encoding="utf-8")

        return is_empty

    def scaffold_kodo_dir(self) -> None:
        """Create ``.kodo/`` and a minimal ``kodo.md`` marker if absent.

        The lightweight counterpart of :meth:`init` used when Kōdo first touches
        an arbitrary directory (e.g. a Problem Solver workspace folder getting
        its checkpoint mirror): it creates only ``.kodo/`` and the ``kodo.md``
        marker — never ``specs/``, ``src/``, or ``test/`` — and never
        overwrites an existing manifest.
        """
        self.kodo_dir.mkdir(parents=True, exist_ok=True)
        if not self.kodo_md.exists():
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
