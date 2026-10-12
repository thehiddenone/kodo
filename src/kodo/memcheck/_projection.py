"""Project a launch's memory with ``llama-fit-params``, without loading it.

``llama-fit-params`` ships next to ``llama-server`` in every llama.cpp build.
It builds the model and context with no allocation (it reads only the GGUF
metadata, never the weights), prints what the given parameters would take
per device, and then searches for parameters that fit the free device memory.
Only that first table is the projection kodo-memcheck wants; it is
read by :class:`LlamaMemoryLog`.

It accepts llama.cpp's common flags but rejects server-only ones
(``--jinja``, ``--reasoning-format``, ``--alias``, ``--cache-ram``, …) and the
speculative-decoding ones (``--spec-type``, ``--model-draft``). Each rejected
flag is dropped and the run retried, and the dropped flags are reported, so
the caller can warn when one of them mattered (an MTP draft context is then
not part of the projection).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ._memory_log import LlamaMemoryLog, MemoryBreakdown

__all__ = ["FitProjection", "ProjectionError", "find_fit_params"]

_INVALID_ARG_RE = re.compile(r"invalid argument:\s*(\S+)")
_TAIL_CHARS = 4000
_VERBOSITY_ARGS = ("-lv", "4")


class ProjectionError(RuntimeError):
    """``llama-fit-params`` did not produce a projection."""


def find_fit_params(server_executable: Path) -> Path | None:
    """The ``llama-fit-params`` binary of the build *server_executable* belongs to.

    Args:
        server_executable (Path): The ``llama-server`` binary.

    Returns:
        Path | None: The sibling ``llama-fit-params``, or ``None`` if the
        build has none.
    """
    name = "llama-fit-params.exe" if sys.platform == "win32" else "llama-fit-params"
    path = server_executable.with_name(name)
    return path if path.is_file() else None


class FitProjection:
    """One ``llama-fit-params`` run, retried without each flag it rejects."""

    __executable: Path
    __model_path: Path
    __llama_args: dict[str, str]
    __timeout: float
    __dropped: list[str]
    __memory_log: LlamaMemoryLog | None

    def __init__(
        self,
        executable: Path,
        model_path: Path,
        llama_args: dict[str, str],
        *,
        timeout: float,
    ) -> None:
        """Bind what to project; nothing runs until :meth:`run`.

        Args:
            executable (Path): The ``llama-fit-params`` binary.
            model_path (Path): The GGUF (first shard of a split GGUF).
            llama_args (dict[str, str]): The launch flags, verbatim
                ``{flag: value}`` pairs (``""`` for a bare flag).
            timeout (float): Seconds to wait for each run.
        """
        self.__executable = executable
        self.__model_path = model_path
        self.__llama_args = dict(llama_args)
        self.__timeout = timeout
        self.__dropped = []
        self.__memory_log = None

    @property
    def dropped_flags(self) -> tuple[str, ...]:
        """Flags ``llama-fit-params`` rejected and the projection was made without."""
        return tuple(self.__dropped)

    @property
    def memory_log(self) -> LlamaMemoryLog | None:
        """The parsed output of the successful run, once :meth:`run` returned."""
        return self.__memory_log

    def run(self) -> MemoryBreakdown:
        """Run ``llama-fit-params`` until it accepts every remaining flag.

        Returns:
            MemoryBreakdown: The projection for the parameters as given.

        Raises:
            ProjectionError: It failed for another reason than a rejected
                flag, or printed no breakdown table.
            TimeoutError: A run did not finish within the timeout.
            OSError: ``llama-fit-params`` could not be started.
        """
        while True:
            output = self.__run_once()
            rejected = _INVALID_ARG_RE.search(output)
            if rejected is not None and rejected.group(1) in self.__llama_args:
                flag = rejected.group(1)
                del self.__llama_args[flag]
                self.__dropped.append(flag)
                continue
            memory_log = LlamaMemoryLog()
            for line in output.splitlines():
                memory_log.feed(line)
            projection = memory_log.projection
            if projection is None:
                raise ProjectionError(
                    f"llama-fit-params printed no memory projection:\n{output[-_TAIL_CHARS:]}"
                )
            self.__memory_log = memory_log
            return projection

    def __run_once(self) -> str:
        cmd = [str(self.__executable), *_VERBOSITY_ARGS, "--model", str(self.__model_path)]
        for flag, value in self.__llama_args.items():
            cmd.append(flag)
            if value:
                cmd.append(value)
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.__timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"llama-fit-params did not finish within {self.__timeout:.0f} s"
            ) from exc
        return result.stdout
