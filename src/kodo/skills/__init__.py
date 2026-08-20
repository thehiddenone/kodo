"""Agent Skills — user-installed instruction packs under ``~/.kodo/skills``.

A skill is a directory holding a ``SKILL.md`` in the open Agent Skill format
(YAML frontmatter with ``name``/``description``, then the instruction body)
plus any files that body references. Skills are installed by hand; Kōdo only
reads and deletes them.

Reaching an agent is a two-step, progressive-disclosure contract (doc/SKILLS.md):
:func:`render_catalog` puts every installed skill's *name and description* into
the system prompt of each agent that declares the ``use_skill`` tool, and that
tool returns the full body of the one skill the model chooses.

This is a leaf package: it imports nothing from ``kodo``, taking the skills
root as a :class:`SkillStore` constructor argument (``kodo.project.
kodo_skills_dir()`` at every production call site).
"""

from ._catalog import render_catalog
from ._skill import SKILL_FILE, Skill, load_skill
from ._store import SkillDeleteError, SkillStore

__all__ = [
    "SKILL_FILE",
    "Skill",
    "SkillDeleteError",
    "SkillStore",
    "load_skill",
    "render_catalog",
]
