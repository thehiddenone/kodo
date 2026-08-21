"""Agent Skills — user-installed instruction packs under ``~/.kodo/skills``.

A skill is a directory holding a ``SKILL.md`` in the open Agent Skill format
(YAML frontmatter with ``name``/``description``, then the instruction body)
plus any files that body references. A skill is installed by hand (copy a
directory into the skills root), with :func:`install_local_skill` (a local
``SKILL.md`` file or directory, no ``git``), or with :func:`scan_repository` /
:func:`install_skills`, which pull one (or several) from a git repository —
see doc/SKILLS.md §2. Either way, Kōdo only reads, installs, and deletes
them; it never writes to one on its own.

Reaching an agent is a two-step, progressive-disclosure contract (doc/SKILLS.md):
:func:`render_catalog` puts every installed skill's *name and description* into
the system prompt of each agent that declares the ``use_skill`` tool, and that
tool returns the full body of the one skill the model chooses.

This is a leaf package: it imports nothing from ``kodo``, taking the skills
root as a :class:`SkillStore` constructor argument (``kodo.project.
kodo_skills_dir()`` at every production call site).
"""

from ._catalog import render_catalog
from ._install import (
    GitNotAvailableError,
    InstallResult,
    SkillInstallError,
    install_local_skill,
    install_skills,
    scan_repository,
)
from ._skill import SKILL_FILE, Skill, load_skill
from ._store import SkillDeleteError, SkillStore

__all__ = [
    "SKILL_FILE",
    "GitNotAvailableError",
    "InstallResult",
    "Skill",
    "SkillDeleteError",
    "SkillInstallError",
    "SkillStore",
    "install_local_skill",
    "install_skills",
    "load_skill",
    "render_catalog",
    "scan_repository",
]
