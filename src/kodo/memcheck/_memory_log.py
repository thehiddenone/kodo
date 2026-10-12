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

Weights loaded with mmap (``*_Mapped`` buffers) are views into the GGUF
file's pages, and two of them can overlap: on Apple Silicon a model's Metal
buffers can span the whole file *including* the token embedding that is also
reported as the ``CPU_Mapped`` buffer (K2 Horizon BF16: 2447.5 + 66337.5 MiB
of mapped buffers over a 66348.5 MiB file). Those pages are resident once, so
the mapped buffers are counted as at most the loaded GGUF files' total size.

The same parser reads ``llama-fit-params`` output, which allocates nothing:
the first ``common_memory_breakdown_print`` table it prints is its projection
for the parameters as given (later tables are its own attempts at moving
layers off the GPU), exposed as :attr:`LlamaMemoryLog.projection`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LlamaMemoryLog", "MemoryBreakdown"]

_MIB = r"([0-9]+(?:\.[0-9]+)?) MiB"
_MODEL_RE = re.compile(r"(\S+)\s+model buffer size\s*=\s*" + _MIB)
_MAPPED_SUFFIX = "_Mapped"
_STATE_RE = re.compile(r"(\S+)\s+(?:KV|RS) buffer size\s*=\s*" + _MIB)
_OUTPUT_RE = re.compile(r"(\S+)\s+output buffer size\s*=\s*" + _MIB)
_COMPUTE_RE = re.compile(r"(\w+):\s+(\S+)\s+compute buffer size\s*=\s*" + _MIB)
_CONTEXT_RE = re.compile(r"constructing llama_context")
_LOADED_RE = re.compile(r"\bmodel loaded\b|\blistening on\b")
# Metal: "has unified memory = true"; Vulkan: "… | uma: 1 | …".
_UNIFIED_RE = re.compile(r"has unified memory\s*=\s*true|\buma:\s*1\b")
_PROMPT_CACHE_RE = re.compile(r"prompt cache is enabled, size limit:\s*(-?[0-9]+) MiB")
_TABLE_HEADER_RE = re.compile(r"memory breakdown \[MiB\]")
# "|   - MTL0 (Apple M5 Max) | 110100 = 110099 + (130822 = 63890 +   65536 +    1396) + …"
_DEVICE_ROW_RE = re.compile(
    r"\|\s+-\s+.+?\|\s*-?\d+\s*=\s*-?\d+\s*\+\s*\(\s*-?\d+\s*="
    r"\s*(\d+)\s*\+\s*(\d+)\s*\+\s*(\d+)\s*\)"
)
# "|   - Host                |                      3039 =  2447 +       0 +     592   |"
_HOST_ROW_RE = re.compile(r"\|\s+-\s+Host\s+\|\s*-?\d+\s*=\s*(\d+)\s*\+\s*(\d+)\s*\+\s*(\d+)\s*\|")


@dataclass(frozen=True)
class MemoryBreakdown:
    """What one llama-server launch allocated, in MiB.

    Attributes:
        weights: Model weight buffers (main model and any draft model), with
            overlapping mmap'd buffers counted once.
        state: KV cache and recurrent-state buffers of every context.
        compute: Compute scratch buffers of every context.
        output: Logits/embeddings output buffers of every context.
        mapped_overlap: How much the mmap'd weight buffers added up to beyond
            the GGUF files' size — already left out of *weights*.
    """

    weights: float
    state: float
    compute: float
    output: float
    mapped_overlap: float = 0.0

    @property
    def total(self) -> float:
        """Everything above, summed (MiB)."""
        return self.weights + self.state + self.compute + self.output


class LlamaMemoryLog:
    """Accumulates the memory facts of a llama-server log, one line at a time."""

    __mapped_limit: float | None
    __mapped_weights: float
    __weights: float
    __state: float
    __output: float
    __compute: dict[tuple[int, str, str], float]
    __context_index: int
    __device_buffers: bool
    __unified: bool
    __prompt_cache_mib: int
    __loaded: bool
    __table_state: int
    __projection: list[tuple[int, int, int]]

    def __init__(self, mapped_limit_mib: float | None = None) -> None:
        """Start with nothing seen.

        Args:
            mapped_limit_mib (float | None): The most the mmap'd
                (``*_Mapped``) weight buffers can occupy — the total size of
                the GGUF files being loaded, in MiB. ``None`` means no cap.
        """
        self.__mapped_limit = mapped_limit_mib
        self.__mapped_weights = 0.0
        self.__weights = 0.0
        self.__state = 0.0
        self.__output = 0.0
        self.__compute = {}
        self.__context_index = 0
        self.__device_buffers = False
        self.__unified = False
        self.__prompt_cache_mib = 0
        self.__loaded = False
        # 0: no table yet, 1: inside the first table, 2: past it.
        self.__table_state = 0
        self.__projection = []

    @property
    def loaded(self) -> bool:
        """Whether llama-server reported the model loaded (all allocations done)."""
        return self.__loaded

    @property
    def breakdown(self) -> MemoryBreakdown:
        """The allocations seen so far."""
        mapped = self.__mapped_weights
        if self.__mapped_limit is not None:
            mapped = min(mapped, self.__mapped_limit)
        return MemoryBreakdown(
            weights=self.__weights + mapped,
            state=self.__state,
            compute=sum(self.__compute.values()),
            output=self.__output,
            mapped_overlap=self.__mapped_weights - mapped,
        )

    @property
    def projection(self) -> MemoryBreakdown | None:
        """The first breakdown table's totals (``llama-fit-params``' projection).

        ``None`` until a table with at least one row has been read. The
        table's ``context`` column (KV cache and recurrent state) is reported
        as *state*; it has no separate output column.
        """
        if not self.__projection:
            return None
        return MemoryBreakdown(
            weights=float(sum(row[0] for row in self.__projection)),
            state=float(sum(row[1] for row in self.__projection)),
            compute=float(sum(row[2] for row in self.__projection)),
            output=0.0,
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
        if self.__table_state < 2 and self.__feed_table(line):
            return
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

    def __feed_table(self, line: str) -> bool:
        """Read the first breakdown table; ``True`` when *line* belonged to it."""
        if self.__table_state == 0:
            if _TABLE_HEADER_RE.search(line):
                self.__table_state = 1
                return True
            return False
        if match := _HOST_ROW_RE.search(line):
            self.__projection.append(_row(match))
            return True
        if match := _DEVICE_ROW_RE.search(line):
            row = _row(match)
            self.__projection.append(row)
            if sum(row) > 0:
                self.__device_buffers = True
            return True
        self.__table_state = 2
        return False

    def __add(self, pattern: re.Pattern[str], backend: str, size: float) -> None:
        if pattern is _MODEL_RE and backend.endswith(_MAPPED_SUFFIX):
            self.__mapped_weights += size
        elif pattern is _MODEL_RE:
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


def _row(match: re.Match[str]) -> tuple[int, int, int]:
    return int(match.group(1)), int(match.group(2)), int(match.group(3))
