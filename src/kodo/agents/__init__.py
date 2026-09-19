"""Kōdo's agents — the prompts, the contracts that go with them, and the registry.

Two kinds of agent live here, and the package is laid out along that split:

**Top-level agents** (``agent_<name>.md`` plus a ``<name>.json`` config beside
it) are what a *human* selects and talks to — ``guide``, ``problem_solver``, ``judge``. They
have no typed contract, because nothing calls them with arguments; what they
declare instead is how they are *chosen* (:class:`TopAgent`).

**Sub-agents** (:mod:`.subagents`) are what an *agent* delegates to. Each has a
:class:`~kodo.agents.subagents.SubAgentSpec` — an input and output JSON Schema —
because it is invoked like a tool and returns a value to its caller.

Everything both kinds share stays in this package: the markdown parser
(:func:`load_agent` → :class:`SubAgent`, which reads either prefix), the prompt
blocks they include (``shared_<name>.md``), and :class:`AgentRegistry`, which
loads the whole set, expands every ``{SHARED:…}`` token, and validates the
result. The dependency runs one way — this package imports :mod:`.subagents`,
never the reverse.

``SubAgent`` is the loaded form of *either* kind, despite the name: it is one
parsed ``.md`` file, and :attr:`SubAgent.is_top_level` says which kind it is.

This package re-exports exactly one name from :mod:`.subagents` —
:class:`~kodo.agents.subagents.SubAgentSpec`, which
:meth:`AgentRegistry.spec_for` returns and so is part of *this* package's own
API. The rest of the sub-agent vocabulary (artifact roles and scopes,
``Need``, ``ALL_SUBAGENTS``) is imported from :mod:`.subagents` directly:
re-exporting it here would put one import away the very separation this
layout exists to draw.
"""

from ._install import (
    KIND_AGENT,
    KIND_SUBAGENT,
    UNVERSIONED,
    AgentInstallError,
    Candidate,
    GitNotAvailableError,
    InstallResult,
    SourceScan,
    install_source,
    require_git,
    scan_source,
)
from ._loader import ROLE_CRITIC, AgentLoadError, SubAgent, load_agent
from ._registry import (
    ALL_PHASES,
    PHASE_INITIAL,
    PHASE_REVISION,
    SHARED_FILE_PREFIX,
    SKILLS_TOKEN,
    SUBAGENT_SPECS_BY_NAME,
    SUBAGENTS_SUBDIR,
    AgentRegistry,
    phase_token,
    render_phase,
    shared_token,
)
from ._topagent import (
    TOP_AGENT_SUFFIX,
    TopAgent,
    TopAgentLoadError,
    load_top_agent,
    load_top_agents,
)
from ._userstore import (
    BUILTIN_NAME_PREFIX,
    SHARED_SUBAGENTS_DIRNAME,
    BrokenAgent,
    UserAgentDeleteError,
    UserAgents,
    UserAgentStore,
    reserved_name_error,
)
from .subagents import SubAgentSpec

__all__: list[str] = [
    "scan_source",
    "require_git",
    "install_source",
    "SourceScan",
    "InstallResult",
    "GitNotAvailableError",
    "Candidate",
    "AgentInstallError",
    "UNVERSIONED",
    "KIND_SUBAGENT",
    "KIND_AGENT",
    "ALL_PHASES",
    "BUILTIN_NAME_PREFIX",
    "PHASE_INITIAL",
    "PHASE_REVISION",
    "ROLE_CRITIC",
    "SHARED_FILE_PREFIX",
    "SHARED_SUBAGENTS_DIRNAME",
    "SKILLS_TOKEN",
    "SUBAGENTS_SUBDIR",
    "SUBAGENT_SPECS_BY_NAME",
    "TOP_AGENT_SUFFIX",
    "AgentLoadError",
    "AgentRegistry",
    "BrokenAgent",
    "SubAgent",
    "SubAgentSpec",
    "TopAgent",
    "TopAgentLoadError",
    "UserAgentDeleteError",
    "UserAgentStore",
    "UserAgents",
    "load_agent",
    "load_top_agent",
    "load_top_agents",
    "phase_token",
    "render_phase",
    "reserved_name_error",
    "shared_token",
]
