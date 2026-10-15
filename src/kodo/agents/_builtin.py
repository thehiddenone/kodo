"""Facts about the **built-in** agents that more than one module needs.

Two of them:

- where the packaged agents live (:data:`PACKAGED_AGENTS_DIR`) — the directory
  the server and the CLI build the registry over, and the one a copy reads its
  sources from;
- which built-in sub-agents the **engine** drives by name
  (:data:`ENGINE_DRIVEN_AGENTS`), outside any ``subagents:`` allow-list. The
  runtime binds its compactor, web-search and dependency-manager calls to these
  exact names, so they are defined here, once, and the runtime imports them.
  Agent Builder needs the same set for two reasons: a renamed copy of one would
  never be called, so it cannot be copied (doc/AGENT_BUILDER.md §9), and its
  prompt is exempt from the required-sections lint rule (§5.2, D26).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "COMPACTOR_AGENT",
    "DEPSMGR_AGENT",
    "ENGINE_DRIVEN_AGENTS",
    "PACKAGED_AGENTS_DIR",
    "WEB_SEARCH_AGENT",
]

#: The directory holding the packaged agents: ``agent_*.md`` + ``<name>.json``
#: and ``shared_*.md`` at its root, sub-agents under ``subagents/``.
PACKAGED_AGENTS_DIR = Path(__file__).parent

#: Condenses a long session's transcript; spawned by the engine's compaction.
COMPACTOR_AGENT = "kodo_compactor"

#: Behind the ``web_search`` tool; driven as a silent multi-round turn.
WEB_SEARCH_AGENT = "kodo_web_search"

#: Behind the ``toolchain_deps`` tool; spawned through its ungated service.
DEPSMGR_AGENT = "kodo_toolchain_depsmgr"

#: Every sub-agent the engine calls by name rather than through a caller's
#: ``subagents:`` allow-list.
ENGINE_DRIVEN_AGENTS: frozenset[str] = frozenset({COMPACTOR_AGENT, WEB_SEARCH_AGENT, DEPSMGR_AGENT})
