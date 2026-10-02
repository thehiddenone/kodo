"""Benchmark Kodo with Harbor — the ``kodo-harbor`` command (doc/HARBOR.md).

``kodo-harbor run`` turns "this Kodo agent, this model, these tasks" into one
Harbor job: it checks the model and its credential, starts the host
llama-server for a local model, writes a Harbor ``JobConfig``
(:class:`JobPlan`), runs a pinned Harbor through ``uv tool run``
(:class:`HarborInvocation`) with Docker sandboxes, and summarizes every arm —
Kodo and any control agent — from Harbor's results (:class:`JobSummary`).

Harbor is **not** imported here: it runs in its own ``uv`` environment, where
it loads the adapter subpackage ``kodo.harbor.agent`` (the only Kodo code that
imports ``harbor``). Nothing in this package imports that subpackage.
Imports: ``kodo.headless`` (model spelling, credentials), ``kodo.llms``
(registry checks), ``kodo.binutils`` and ``kodo.project``; the host
llama-server is driven as ``python -m kodo.llamaserver``.
"""

from ._cli import main
from ._errors import HarborRunError
from ._job import (
    CONTAINER_REGISTRY_PATH,
    KODO_AGENT_IMPORT_PATH,
    TERMINUS_2,
    JobPlan,
    KodoInstall,
    platform_overlay,
)
from ._llama import CONTAINER_HOST, HostLlama, LlamaAccess
from ._model import BenchModel, ListedModel
from ._runner import HARBOR_VERSION, HarborInvocation, KodoSource, check_docker, find_uv
from ._selection import Selection
from ._suites import BUILTIN_SUITES_DIR, Suite, SuiteCatalog
from ._summary import JobSummary, sign_test_p_value

__all__ = [
    "BUILTIN_SUITES_DIR",
    "CONTAINER_HOST",
    "CONTAINER_REGISTRY_PATH",
    "HARBOR_VERSION",
    "KODO_AGENT_IMPORT_PATH",
    "TERMINUS_2",
    "BenchModel",
    "HarborInvocation",
    "HarborRunError",
    "HostLlama",
    "JobPlan",
    "JobSummary",
    "KodoInstall",
    "KodoSource",
    "ListedModel",
    "LlamaAccess",
    "Selection",
    "Suite",
    "SuiteCatalog",
    "check_docker",
    "find_uv",
    "main",
    "platform_overlay",
    "sign_test_p_value",
]
