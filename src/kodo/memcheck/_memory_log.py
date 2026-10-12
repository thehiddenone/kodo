"""Parse a llama-server startup log into the memory it allocated.

llama.cpp allocates everything a loaded model needs at startup: the weights
(``load_tensors: … model buffer size``), each context's KV cache and
recurrent state (``KV`` / ``RS buffer size``) plus its output buffer, and each
context's compute scratch (``compute buffer size``, reserved for the
worst-case batch). Those lines are printed at verbosity 4 (``-lv 4``).

The ``common_memory_breakdown_print`` table llama-server prints on shutdown is
deliberately *not* used: it covers only the main context, so an MTP draft
context's KV and compute buffers land in its "unaccounted" column — which also
absorbs every other process's allocations on the same device.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LlamaMemoryLog", "MemoryBreakdown"]

_MIB = r"([0-9]+(?:\.[0-9]+)?) MiB"
_MODEL_RE = re.compile(r"(\S+)\s+model buffer size\s*=\s*" + _MIB)
_STATE_RE = re.compile(r"(\S+)\s+(?:KV|RS) buffer size\s*=\s*" + _MIB)
_OUTPUT_RE = re.compile(r"(\S+)\s+output buffer size\s*=\s*" + _MIB)
_COMPUTE_RE = re.compile(r"(\w+):\s+(\S+)\s+compute buffer size\s*=\s*" + _MIB)
_CONTEXT_RE = re.compile(r"constructing llama_context")
_LOADED_RE = re.compile(r"\bmodel loaded\b|\blistening on\b")
# Metal: "has unified memory = true"; Vulkan: "… | uma: 1 | …".
_UNIFIED_RE = re.compile(r"has unified memory\s*=\s*true|\buma:\s*1\b")
_PROMPT_CACHE_RE = re.compile(r"prompt cache is enabled, size limit:\s*(-?[0-9]+) MiB")


@dataclass(frozen=True)
class MemoryBreakdown:
    """What one llama-server launch allocated, in MiB.

    Attributes:
        weights: Model weight buffers (main model and any draft model).
        state: KV cache and recurrent-state buffers of every context.
        compute: Compute scratch buffers of every context.
        output: Logits/embeddings output buffers of every context.
    """

    weights: float
    state: float
    compute: float
    output: float

    @property
    def total(self) -> float:
        """Everything above, summed (MiB)."""
        return self.weights + self.state + self.compute + self.output


class LlamaMemoryLog:
    """Accumulates the memory facts of a llama-server log, one line at a time."""

    __weights: float
    __state: float
    __output: float
    __compute: dict[tuple[int, str, str], float]
    __context_index: int
    __device_buffers: bool
    __unified: bool
    __prompt_cache_mib: int
    __loaded: bool

    def __init__(self) -> None:
        """Start with nothing seen."""
        self.__weights = 0.0
        self.__state = 0.0
        self.__output = 0.0
        self.__compute = {}
        self.__context_index = 0
        self.__device_buffers = False
        self.__unified = False
        self.__prompt_cache_mib = 0
        self.__loaded = False

    @property
    def loaded(self) -> bool:
        """Whether llama-server reported the model loaded (all allocations done)."""
        return self.__loaded

    @property
    def breakdown(self) -> MemoryBreakdown:
        """The allocations seen so far."""
        return MemoryBreakdown(
            weights=self.__weights,
            state=self.__state,
            compute=sum(self.__compute.values()),
            output=self.__output,
        )

    @property
    def discrete_gpu(self) -> bool:
        """Whether buffers went to a GPU that did not declare unified memory.

        Only Metal and Vulkan declare unified memory in the log, so a CUDA,
        ROCm or SYCL device always counts as discrete here.
        """
        return self.__device_buffers and not self.__unified

    @property
    def prompt_cache_mib(self) -> int:
        """The host-RAM prompt cache limit (``--cache-ram``) or ``0`` if disabled.

        ``-1`` means unlimited. Not allocated at load; it grows at runtime.
        """
        return self.__prompt_cache_mib

    def feed(self, line: str) -> None:
        """Account for one log line.

        Args:
            line (str): A line of llama-server output, with or without its
                timestamp/level prefix.
        """
        if _CONTEXT_RE.search(line):
            self.__context_index += 1
            return
        if _UNIFIED_RE.search(line):
            self.__unified = True
            return
        if _LOADED_RE.search(line):
            self.__loaded = True
            return
        if match := _PROMPT_CACHE_RE.search(line):
            self.__prompt_cache_mib = int(match.group(1))
            return
        if match := _COMPUTE_RE.search(line):
            # A context can be re-reserved (llama-server does so for the draft
            # context once speculative decoding is wired up); a later
            # reservation replaces the earlier one rather than adding to it.
            function, backend, size = match.group(1), match.group(2), float(match.group(3))
            self.__compute[(self.__context_index, function, backend)] = size
            self.__note_backend(backend)
            return
        for pattern in (_MODEL_RE, _STATE_RE, _OUTPUT_RE):
            if match := pattern.search(line):
                self.__add(pattern, match.group(1), float(match.group(2)))
                return

    def __add(self, pattern: re.Pattern[str], backend: str, size: float) -> None:
        if pattern is _MODEL_RE:
            self.__weights += size
        elif pattern is _STATE_RE:
            self.__state += size
        else:
            self.__output += size
        self.__note_backend(backend)

    def __note_backend(self, backend: str) -> None:
        # "CPU", "CPU_Mapped", "CPU_REPACK", "CUDA_Host" (pinned host RAM) are
        # all system memory; anything else is a device's own memory.
        if not (backend.startswith("CPU") or backend.endswith("_Host")):
            self.__device_buffers = True
