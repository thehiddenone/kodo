"""Installing Agent Skills from a git repository (doc/SKILLS.md §2).

Two entry points, both synchronous (blocking on the network/disk — the server
wraps calls in ``asyncio.to_thread``, same convention as local-model install;
see ``kodo/server/_app.py``'s existing ``to_thread`` call sites):

- :func:`scan_repository` clones *repo_url* to a throwaway temp directory,
  finds every ``SKILL.md`` in it, and returns the ones that load cleanly —
  name and description only, for a picker. The clone is deleted before this
  returns; nothing here can be used to install the skill.
- :func:`install_skills` clones *repo_url* again (independently — this
  package caches nothing between calls, matching :class:`~kodo.skills.SkillStore`'s
  own "stateless between calls" convention) and copies each *selected* skill's
  whole directory into *skills_root*, the same "copy the directory" a user
  would do by hand.

Both re-run the exact same discovery as the other: a name accepted by
:func:`scan_repository` is exactly the set :func:`install_skills` will find
again, short of the repo changing underneath the two calls (a request already
covered by :attr:`InstallResult.missing`).

A repo is scanned for **every** ``SKILL.md`` it contains, not just one at
the root — a single repo may bundle several skills, one per subdirectory. A
``SKILL.md`` at the clone root itself has no directory of its own to name it
after (the clone root's name is a random temp-dir string), so that one case
uses the repo's own name — the last path segment of *repo_url*, ``.git``
stripped — as its install name instead (see :func:`_repo_basename`).

A third entry point, :func:`install_local_skill`, installs from a local
``SKILL.md`` file or directory already on disk — no ``git``, no clone, no
picker. It backs the Kōdo Settings panel's "Install from a local file" file
picker and ``python -m kodo --install-skill``'s local-path support
(doc/SKILLS.md §2). Unlike the repo flow it installs **exactly one** skill —
the one at the given path — since a directory the user pointed at directly
has no ambiguity to resolve with a picker the way a whole repository does.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ._skill import SKILL_FILE, Skill, load_skill

__all__ = [
    "GitNotAvailableError",
    "InstallResult",
    "SkillInstallError",
    "install_local_skill",
    "install_skills",
    "require_git",
    "scan_repository",
]

# `git clone` runs against a URL the user typed; an unreachable host must not
# hang the calling thread (or, on the server, the asyncio.to_thread worker)
# forever.
_CLONE_TIMEOUT_SECONDS = 60


class GitNotAvailableError(Exception):
    """Raised when the ``git`` CLI is not on ``PATH``."""


class SkillInstallError(Exception):
    """Raised when cloning a repository, or installing from it, fails."""


@dataclass(frozen=True)
class InstallResult:
    """The outcome of one :func:`install_skills` call.

    Attributes:
        installed: Names actually copied into the skills root (fresh or
            overwritten).
        conflicts: Requested names that already exist under the skills root
            and were *not* overwritten because the caller did not mark them
            for overwrite. Nothing on disk was touched for these.
        missing: Requested names not found among this clone's valid skills —
            normally means the repo changed between a prior scan and this
            call.
    """

    installed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def require_git() -> None:
    """Raise :class:`GitNotAvailableError` if the ``git`` CLI is not on ``PATH``."""
    if shutil.which("git") is None:
        raise GitNotAvailableError(
            "git is required to install skills from a repository, but was not found on PATH."
        )


def scan_repository(repo_url: str) -> list[Skill]:
    """Clone *repo_url* and return the valid skills it contains.

    The clone is deleted before returning. Only :attr:`Skill.usable` results
    are returned (see :attr:`~kodo.skills.Skill.usable`) — a repo may hold
    other Markdown files or a malformed ``SKILL.md`` that isn't a skill;
    :func:`install_skills` applies the identical filter, so this is exactly
    the picker: whatever :meth:`Skill.name` a candidate returned here has,
    :func:`install_skills` will find under.

    Args:
        repo_url: Any URL ``git clone`` accepts (``https://…``, ``git@…``, or
            a local path — the last is what the test suite uses).

    Returns:
        list[Skill]: Name-sorted, description populated, ``root``/``body``
        pointing into the now-deleted clone (do not read them from this
        return value — see the module docstring).

    Raises:
        GitNotAvailableError: ``git`` is not on ``PATH``.
        SkillInstallError: The clone failed (bad URL, network error, no such
            branch, timeout).
    """
    require_git()
    with tempfile.TemporaryDirectory(prefix="kodo-skill-scan-") as tmp:
        clone_root = Path(tmp)
        _git_clone(repo_url, clone_root)
        return [skill for skill in _discover_skills(clone_root, repo_url) if skill.usable]


def install_skills(repo_url: str, selections: dict[str, bool], skills_root: Path) -> InstallResult:
    """Clone *repo_url* and copy each selected skill into *skills_root*.

    Args:
        repo_url: Same as :func:`scan_repository`.
        selections: Install-name -> whether an existing same-named skill
            under *skills_root* may be overwritten. A name absent from the
            freshly re-scanned clone is reported in
            :attr:`InstallResult.missing`, not installed. A name present but
            already installed with ``overwrite`` false is reported in
            :attr:`InstallResult.conflicts`, also not installed — this is the
            one enforcement point for "prompt to confirm overwrite"
            (doc/SKILLS.md §2): the caller (CLI loop or the Kōdo Settings
            panel's install modal) collects that confirmation before calling,
            and this still re-checks rather than trusting it blindly, since
            the two calls are independent and the target can change between
            them.
        skills_root: Normally ``kodo.project.kodo_skills_dir()``.

    Returns:
        InstallResult: What happened to each requested name.

    Raises:
        GitNotAvailableError: ``git`` is not on ``PATH``.
        SkillInstallError: The clone failed.
    """
    require_git()
    result = InstallResult()
    with tempfile.TemporaryDirectory(prefix="kodo-skill-install-") as tmp:
        clone_root = Path(tmp)
        _git_clone(repo_url, clone_root)
        found = {
            skill.name: skill for skill in _discover_skills(clone_root, repo_url) if skill.usable
        }

        for name, overwrite in selections.items():
            skill = found.get(name)
            if skill is None:
                result.missing.append(name)
                continue
            target = skills_root / name
            if target.exists() and not overwrite:
                result.conflicts.append(name)
                continue
            skills_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill.root, target)
            result.installed.append(name)

    return result


def install_local_skill(path: str, skills_root: Path, *, overwrite: bool) -> InstallResult:
    """Install exactly one skill from a local ``SKILL.md`` file or directory.

    No ``git``, no clone, no picker — *path* already names the one skill to
    install. This is an assisted version of doc/SKILLS.md §2's "by hand"
    method (copy a directory into ``skills_root``), not the repo flow: it
    does not scan *path* recursively for further ``SKILL.md`` files the way
    :func:`scan_repository`/:func:`install_skills` scan a whole clone, so a
    directory bundling several skills must be installed one at a time, each
    by pointing this at its own subdirectory.

    Args:
        path: A filesystem path to either a ``SKILL.md`` file or the
            directory containing one. Relative paths are resolved against
            the current working directory — only meaningful for the CLI
            entry point, since the Kōdo Settings panel always sends an
            absolute path from its native file picker.
        skills_root: Normally ``kodo.project.kodo_skills_dir()``.
        overwrite: Whether a same-named skill already installed under
            *skills_root* may be replaced. When false and one exists,
            nothing on disk is touched and the name is reported in
            :attr:`InstallResult.conflicts` instead of
            :attr:`InstallResult.installed` — the caller (CLI prompt or the
            settings panel's confirm dialog) collects that confirmation and
            calls again with ``overwrite=True``.

    Returns:
        InstallResult: :attr:`InstallResult.missing` is always empty here —
        it exists only for :func:`install_skills`' re-scan case, which does
        not apply to a direct path. Exactly one of
        :attr:`InstallResult.installed`/:attr:`InstallResult.conflicts`
        holds the skill's name.

    Raises:
        SkillInstallError: *path* does not exist, is a file not named
            ``SKILL.md``, its ``SKILL.md`` fails to load (doc/SKILLS.md §1),
            or *path* already **is** the installed copy of this skill (would
            make ``install_skills``' overwrite-by-delete-then-copy destroy
            its own source).
    """
    directory = _resolve_local_skill_dir(path)
    skill = load_skill(directory)
    if skill.error:
        raise SkillInstallError(f"{skill.skill_md}: {skill.error}")

    result = InstallResult()
    target = skills_root / skill.name
    if target.exists():
        if target.resolve() == skill.root.resolve():
            raise SkillInstallError(
                f"{directory} is already the installed copy of the {skill.name!r} skill."
            )
        if not overwrite:
            result.conflicts.append(skill.name)
            return result
        shutil.rmtree(target)

    skills_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill.root, target)
    result.installed.append(skill.name)
    return result


def _resolve_local_skill_dir(path: str) -> Path:
    """Resolve *path* (a ``SKILL.md`` file or its directory) to a skill directory.

    Args:
        path: Absolute or relative filesystem path. A relative path is
            resolved against the current working directory.

    Returns:
        Path: The directory holding ``SKILL.md`` — *path* itself if it is
        already that directory, or its parent if *path* is the file.

    Raises:
        SkillInstallError: *path* does not exist, or is a file whose name is
            not exactly ``SKILL.md``.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        if candidate.name != SKILL_FILE:
            raise SkillInstallError(f"{candidate} is not a {SKILL_FILE} file.")
        return candidate.parent
    raise SkillInstallError(f"{candidate} does not exist.")


def _git_clone(repo_url: str, dest: Path) -> None:
    """Shallow-clone *repo_url*'s default branch into *dest* (must not yet exist)."""
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", "--", repo_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillInstallError(
            f"git clone of {repo_url!r} timed out after {_CLONE_TIMEOUT_SECONDS}s."
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise SkillInstallError(f"git clone of {repo_url!r} failed: {detail}")


def _discover_skills(clone_root: Path, repo_url: str) -> list[Skill]:
    """Every ``SKILL.md`` under *clone_root*, loaded with its install name.

    Sorted by path for deterministic output; a clone-root-level ``SKILL.md``
    (the whole repo is one skill) is named after *repo_url* rather than the
    temp clone directory (see the module docstring).
    """
    results = []
    for skill_md in sorted(clone_root.rglob(SKILL_FILE)):
        directory = skill_md.parent
        if ".git" in directory.relative_to(clone_root).parts:
            continue
        install_name = _repo_basename(repo_url) if directory == clone_root else directory.name
        results.append(load_skill(directory, name=install_name))
    return results


_TRAILING_GIT_RE = re.compile(r"\.git$")


def _repo_basename(repo_url: str) -> str:
    """The repo's own name: the last path segment of *repo_url*, ``.git`` stripped.

    Handles both URL forms real hosts use: ``https://host/owner/name(.git)``
    and scp-like ``git@host:owner/name(.git)``.
    """
    trimmed = _TRAILING_GIT_RE.sub("", repo_url.strip().rstrip("/"))
    return re.split(r"[/:]", trimmed)[-1]
