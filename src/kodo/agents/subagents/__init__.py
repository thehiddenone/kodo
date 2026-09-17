"""Sub-agents — the agents another agent delegates to.

A sub-agent is "a tool with agentic behavior": something an agent *calls*, with
arguments, and gets a typed result back from. That is the whole distinction from
a top-level agent (:mod:`kodo.agents`), which a human selects and talks to in
prose, and it is why everything sub-agent-shaped lives here:

- ``subagent_<name>.md`` — the prompt and its frontmatter.
- :mod:`.specs` — one ``<name>.json`` per sub-agent, its input/output contract.
- :class:`SubAgentSpec` — what such a file builds.
- :mod:`._artifacts` — the artifact *roles* and *scopes* a spec's
  ``produces``/``consumes`` are written in, which only sub-agents declare.

What is deliberately **not** here: :class:`~kodo.agents.SubAgent`,
:func:`~kodo.agents.load_agent` and :class:`~kodo.agents.AgentRegistry`. Those
are shared machinery — the same parser reads both kinds of prompt, and one
registry holds them together — so they stay in the parent package. The
dependency runs one way, parent → child: nothing here imports
:mod:`kodo.agents`.
"""

from ._artifacts import (
    ALL_ROLES,
    ALL_SCOPES,
    PRODUCES_REMAINDER,
    ROLE_ARCHITECTURE,
    ROLE_CODE,
    ROLE_DESIGN_PLAN,
    ROLE_E2E_TEST_CODE,
    ROLE_E2E_TEST_PLAN,
    ROLE_FUNCTIONAL_DESIGN,
    ROLE_NARRATIVE,
    ROLE_REQUIREMENTS,
    ROLE_TECH_STACK,
    ROLE_TEST_CODE,
    ROLE_TEST_PLAN,
    SCOPE_ALL,
    SCOPE_DEPENDENCIES,
    SCOPE_GLOBAL,
    SCOPE_SELF,
    SCOPE_UNDER_REVIEW,
    Need,
)
from ._subagentspec import RESPONSIBILITY_CODE_KEY, SubAgentSpec
from .specs import ALL_SUBAGENTS

__all__: list[str] = [
    "ALL_ROLES",
    "ALL_SCOPES",
    "ALL_SUBAGENTS",
    "PRODUCES_REMAINDER",
    "RESPONSIBILITY_CODE_KEY",
    "ROLE_ARCHITECTURE",
    "ROLE_CODE",
    "ROLE_DESIGN_PLAN",
    "ROLE_E2E_TEST_CODE",
    "ROLE_E2E_TEST_PLAN",
    "ROLE_FUNCTIONAL_DESIGN",
    "ROLE_NARRATIVE",
    "ROLE_REQUIREMENTS",
    "ROLE_TECH_STACK",
    "ROLE_TEST_CODE",
    "ROLE_TEST_PLAN",
    "SCOPE_ALL",
    "SCOPE_DEPENDENCIES",
    "SCOPE_GLOBAL",
    "SCOPE_SELF",
    "SCOPE_UNDER_REVIEW",
    "Need",
    "SubAgentSpec",
]
