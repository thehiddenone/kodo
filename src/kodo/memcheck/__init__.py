"""Empirical memory measurement — the ``kodo-memcheck`` command.

Loads one local-registry quant in llama-server with its catalog-default
launch flags (plus command-line overrides for context size, YaRN and KV cache
type), sums the buffers llama-server reports allocating, and prints the total
in decimal GB — the unit of a catalog entry's ``min_memory``/``memory``. Meant
for unified-memory machines (Apple Silicon, APUs/NPUs), where that sum is
exactly what the model needs; see doc/MEMCHECK.md.

Imports ``kodo.llms`` and ``kodo.project`` only; nothing else in kodo imports
this package.
"""

from ._cli import main
from ._memory_log import LlamaMemoryLog, MemoryBreakdown
from ._probe import PROBE_ARGS, MemoryProbe, ProbeError

__all__ = [
    "PROBE_ARGS",
    "LlamaMemoryLog",
    "MemoryBreakdown",
    "MemoryProbe",
    "ProbeError",
    "main",
]
