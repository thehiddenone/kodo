"""The shared ``intent`` parameter carried by every content-mutating tool.

Every **first-degree mutator** — a tool whose own dispatch changes content on
disk (``filesystem``, ``edit_file``, ``create_file``, ``create_directory``,
``run_command``, ``create_new_project``, ``rollback``) — declares a mandatory
``intent`` string
as the FIRST property of its ``input_schema``. Tools that only mutate
*through other agents* (``run_subagent``, ``run_author_critic_iteration``,
``toolchain_deps``) are exempt: the sub-agent's own first-degree calls carry
their own intents.

The property is defined once here (:data:`INTENT_PROPERTY`) so its guidance —
the generic "how to state your intent" instructions every agent reads — lives
in exactly one place and can never drift between specs. It is declared with
``"always"`` visibility and, being the first schema property, renders as the
top row of the tool-call detail box.

The security layer consumes the declared intent to judge each call —
auto-allow, auto-deny, or ask the user. :class:`~kodo.tools.ToolDispatcher`
enforces presence generically: any call to a spec that requires ``intent``
(see :func:`requires_intent`) is rejected before dispatch when the field is
missing or blank.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["INTENT_KEY", "INTENT_PROPERTY", "requires_intent"]

INTENT_KEY = "intent"

# The generic instructions for declaring intent, shown to every agent holding a
# mutating tool (this description is the single source; each spec embeds it).
_INTENT_DESCRIPTION = (
    "One sentence stating the purpose of THIS call: what it changes and why "
    "that serves your current task (e.g. 'Create the parser module the plan's "
    "step 2 specifies', not 'create a file'). The security layer and the user "
    "read it to judge whether the action is expected and to allow, deny, or "
    "ask — so be specific and truthful; a vague, generic, or misleading "
    "intent invites denial. Describe this single call, not your whole task."
)

# The schema fragment every mutating tool embeds as its FIRST input property.
INTENT_PROPERTY: dict[str, object] = {
    "type": "string",
    "description": _INTENT_DESCRIPTION,
}


def requires_intent(spec: ToolSpec) -> bool:
    """Whether *spec* declares a mandatory ``intent`` input property.

    True exactly when ``intent`` is listed in the spec's ``input_schema``
    ``required`` array — the single condition the dispatcher's generic
    enforcement keys on.
    """
    required = spec.input_schema.get("required")
    return isinstance(required, (list, tuple)) and INTENT_KEY in required
