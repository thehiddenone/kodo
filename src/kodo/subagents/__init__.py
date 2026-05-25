"""Kōdo subagent registry — markdown subagent files and (name, model) lookup.

Stub for M1; full implementation in M3.
"""

from ._loader import AgentLoadError, SubAgent, load_agent
from ._registry import AgentRegistry

__all__: list[str] = ["AgentLoadError", "AgentRegistry", "SubAgent", "load_agent"]
