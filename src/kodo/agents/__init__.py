"""Kōdo's agents — the prompts, the contracts that go with them, and the registry.

Two kinds of agent live here, and the package is laid out along that split:

**Top-level agents** (``agent_<name>.md`` plus a ``<name>.json`` config beside
it) are what a *human* selects and talks to — ``guide``, ``problem_solver``. They
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

from ._builtin import (
    COMPACTOR_AGENT,
    DEPSMGR_AGENT,
    ENGINE_DRIVEN_AGENTS,
    PACKAGED_AGENTS_DIR,
    WEB_SEARCH_AGENT,
)
from ._copy import (
    AgentCopyError,
    CopyMember,
    CopyPlan,
    CopyResult,
    CopySource,
    copy_agent,
    copy_sources,
    plan_copy,
)
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
from ._lint import (
    LINT_RULES,
    RULE_PURPOSE,
    RULE_SCHEMA_RESTATED,
    RULE_SECTIONS,
    RULE_SUBAGENT_NOT_LISTED,
    RULE_TOOL_NOT_GRANTED,
    RULE_UNKNOWN_AGENT,
    RULE_VAGUE_QUALIFIER,
    SEVERITY_WARNING,
    LintContext,
    LintFinding,
    lint_prompt,
)
from ._loader import (
    FRONTMATTER_KEYS,
    ROLE_CRITIC,
    AgentLoadError,
    SubAgent,
    load_agent,
    split_frontmatter,
)
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
    TOP_AGENT_KEYS,
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
from ._validate import AgentReport, AgentValidator
from .subagents import SubAgentSpec

__all__: list[str] = [
    "AgentCopyError",
    "AgentReport",
    "AgentValidator",
    "COMPACTOR_AGENT",
    "CopyMember",
    "CopyPlan",
    "CopyResult",
    "CopySource",
    "DEPSMGR_AGENT",
    "ENGINE_DRIVEN_AGENTS",
    "FRONTMATTER_KEYS",
    "LINT_RULES",
    "LintContext",
    "LintFinding",
    "PACKAGED_AGENTS_DIR",
    "RULE_PURPOSE",
    "RULE_SCHEMA_RESTATED",
    "RULE_SECTIONS",
    "RULE_SUBAGENT_NOT_LISTED",
    "RULE_TOOL_NOT_GRANTED",
    "RULE_UNKNOWN_AGENT",
    "RULE_VAGUE_QUALIFIER",
    "SEVERITY_WARNING",
    "WEB_SEARCH_AGENT",
    "copy_agent",
    "copy_sources",
    "lint_prompt",
    "plan_copy",
    "split_frontmatter",
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
    "TOP_AGENT_KEYS",
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
