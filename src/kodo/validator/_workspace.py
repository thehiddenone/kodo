"""Simulated VS Code workspace for validation runs.

The engine never inspects VS Code itself — its whole picture of the user's
workspace is the ``workspace.folders`` message (``{physical_root, folders:
{name: path}}``) that the extension pushes on connect and on every
workspace-folders change. Tools such as ``get_root_paths`` then serve roots
from that pushed map. Simulating a workspace therefore means: create real
directories on disk, optionally seed them from elsewhere, and emit that same
payload. One root simulates a single-root window; several roots simulate a
multi-root (``.code-workspace``) window.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SimulatedWorkspace", "WorkspaceRoot"]


@dataclass(frozen=True)
class WorkspaceRoot:
    """One simulated workspace folder.

    Attributes:
        name: The VS Code workspace-folder display name (the logical root key).
        path: Absolute directory backing the folder.
    """

    name: str
    path: Path


class SimulatedWorkspace:
    """A scratch directory tree posing as the folders of one VS Code window.

    Roots are created under ``base_dir`` (one subdirectory per workspace
    folder), which doubles as the window's *physical root* — mirroring the
    real extension, where the physical root is the parent directory of the
    window's first folder.

    Args:
        base_dir: Directory to hold every simulated workspace folder.
    """

    __base_dir: Path
    __roots: list[WorkspaceRoot]

    def __init__(self, base_dir: Path) -> None:
        self.__base_dir = base_dir.resolve()
        self.__base_dir.mkdir(parents=True, exist_ok=True)
        self.__roots = []

    @property
    def physical_root(self) -> Path:
        """Parent directory of every simulated folder (the window's cwd)."""
        return self.__base_dir

    @property
    def roots(self) -> list[WorkspaceRoot]:
        """Copy of the simulated workspace folders, in creation order."""
        return list(self.__roots)

    def add_root(self, name: str, *, seed_from: Path | None = None) -> WorkspaceRoot:
        """Create one workspace folder, optionally seeded from a source tree.

        Call once for a single-root workspace, several times for multi-root.

        Args:
            name (str): Workspace-folder display name; also the directory name.
            seed_from (Path | None): Existing file or directory whose content
                initializes the new root (copied, source untouched).

        Returns:
            WorkspaceRoot: The created root.

        Raises:
            ValueError: If a root with *name* already exists.
            FileNotFoundError: If *seed_from* does not exist.
        """
        if any(r.name == name for r in self.__roots):
            raise ValueError(f"Workspace root already exists: {name!r}")
        path = self.__base_dir / name
        path.mkdir(parents=True, exist_ok=True)
        root = WorkspaceRoot(name=name, path=path)
        self.__roots.append(root)
        if seed_from is not None:
            self.seed(name, seed_from)
        return root

    def seed(self, root_name: str, source: Path, *, dest_rel: str = "") -> Path:
        """Copy a file or directory tree into an existing root.

        Args:
            root_name (str): Name of a root created via :meth:`add_root`.
            source (Path): File or directory to copy from (untouched).
            dest_rel (str): Destination relative to the root; empty means the
                root itself for directories, or the source's basename for files.

        Returns:
            Path: Absolute path of the copied file / directory root.

        Raises:
            KeyError: If *root_name* is unknown.
            FileNotFoundError: If *source* does not exist.
        """
        root = self.__require_root(root_name)
        src = source.resolve()
        if not src.exists():
            raise FileNotFoundError(f"Seed source does not exist: {src}")
        if src.is_dir():
            dest = root.path / dest_rel if dest_rel else root.path
            shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)
        else:
            dest = root.path / (dest_rel or src.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return dest

    def write_file(self, root_name: str, rel_path: str, content: str) -> Path:
        """Write a text file into a root (convenience for small fixtures).

        Args:
            root_name (str): Name of a root created via :meth:`add_root`.
            rel_path (str): File path relative to the root.
            content (str): UTF-8 text content.

        Returns:
            Path: Absolute path of the written file.

        Raises:
            KeyError: If *root_name* is unknown.
        """
        root = self.__require_root(root_name)
        dest = root.path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return dest

    def root_path(self, root_name: str) -> Path:
        """Absolute directory of the named root.

        Args:
            root_name (str): Name of a root created via :meth:`add_root`.

        Returns:
            Path: The root's directory.

        Raises:
            KeyError: If *root_name* is unknown.
        """
        return self.__require_root(root_name).path

    def folders_payload(self) -> dict[str, object]:
        """The ``workspace.folders`` payload body describing this workspace.

        Returns:
            dict[str, object]: ``{"physical_root": ..., "folders": {name: path}}``.
        """
        return {
            "physical_root": str(self.__base_dir),
            "folders": {r.name: str(r.path) for r in self.__roots},
        }

    def __require_root(self, root_name: str) -> WorkspaceRoot:
        for root in self.__roots:
            if root.name == root_name:
                return root
        raise KeyError(f"Unknown workspace root: {root_name!r}")
