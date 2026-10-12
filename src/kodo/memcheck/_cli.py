"""``kodo-memcheck`` — measure how much memory a local-registry quant needs.

::

    kodo-memcheck ENTRY [--ctx-size N] [--rope-scale F] [--yarn-orig-ctx N]
                        [--cache-type-k T] [--cache-type-v T] [--timeout S]

Loads ``ENTRY`` (a local-registry entry name, which names LLM + quant) in
llama-server with the entry's *catalog-default* launch flags — its base args
plus each knob's catalog default, ignoring the user's stored knob picks and
profiles — with the command-line overrides layered on top. It reads the
buffers llama-server allocated from its log, stops it, and prints the total
to stdout as decimal GB with one decimal place, rounded up: the same unit as a
catalog entry's ``min_memory``/``memory``. Details go to stderr.

The total is the sum of every buffer on every device, which is the memory the
model needs only where devices share one memory pool: Apple Silicon, and
unified-memory APUs/NPUs. On a discrete GPU it still prints the sum, with a
warning that the figure is not meaningful there.
"""

from __future__ import annotations

import argparse
import math
import shlex
import sys
from pathlib import Path

from kodo.llms import (
    LocalLLMEntry,
    get_llama_server_override_path,
    get_local_registry,
    knob_selection_args,
)
from kodo.llms.llamacpp import find_installed, find_installed_model_path
from kodo.project import kodo_user_dir

from ._memory_log import LlamaMemoryLog, MemoryBreakdown
from ._probe import MemoryProbe, ProbeError

__all__ = ["main"]

_DEFAULT_TIMEOUT_SECONDS = 600.0
_BYTES_PER_MIB = 1024 * 1024
_BYTES_PER_GB = 1_000_000_000
# llama.cpp's accepted --cache-type-k/-v values.
_KV_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")


class _MemcheckError(RuntimeError):
    """The requested measurement cannot be made."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kodo-memcheck",
        description=(
            "Load a local-registry model in llama-server and print the memory it needs "
            "(decimal GB) to stdout. Meant for unified-memory machines."
        ),
    )
    parser.add_argument("model", help="Local-registry entry name (LLM + quant).")
    parser.add_argument(
        "-c",
        "--ctx-size",
        type=_non_negative_int,
        help="Context size in tokens (0 = the GGUF's trained length). "
        "Default: the entry's catalog default.",
    )
    parser.add_argument(
        "--rope-scale",
        type=_positive_float,
        help="YaRN context scaling factor; implies --rope-scaling yarn.",
    )
    parser.add_argument(
        "--yarn-orig-ctx",
        type=_non_negative_int,
        help="YaRN: the model's original context size; implies --rope-scaling yarn.",
    )
    parser.add_argument(
        "-ctk", "--cache-type-k", choices=_KV_TYPES, help="KV cache data type for K."
    )
    parser.add_argument(
        "-ctv", "--cache-type-v", choices=_KV_TYPES, help="KV cache data type for V."
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for the model to load (default {_DEFAULT_TIMEOUT_SECONDS:.0f}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the ``kodo-memcheck`` CLI.

    Args:
        argv (list[str] | None): Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code — ``0`` measured, ``1`` failure.
    """
    args = _parser().parse_args(argv)
    kodo_dir = kodo_user_dir()
    try:
        entry, model_path = _resolve_entry(kodo_dir, args.model)
        executable = _resolve_executable(kodo_dir)
    except _MemcheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    llama_args = _launch_args(entry, args)
    probe = MemoryProbe(executable, model_path, llama_args, timeout=args.timeout)
    print(f"model:   {entry.name}", file=sys.stderr)
    print(f"command: {shlex.join(probe.command)}", file=sys.stderr)
    try:
        memory_log = probe.run()
    except (ProbeError, TimeoutError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _report(memory_log)
    return 0


def _resolve_entry(kodo_dir: Path, model: str) -> tuple[LocalLLMEntry, Path]:
    entry = get_local_registry(kodo_dir).get(model)
    if entry is None:
        raise _MemcheckError(
            f"Unknown local model {model!r}. Use a local-registry entry name "
            "(the catalog file's name without .json)."
        )
    if entry.kind == "custom_server_url":
        raise _MemcheckError(
            f"{model!r} is a link to an external server ({entry.url}), not a model "
            "kodo-memcheck can load."
        )
    model_path = find_installed_model_path(entry, kodo_dir)
    if model_path is None:
        raise _MemcheckError(
            f"Model {model!r} is not installed. Install it from the Local Inference "
            "Settings panel first."
        )
    return entry, model_path


def _resolve_executable(kodo_dir: Path) -> Path:
    override = get_llama_server_override_path(kodo_dir)
    if override:
        return Path(override)
    install = find_installed(kodo_dir)
    if install is None:
        raise _MemcheckError("llama.cpp is not installed.")
    return install.executable


def _launch_args(entry: LocalLLMEntry, args: argparse.Namespace) -> dict[str, str]:
    """The entry's catalog-default flags with the command-line overrides on top."""
    llama_args = dict(entry.base_llama_args)
    llama_args.update(knob_selection_args(entry.knobs, {}, entry.knob_defaults))
    if args.ctx_size is not None:
        llama_args.pop("-c", None)
        llama_args["--ctx-size"] = str(args.ctx_size)
    if args.rope_scale is not None or args.yarn_orig_ctx is not None:
        llama_args["--rope-scaling"] = "yarn"
    if args.rope_scale is not None:
        llama_args["--rope-scale"] = str(args.rope_scale)
    if args.yarn_orig_ctx is not None:
        llama_args["--yarn-orig-ctx"] = str(args.yarn_orig_ctx)
    if args.cache_type_k is not None:
        llama_args["--cache-type-k"] = args.cache_type_k
    if args.cache_type_v is not None:
        llama_args["--cache-type-v"] = args.cache_type_v
    return llama_args


def _report(memory_log: LlamaMemoryLog) -> None:
    breakdown = memory_log.breakdown
    if memory_log.discrete_gpu:
        print(
            "warning: llama-server placed buffers on a GPU that does not declare unified "
            "memory. kodo-memcheck is meant for unified-memory machines (Apple Silicon, "
            "APUs/NPUs); on a discrete GPU the total below mixes VRAM and system RAM and "
            "is not a meaningful memory requirement.",
            file=sys.stderr,
        )
    _print_breakdown(breakdown)
    cache = memory_log.prompt_cache_mib
    if cache != 0:
        limit = "an unlimited amount" if cache < 0 else f"up to {cache} MiB"
        print(
            f"note: llama-server's prompt cache can additionally take {limit} of RAM at "
            "runtime (--cache-ram); not included above.",
            file=sys.stderr,
        )
    print(f"{_ceil_gb(breakdown.total):.1f}")


def _print_breakdown(breakdown: MemoryBreakdown) -> None:
    rows = (
        ("weights", breakdown.weights),
        ("kv/state", breakdown.state),
        ("compute", breakdown.compute),
        ("output", breakdown.output),
        ("total", breakdown.total),
    )
    for label, mib in rows:
        print(f"{label:<9}{mib:>12.1f} MiB", file=sys.stderr)


def _ceil_gb(mib: float) -> float:
    """*mib* in decimal GB, rounded up to one decimal place."""
    tenths = mib * _BYTES_PER_MIB / _BYTES_PER_GB * 10
    # The epsilon keeps an exact tenth (e.g. 12.0 GB) from rounding up to 12.1.
    return math.ceil(tenths - 1e-9) / 10


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {raw}")
    return value
