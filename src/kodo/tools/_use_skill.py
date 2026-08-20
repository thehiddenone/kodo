"""``use_skill`` tool — return one installed skill's full instructions.

The second half of the progressive-disclosure contract (doc/SKILLS.md §3): the
agent's system prompt already carries every installed skill's name and
description (rendered by :func:`kodo.skills.render_catalog` when the registry
expands the ``{SKILLS}`` token), and this tool hands back the body of the one
the model picks — together with the skill's directory path, so the instructions
can point at companion files the agent then opens with ``read_file``.

The store is re-scanned per call rather than snapshotted at session start: a
skill dropped into ``~/.kodo/skills`` mid-session is usable on the next turn,
and one deleted from the Kōdo Settings panel stops resolving immediately.
"""

from __future__ import annotations

import json
import logging

from kodo.project import kodo_skills_dir
from kodo.skills import SkillStore

from ._tool import Tool

__all__ = ["UseSkillTool"]

_log = logging.getLogger(__name__)


class UseSkillTool(Tool):
    """Load the instructions of one skill named in the prompt's catalog."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        name = str(tool_input.get("name", "")).strip()
        if not name:
            return json.dumps({"error": "No skill name given."})

        store = SkillStore(kodo_skills_dir())
        skill = store.get(name)
        if skill is None:
            # Name the real options rather than just refusing: the catalog is
            # in the prompt, so a miss here means the model paraphrased a name
            # or a skill was deleted mid-session, and both recover from a list.
            available = [entry.name for entry in store.usable()]
            listed = ", ".join(available) if available else "(none installed)"
            return json.dumps(
                {"error": f"No skill named {name!r} is installed. Available skills: {listed}."}
            )

        _log.info("use_skill %r loaded by %s", skill.name, self.context.agent_name)
        return json.dumps(
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.root),
                "instructions": skill.body,
            }
        )
