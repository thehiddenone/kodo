"""Kodo as a Harbor agent — load with ``--agent-import-path kodo.harbor.agent:KodoAgent``.

This subpackage is the one place in Kodo that imports ``harbor``, and only
Harbor's own process ever imports it: ``kodo-harbor`` runs Harbor through
``uv tool run`` with ``py-kodo`` alongside, and nothing else in Kodo imports
this package (doc/HARBOR.md). It imports ``harbor`` and ``kodo.headless``
only; the task container never sees it — there, Kodo is just
``kodo-headless``.
"""

from ._agent import KODO_AGENT_NAME, KodoAgent
from ._context import KODO_JSONL, KODO_RESULT_JSON, KodoRunRecord
from ._options import DEFAULT_TOP_AGENT, KodoAgentOptions
from ._trajectory import ATIF_SCHEMA_VERSION, session_to_trajectory

__all__ = [
    "ATIF_SCHEMA_VERSION",
    "DEFAULT_TOP_AGENT",
    "KODO_AGENT_NAME",
    "KODO_JSONL",
    "KODO_RESULT_JSON",
    "KodoAgent",
    "KodoAgentOptions",
    "KodoRunRecord",
    "session_to_trajectory",
]
