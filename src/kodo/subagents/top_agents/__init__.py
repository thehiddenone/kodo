"""Top-level agent configs — how each user-facing agent is selected and shown.

This package holds **one ``<name>.json`` file per top-level agent** (``guide``,
``problem_solver``, ``judge``), plus the loader that turns them into
:class:`~kodo.subagents.TopAgent` records. It is the selection half of a
top-level agent; the behaviour half is its ``agent_<name>.md`` prompt and
frontmatter, one directory up.

This is the counterpart of :mod:`kodo.subagents.specs` for the agents that have
no typed contract. A sub-agent's extra declaration is its input/output schema,
because something calls it with arguments; a top-level agent talks to a human in
prose and is *chosen*, so what it declares instead is which value selects it,
what to call it in a picker, and whether it belongs in one.

Unlike :mod:`~kodo.subagents.specs` there is no import-time catalog constant
here — the registry loads whichever directory it was built with. :mod:`._loader`
explains why that difference is load-bearing rather than incidental.
"""

from .._topagent import TopAgent
from ._loader import TOP_AGENT_SUFFIX, TopAgentLoadError, load_top_agent, load_top_agents

__all__ = [
    "TOP_AGENT_SUFFIX",
    "TopAgent",
    "TopAgentLoadError",
    "load_top_agent",
    "load_top_agents",
]
