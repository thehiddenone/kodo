"""Standalone llama-server management — the ``kodo-llama-server`` command.

Serves one local-registry model on a chosen port, outside any kodo server,
with exactly the launch flags the kodo server itself would use for it. Its
state lives in ``~/.kodo/llama.cpp/standalone/<port>.json``, never in the kodo
server's own runtime file, so no kodo server ever adopts or stops it. Built
for ``kodo-headless`` (and Harbor trials) that attach to a host-side model by
URL — see doc/HEADLESS.md.

Imports ``kodo.llms`` and ``kodo.project`` only; nothing else in kodo imports
this package (``kodo-headless`` runs it as a subprocess, by module name).
"""

from ._cli import main
from ._state import StandaloneState
from ._supervisor import StandaloneError, Supervisor, resolve_standalone_entry

__all__ = [
    "StandaloneError",
    "StandaloneState",
    "Supervisor",
    "main",
    "resolve_standalone_entry",
]
