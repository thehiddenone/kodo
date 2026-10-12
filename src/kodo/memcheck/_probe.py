"""Launch llama-server just long enough to read what it allocated.

The probe starts llama-server on a free loopback port with ``-lv 4`` (the
verbosity that prints buffer sizes) and ``--fit off`` (so llama.cpp never
silently shrinks the requested context to fit), feeds its output to a
:class:`LlamaMemoryLog` as it arrives, and stops the process as soon as the
model is loaded. No request is ever sent: every buffer the measurement needs
is allocated before llama-server starts listening.
"""

from __future__ import annotations

import re
import socket
import subprocess
import threading
from collections import deque
from pathlib import Path

from ._memory_log import LlamaMemoryLog

__all__ = ["MemoryProbe", "ProbeError"]

_STOP_GRACE_SECONDS = 10.0
_PUMP_JOIN_SECONDS = 5.0
_TAIL_LINES = 40
_BYTES_PER_MIB = 1024 * 1024
_SHARD_RE = re.compile(r"^(?P<stem>.+)-\d{5}-of-(?P<count>\d{5})\.gguf$")
_DRAFT_MODEL_FLAGS = ("--model-draft", "-md", "--spec-draft-model")

#: Flags the probe itself adds after the model's own launch flags (so they win).
PROBE_ARGS: dict[str, str] = {"--fit": "off"}

#: Verbosity 4 prints the buffer sizes. It goes *first*: llama-server applies
#: flags in order, and a device-touching flag such as ``--n-gpu-layers``
#: initializes the backends mid-parse — a later ``-lv`` would come too late
#: for the backend's own init lines (Metal's "has unified memory").
_VERBOSITY_ARGS = ("-lv", "4")


class ProbeError(RuntimeError):
    """llama-server did not finish loading the model."""


class MemoryProbe:
    """One measuring launch of llama-server."""

    __executable: Path
    __model_path: Path
    __llama_args: dict[str, str]
    __timeout: float

    def __init__(
        self,
        executable: Path,
        model_path: Path,
        llama_args: dict[str, str],
        *,
        timeout: float,
    ) -> None:
        """Bind what to launch; nothing starts until :meth:`run`.

        Args:
            executable (Path): The ``llama-server`` binary.
            model_path (Path): The GGUF to load (first shard of a split GGUF).
            llama_args (dict[str, str]): The model's launch flags, verbatim
                ``{flag: value}`` pairs (``""`` for a bare flag).
            timeout (float): Seconds to wait for the model to load.
        """
        self.__executable = executable
        self.__model_path = model_path
        self.__llama_args = dict(llama_args)
        self.__timeout = timeout

    @property
    def command(self) -> list[str]:
        """The argv the probe launches (port excluded; it is picked at run time)."""
        cmd = [str(self.__executable), *_VERBOSITY_ARGS, "--model", str(self.__model_path)]
        for flag, value in {**self.__llama_args, **PROBE_ARGS}.items():
            cmd.append(flag)
            if value:
                cmd.append(value)
        return cmd

    def run(self) -> LlamaMemoryLog:
        """Launch llama-server, read its log until the model is loaded, stop it.

        Returns:
            LlamaMemoryLog: The parsed log of a successful load.

        Raises:
            ProbeError: llama-server exited before loading the model (the
                message carries the tail of its output).
            TimeoutError: The model did not load within the timeout.
            OSError: llama-server could not be started.
        """
        cmd = [*self.command, "--host", "127.0.0.1", "--port", str(_free_port())]
        memory_log = LlamaMemoryLog(mapped_limit_mib=self.__gguf_mib())
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        settled = threading.Event()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        def pump() -> None:
            # Keeps draining after the load so llama-server never blocks on a
            # full pipe while it is being stopped.
            assert proc.stdout is not None
            for line in proc.stdout:
                tail.append(line.rstrip())
                if not memory_log.loaded:
                    memory_log.feed(line)
                    if memory_log.loaded:
                        settled.set()
            settled.set()

        reader = threading.Thread(target=pump, name="memcheck-log", daemon=True)
        reader.start()
        try:
            in_time = settled.wait(self.__timeout)
        finally:
            _stop(proc)
            reader.join(_PUMP_JOIN_SECONDS)

        if memory_log.loaded:
            return memory_log
        if not in_time:
            raise TimeoutError(f"the model did not load within {self.__timeout:.0f} s")
        output = "\n".join(tail)
        raise ProbeError(
            f"llama-server exited (code {proc.returncode}) before loading the model:\n{output}"
        )

    def __gguf_mib(self) -> float:
        """Total size of every GGUF file the launch loads, in MiB."""
        files = _gguf_files(self.__model_path)
        for flag in _DRAFT_MODEL_FLAGS:
            draft = self.__llama_args.get(flag)
            if draft:
                files.extend(_gguf_files(Path(draft)))
        return sum(path.stat().st_size for path in files if path.is_file()) / _BYTES_PER_MIB


def _gguf_files(path: Path) -> list[Path]:
    """*path*, or every shard of the split GGUF whose first shard *path* is."""
    match = _SHARD_RE.match(path.name)
    if match is None:
        return [path]
    count = int(match.group("count"))
    stem = match.group("stem")
    return [
        path.with_name(f"{stem}-{index:05d}-of-{count:05d}.gguf") for index in range(1, count + 1)
    ]


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
