"""Render the installed skills as the catalog block an agent's prompt carries.

This is the *first* half of the progressive-disclosure contract (doc/SKILLS.md
§3): every usable skill contributes one line — its name and its whole
``description`` — so the model can tell, from the system prompt alone, whether
any skill is relevant. The second half is the ``use_skill`` tool, which returns
the full ``SKILL.md`` body of the one skill the model picks. Bodies are never
rendered here; a dozen installed skills would otherwise cost thousands of
tokens on every turn of every agent that holds the tool.

Descriptions are reproduced verbatim and uncropped. In this format the
description *is* the routing signal ("Use this skill whenever the user wants
to…"), so truncating it to save tokens would defeat the mechanism it exists
for.
"""

from __future__ import annotations

from collections.abc import Sequence

from ._skill import Skill

__all__ = ["render_catalog"]

_HEADING = "## Available skills"

_PREAMBLE = (
    "A **skill** is a set of expert instructions for one kind of task, installed "
    "by the user. Each is listed below by name, followed by the situations it is "
    "for. When a task matches one, call `use_skill` with that name **before "
    "planning your approach** — the skill's instructions supersede your own "
    "default way of doing that task. Consult at most one skill at a time, and "
    "only when its description genuinely covers the work in front of you; "
    "nothing here is required reading."
)

_EMPTY = (
    "No skills are installed. `use_skill` has nothing to return until the user "
    "adds one, so do not call it."
)


def render_catalog(skills: Sequence[Skill]) -> str:
    """The catalog block for *skills*, ready to substitute into a prompt.

    Args:
        skills: The usable skills to advertise, in the order they should be
            listed (``SkillStore.usable()`` yields them name-sorted). Broken
            skills must already be filtered out — this renders whatever it is
            given.

    Returns:
        str: A markdown section starting with ``## Available skills``. Never
            empty: with no skills installed it renders a short block telling
            the agent so, which is what stops a model from inventing a skill
            name to pass to ``use_skill``.
    """
    if not skills:
        return f"{_HEADING}\n\n{_EMPTY}"
    lines = [f"- **{skill.name}** — {_one_line(skill.description)}" for skill in skills]
    return f"{_HEADING}\n\n{_PREAMBLE}\n\n" + "\n".join(lines)


def _one_line(description: str) -> str:
    """Collapse a description to a single line so one skill is always one row."""
    return " ".join(description.split())
