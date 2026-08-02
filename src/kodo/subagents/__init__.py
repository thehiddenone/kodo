"""Kōdo subagent registry — markdown subagent files and (name, model) lookup.

Stub for M1; full implementation in M3.
"""

from ._loader import AgentLoadError, SubAgent, load_agent
from ._registry import SHARED_FILE_PREFIX, AgentRegistry, shared_token
from ._subagentspec import SubAgentSpec

__all__: list[str] = [
    "SHARED_FILE_PREFIX",
    "AgentLoadError",
    "AgentRegistry",
    "SubAgent",
    "SubAgentSpec",
    "load_agent",
    "shared_token",
]
