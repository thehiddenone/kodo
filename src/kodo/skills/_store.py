"""The skills store — scan, look up, and delete skills under one root.

One :class:`SkillStore` per root directory (``~/.kodo/skills`` in production,
a temp dir in tests). The root is taken as a constructor argument rather than
read from :func:`kodo.project.kodo_skills_dir` inside, matching
``kodo.websearch``'s store convention and keeping this package a leaf that
imports nothing from ``kodo``.

The store is **stateless between calls**: every :meth:`SkillStore.entries` re-scans
the directory. Skills are installed by hand — the user drops a directory in
while the server is running and expects it to work — so there is no cache to
invalidate and no refresh command to forget to send.
"""

from __future__ import annotations

from pathlib import Path

from ._skill import Skill, load_skill

__all__ = ["SkillDeleteError", "SkillStore"]


class SkillDeleteError(Exception):
    """Raised when a skill cannot be deleted (unknown, escaping, or I/O error)."""


class SkillStore:
    """Read and delete the skills installed under one root directory.

    Args:
        root: The skills root (``~/.kodo/skills``). Need not exist — a missing
            root simply means no skills are installed.
    """

    __root: Path

    def __init__(self, root: Path) -> None:
        self.__root = root

    @property
    def root(self) -> Path:
        """The skills root directory this store scans."""
        return self.__root

    def ensure_root(self) -> Path:
        """Create the skills root if absent and return it.

        Called on server startup so the directory the user is told to drop
        skills into actually exists, and so the Settings panel's "open the
        skills folder" action always has something to open. Never raises for a
        root that already exists.
        """
        self.__root.mkdir(parents=True, exist_ok=True)
        return self.__root

    def entries(self) -> list[Skill]:
        """Every skill directory under the root, name-sorted, broken ones included.

        A non-directory entry (a stray ``README.md``, a ``.DS_Store``) is not a
        skill and is skipped silently; a *directory* that fails to load is
        returned with its :attr:`Skill.error` set, so the Settings panel can
        show the user what is wrong and offer to delete it.

        Returns:
            list[Skill]: Sorted by :attr:`Skill.name` (case-insensitive).
        """
        if not self.__root.is_dir():
            return []
        try:
            entries = sorted(self.__root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        return [load_skill(entry) for entry in entries if entry.is_dir()]

    def usable(self) -> list[Skill]:
        """The subset of :meth:`entries` an agent may actually be offered."""
        return [skill for skill in self.entries() if skill.usable]

    def get(self, name: str) -> Skill | None:
        """The usable skill named *name*, or ``None``.

        *name* is matched against the directory name (a skill's identity; see
        :class:`~kodo.skills.Skill`). A broken skill returns ``None`` — from an
        agent's point of view it does not exist.

        Args:
            name: Skill directory name, as listed in the prompt catalog.
        """
        resolved = self.__resolve(name)
        if resolved is None or not resolved.is_dir():
            return None
        skill = load_skill(resolved)
        return skill if skill.usable else None

    def delete(self, name: str) -> None:
        """Delete the skill directory named *name*, recursively.

        Args:
            name: Skill directory name.

        Raises:
            SkillDeleteError: *name* is not a direct child directory of the
                root (empty, separator-bearing, ``..``, a symlink pointing
                out), or the removal itself failed.
        """
        resolved = self.__resolve(name)
        if resolved is None:
            raise SkillDeleteError(f"{name!r} is not a skill in {self.__root}.")
        if not resolved.is_dir():
            raise SkillDeleteError(f"No skill named {name!r} is installed.")
        # Imported lazily: this is the only call in the package that mutates
        # the filesystem, and keeping the import next to it makes that obvious.
        import shutil

        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise SkillDeleteError(f"Could not delete {name!r}: {exc.strerror or exc}") from exc

    def __resolve(self, name: str) -> Path | None:
        """*name* as an absolute path directly under the root, or ``None``.

        The containment guard for both :meth:`get` and :meth:`delete`, and the
        reason ``delete`` cannot be talked into removing something outside the
        store. ``name`` arrives from the LLM (``use_skill``) and from the
        Settings panel over the wire, so it is validated three ways: it must be
        a single path component with no separator and no ``.``/``..``, and
        after ``resolve()`` — which follows symlinks — its parent must still be
        the resolved root.
        """
        candidate = name.strip()
        if not candidate or candidate in (".", ".."):
            return None
        if "/" in candidate or "\\" in candidate or Path(candidate).name != candidate:
            return None
        try:
            root = self.__root.resolve()
            resolved = (root / candidate).resolve()
        except OSError:
            return None
        if resolved.parent != root:
            return None
        return resolved
