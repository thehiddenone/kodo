"""The skills store — scan, look up, and delete skills under two roots.

One :class:`SkillStore` per user root (``~/.kodo/skills`` in production, a
temp dir in tests). The user root is taken as a constructor argument rather
than read from :func:`kodo.project.kodo_skills_dir` inside, matching
``kodo.websearch``'s store convention and keeping this package a leaf that
imports nothing from ``kodo``.

The store also reads a second, read-only root: the **shipped skills** packaged
with Kōdo (:data:`~kodo.skills.BUILTIN_SKILLS_DIR`, doc/AGENT_BUILDER.md §7).
Shipped skill names start ``kodo-`` and no user skill may, so a name is never
in both roots and neither shadows the other. A user directory that takes the
prefix anyway is listed as broken, and stays deletable.

The store is **stateless between calls**: every :meth:`SkillStore.entries` re-scans
the directory. Skills are installed by hand — the user drops a directory in
while the server is running and expects it to work — so there is no cache to
invalidate and no refresh command to forget to send.
"""

from __future__ import annotations

from pathlib import Path

from ._skill import BUILTIN_SKILLS_DIR, Skill, load_skill

__all__ = ["SkillDeleteError", "SkillStore"]


class SkillDeleteError(Exception):
    """Raised when a skill cannot be deleted (unknown, escaping, or I/O error)."""


class SkillStore:
    """Read the shipped skills, and read and delete the user's own.

    Args:
        root: The user skills root (``~/.kodo/skills``). Need not exist — a
            missing root simply means the user has installed no skills.
        builtin_root: The read-only shipped skills root. Defaults to the one
            packaged with Kōdo; ``None`` reads the user root alone.
    """

    __root: Path
    __builtin_root: Path | None

    def __init__(self, root: Path, *, builtin_root: Path | None = BUILTIN_SKILLS_DIR) -> None:
        self.__root = root
        self.__builtin_root = builtin_root

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
        """Every skill directory under both roots, name-sorted, broken ones included.

        A non-directory entry (a stray ``README.md``, a ``.DS_Store``) is not a
        skill and is skipped silently; a *directory* that fails to load is
        returned with its :attr:`Skill.error` set, so the Settings panel can
        show the user what is wrong and offer to delete it.

        Returns:
            list[Skill]: Sorted by :attr:`Skill.name` (case-insensitive), shipped
            and user skills together.
        """
        found = [load_skill(entry, builtin=True) for entry in _directories(self.__builtin_root)]
        found += [load_skill(entry) for entry in _directories(self.__root)]
        return sorted(found, key=lambda skill: skill.name.lower())

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
        shipped = self.__resolve_builtin(name)
        if shipped is not None:
            builtin = load_skill(shipped, builtin=True)
            return builtin if builtin.usable else None
        resolved = self.__resolve(name)
        if resolved is None or not resolved.is_dir():
            return None
        skill = load_skill(resolved)
        return skill if skill.usable else None

    def delete(self, name: str) -> None:
        """Delete the user skill directory named *name*, recursively.

        Only the user root is ever touched. A user directory that shares a
        shipped skill's name is deleted (it is broken, having taken the
        reserved prefix); a shipped skill itself is refused.

        Args:
            name: Skill directory name.

        Raises:
            SkillDeleteError: *name* is not a direct child directory of the
                user root (empty, separator-bearing, ``..``, a symlink pointing
                out), is a shipped skill, or the removal itself failed.
        """
        resolved = self.__resolve(name)
        if resolved is None:
            raise SkillDeleteError(f"{name!r} is not a skill in {self.__root}.")
        if not resolved.is_dir():
            if self.__resolve_builtin(name) is not None:
                raise SkillDeleteError(
                    f"{name!r} is a built-in skill that ships with Kōdo and cannot be deleted."
                )
            raise SkillDeleteError(f"No skill named {name!r} is installed.")
        # Imported lazily: this is the only call in the package that mutates
        # the filesystem, and keeping the import next to it makes that obvious.
        import shutil

        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise SkillDeleteError(f"Could not delete {name!r}: {exc.strerror or exc}") from exc

    def __resolve_builtin(self, name: str) -> Path | None:
        """The shipped skill directory named *name*, or ``None``."""
        if self.__builtin_root is None:
            return None
        resolved = _contained(self.__builtin_root, name)
        return resolved if resolved is not None and resolved.is_dir() else None

    def __resolve(self, name: str) -> Path | None:
        """*name* as an absolute path directly under the user root, or ``None``.

        The containment guard for both :meth:`get` and :meth:`delete`, and the
        reason ``delete`` cannot be talked into removing something outside the
        store. ``name`` arrives from the LLM (``use_skill``) and from the
        Settings panel over the wire, so it is validated three ways: it must be
        a single path component with no separator and no ``.``/``..``, and
        after ``resolve()`` — which follows symlinks — its parent must still be
        the resolved root.
        """
        return _contained(self.__root, name)


def _directories(root: Path | None) -> list[Path]:
    """Every directory directly under *root*; empty when it cannot be listed."""
    if root is None or not root.is_dir():
        return []
    try:
        return [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _contained(root: Path, name: str) -> Path | None:
    """*name* as an absolute path directly under *root*, or ``None`` (see ``__resolve``)."""
    candidate = name.strip()
    if not candidate or candidate in (".", ".."):
        return None
    if "/" in candidate or "\\" in candidate or Path(candidate).name != candidate:
        return None
    try:
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
    except OSError:
        return None
    if resolved.parent != resolved_root:
        return None
    return resolved
