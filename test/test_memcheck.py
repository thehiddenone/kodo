"""``kodo-memcheck`` — empirical memory measurement of a local quant (doc/MEMCHECK.md).

The log parser is fed excerpts of real llama-server b11552 logs (``-lv 4``).
The CLI runs end to end against a fake "llama-server" executable that records
its argv, prints a canned log, and then waits to be stopped — the same
throwaway-``HOME`` approach as ``test_llamaserver_standalone.py``: llama.cpp
"installed" via ``llama-meta.json`` and one ``custom_file`` registry entry.
The CLI's canned logs keep their mmap'd weights at a few MiB, since those are
capped at the size of the (real, on-disk) fake GGUF files.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from kodo.memcheck import FitProjection, LlamaMemoryLog, main

# Apple M5 Max, Ornith-1.5-35B-A3B with built-in MTP drafting at 128K: a main
# context (KV + recurrent state) and an MTP draft context, whose compute
# buffer is reserved a second time once speculative decoding is wired up.
_METAL_MTP_LOG = """\
0.00.041.211 I ggml_metal_device_init: has unified memory    = true
0.00.223.422 I load_tensors: loading model tensors, this can take a while... (load_mode = mmap)
0.12.236.941 I load_tensors:   CPU_Mapped model buffer size =   970.00 MiB
0.12.236.942 I load_tensors:  MTL0_Mapped model buffer size = 36931.32 MiB
0.12.236.942 I load_tensors:  MTL0_Mapped model buffer size = 29862.97 MiB
0.12.239.843 I llama_context: constructing llama_context
0.12.239.847 I llama_context: kv_unified            = true
0.12.240.161 I llama_context:        CPU  output buffer size =     3.79 MiB
0.12.271.339 I llama_kv_cache:       MTL0 KV buffer size =  1360.00 MiB
0.12.311.532 I llama_kv_cache: size = 1360.00 MiB (131072 cells,  10 layers,  4/1 seqs)
0.12.358.518 I llama_memory_recurrent:       MTL0 RS buffer size =  1005.00 MiB
0.12.379.168 I sched_reserve:       MTL0 compute buffer size =   456.72 MiB
0.12.379.174 I sched_reserve:        CPU compute buffer size =   144.29 MiB
0.12.411.852 I llama_context: constructing llama_context
0.12.412.074 I llama_context:        CPU  output buffer size =     3.79 MiB
0.12.417.952 I llama_kv_cache:       MTL0 KV buffer size =   256.00 MiB
0.12.434.902 I sched_reserve:       MTL0 compute buffer size =   444.58 MiB
0.12.434.905 I sched_reserve:        CPU compute buffer size =   136.02 MiB
0.12.434.985 I srv    load_model: initializing, n_slots = 4, n_ctx_slot = 131072
0.12.446.880 I sched_reserve:       MTL0 compute buffer size =   444.58 MiB
0.12.446.883 I sched_reserve:        CPU compute buffer size =   136.02 MiB
0.12.452.115 I srv    load_model: prompt cache is enabled, size limit: 8192 MiB
0.12.455.255 I srv  llama_server: model loaded
0.12.455.256 I srv  llama_server: listening on http://127.0.0.1:18766
"""

_METAL_MTP_WEIGHTS = 970.00 + 36931.32 + 29862.97
_METAL_MTP_STATE = 1360.00 + 1005.00 + 256.00
_METAL_MTP_COMPUTE = 456.72 + 144.29 + 444.58 + 136.02
_METAL_MTP_OUTPUT = 3.79 + 3.79

# K2-Horizon-32B BF16 on Metal: the two MTL0_Mapped buffers span the whole
# 66348.5 MiB file, so the CPU_Mapped token embedding overlaps them.
_K2_HORIZON_WEIGHTS_LOG = """\
0.06.326.644 I load_tensors:   CPU_Mapped model buffer size =  2447.50 MiB
0.06.326.646 I load_tensors:  MTL0_Mapped model buffer size = 37961.54 MiB
0.06.326.646 I load_tensors:  MTL0_Mapped model buffer size = 28375.98 MiB
"""
_K2_HORIZON_GGUF_MIB = 66348.5

_CUDA_LOG = """\
ggml_cuda_init: found 1 CUDA devices:
  Device 0: NVIDIA GeForce RTX 4090, compute capability 8.9, VMM: yes
load_tensors:        CUDA0 model buffer size =  7953.53 MiB
load_tensors:   CPU_Mapped model buffer size =   973.50 MiB
llama_context: constructing llama_context
llama_context:  CUDA_Host  output buffer size =     2.54 MiB
llama_kv_cache:      CUDA0 KV buffer size =  2992.00 MiB
sched_reserve:      CUDA0 compute buffer size =   232.81 MiB
sched_reserve:  CUDA_Host compute buffer size =    56.10 MiB
srv  llama_server: model loaded
"""

_VULKAN_APU_LOG = """\
ggml_vulkan: 0 = AMD Radeon 8060S Graphics (RADV GFX1151) (radv) | uma: 1 | fp16: 1 | bf16: 0
load_tensors:      Vulkan0 model buffer size =  7953.53 MiB
srv  llama_server: model loaded
"""

_CPU_ONLY_LOG = """\
load_tensors:          CPU model buffer size =  7953.53 MiB
llama_kv_cache:        CPU KV buffer size =  2992.00 MiB
srv  llama_server: model loaded
"""


def _parse(text: str, mapped_limit_mib: float | None = None) -> LlamaMemoryLog:
    log = LlamaMemoryLog(mapped_limit_mib)
    for line in text.splitlines():
        log.feed(line)
    return log


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------


def test_sums_every_context_including_the_mtp_draft() -> None:
    breakdown = _parse(_METAL_MTP_LOG).breakdown

    assert breakdown.weights == pytest.approx(_METAL_MTP_WEIGHTS)
    assert breakdown.state == pytest.approx(_METAL_MTP_STATE)
    assert breakdown.output == pytest.approx(_METAL_MTP_OUTPUT)
    # The draft context's second reservation replaces its first.
    assert breakdown.compute == pytest.approx(_METAL_MTP_COMPUTE)
    assert breakdown.total == pytest.approx(
        _METAL_MTP_WEIGHTS + _METAL_MTP_STATE + _METAL_MTP_COMPUTE + _METAL_MTP_OUTPUT
    )


def test_overlapping_mapped_weights_count_as_the_file_size() -> None:
    breakdown = _parse(_K2_HORIZON_WEIGHTS_LOG, _K2_HORIZON_GGUF_MIB).breakdown

    assert breakdown.weights == pytest.approx(_K2_HORIZON_GGUF_MIB)
    assert breakdown.mapped_overlap == pytest.approx(2447.50 + 37961.54 + 28375.98 - 66348.5)


def test_mapped_weights_within_the_file_size_are_kept() -> None:
    breakdown = _parse(_METAL_MTP_LOG, mapped_limit_mib=67774.8).breakdown

    assert breakdown.weights == pytest.approx(_METAL_MTP_WEIGHTS)
    assert breakdown.mapped_overlap == 0


def test_only_mapped_weights_are_capped() -> None:
    breakdown = _parse(_CUDA_LOG, mapped_limit_mib=100.0).breakdown

    # The CUDA0 copy is real device memory; only the CPU_Mapped view is capped.
    assert breakdown.weights == pytest.approx(7953.53 + 100.0)


# llama-fit-params on K2-Horizon-32B BF16 at 256K: the first table is the
# projection for the parameters as given; later ones are its layer-moving trials.
_FIT_LOG = """\
ggml_metal_device_init: has unified memory    = true
common_params_fit_impl: getting device memory data for initial parameters:
common_memory_breakdown_print: | memory breakdown [MiB]  |  total     free      self   model   context   compute    unaccounted |
common_memory_breakdown_print: |   - MTL0 (Apple M5 Max) | 110100 = 110099 + (130822 = 63890 +   65536 +    1396) +     -130822 |
common_memory_breakdown_print: |   - Host                |                      3039 =  2447 +       0 +     592                |
common_params_fit_impl: projected to use 130822 MiB of device memory vs. 110099 MiB of free device memory
common_memory_breakdown_print: | memory breakdown [MiB]  |  total     free      self   model   context   compute    unaccounted |
common_memory_breakdown_print: |   - MTL0 (Apple M5 Max) | 110100 = 110099 + (108994 = 53329 +   54272 +    1392) +     -108993 |
common_memory_breakdown_print: |   - Host                |                     25195 = 13007 +   11264 +     924                |
-c 262144 -ngl 54
"""  # noqa: E501
_FIT_TOTAL_MIB = 63890 + 65536 + 1396 + 2447 + 0 + 592


def test_projection_is_the_first_breakdown_table_only() -> None:
    projection = _parse(_FIT_LOG).projection

    assert projection is not None
    assert projection.weights == 63890 + 2447
    assert projection.state == 65536
    assert projection.compute == 1396 + 592
    assert projection.total == _FIT_TOTAL_MIB


def test_no_projection_without_a_table() -> None:
    assert _parse(_METAL_MTP_LOG).projection is None


def test_projection_onto_a_cuda_device_counts_as_discrete() -> None:
    log = _parse(_FIT_LOG.replace("has unified memory    = true", "").replace("MTL0", "CUDA0"))

    assert log.discrete_gpu


def test_reports_load_and_prompt_cache_limit() -> None:
    log = _parse(_METAL_MTP_LOG)

    assert log.loaded
    assert log.prompt_cache_mib == 8192


def test_not_loaded_until_llama_server_says_so() -> None:
    log = _parse(_METAL_MTP_LOG.split("model loaded")[0])

    assert not log.loaded


@pytest.mark.parametrize(
    ("text", "discrete"),
    [
        (_METAL_MTP_LOG, False),
        (_VULKAN_APU_LOG, False),
        (_CPU_ONLY_LOG, False),
        (_CUDA_LOG, True),
        (_METAL_MTP_LOG.replace("unified memory    = true", "unified memory    = false"), True),
    ],
    ids=["metal-unified", "vulkan-uma", "cpu-only", "cuda", "metal-discrete"],
)
def test_detects_discrete_gpu(text: str, discrete: bool) -> None:
    assert _parse(text).discrete_gpu is discrete


# ---------------------------------------------------------------------------
# CLI, end to end against a fake llama-server
# ---------------------------------------------------------------------------

_MIB = 1024 * 1024

# Small mmap'd weights (they fit a 1 MiB fake GGUF) and a large KV cache.
_CLI_LOG = """\
ggml_metal_device_init: has unified memory    = true
load_tensors:   CPU_Mapped model buffer size =     0.25 MiB
load_tensors:  MTL0_Mapped model buffer size =     0.75 MiB
llama_context: constructing llama_context
llama_context:        CPU  output buffer size =     1.80 MiB
llama_kv_cache:       MTL0 KV buffer size = 70000.00 MiB
sched_reserve:       MTL0 compute buffer size =  1569.60 MiB
srv  load_model: prompt cache is enabled, size limit: 8192 MiB
srv  llama_server: model loaded
"""


def _cli_log_with_mapped(cpu_mib: float, device_mib: float) -> str:
    return _CLI_LOG.replace("0.25 MiB", f"{cpu_mib:.2f} MiB").replace(
        "0.75 MiB", f"{device_mib:.2f} MiB"
    )


def _stderr_row(err: str, label: str) -> str:
    match = re.search(rf"^{label}\s+(\S+) MiB$", err, re.MULTILINE)
    assert match is not None, err
    return match.group(1)


_FAKE_SERVER = r"""
import json, os, sys, time
with open(os.environ["FAKE_LLAMA_ARGV_FILE"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
with open(os.environ["FAKE_LLAMA_LOG_FILE"], encoding="utf-8") as fh:
    sys.stdout.write(fh.read())
sys.stdout.flush()
code = os.environ.get("FAKE_LLAMA_EXIT_CODE")
if code:
    sys.exit(int(code))
time.sleep(600)
"""


def _make_fake_executable(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake-llama-server.py"
    script_path.write_text(_FAKE_SERVER, encoding="utf-8")
    if sys.platform == "win32":
        path = tmp_path / "fake-llama-server.bat"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")
        return path
    path = tmp_path / "fake-llama-server"
    path.write_text(f"#!/usr/bin/env python3\n{_FAKE_SERVER}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class _FakeLlama:
    """A throwaway HOME with llama.cpp "installed" and one model, plus its fake server."""

    __argv_file: Path
    __log_file: Path

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        log: str,
        *,
        shards: int = 1,
    ) -> None:
        """Build the fake home and point ``HOME`` at it.

        Args:
            tmp_path (Path): Per-test directory.
            monkeypatch (pytest.MonkeyPatch): Undoes the environment changes.
            log (str): What the fake llama-server prints before waiting.
            shards (int): How many 1 MiB files the GGUF is split into.
        """
        home = tmp_path / "home"
        kodo_dir = home / ".kodo"
        (kodo_dir / "llama.cpp").mkdir(parents=True)
        (kodo_dir / "etc").mkdir(parents=True)
        executable = _make_fake_executable(tmp_path)
        (kodo_dir / "llama.cpp" / "llama-meta.json").write_text(
            json.dumps({"build": 1, "executable": str(executable)}), encoding="utf-8"
        )
        paths = (
            [tmp_path / "model.gguf"]
            if shards == 1
            else [tmp_path / f"model-{i:05d}-of-{shards:05d}.gguf" for i in range(1, shards + 1)]
        )
        for path in paths:
            path.write_bytes(b"GGUF".ljust(_MIB, b"\0"))
        gguf = paths[0]
        (kodo_dir / "etc" / "local-llm-registry.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "name": "fake-model",
                            "kind": "custom_file",
                            "path": str(gguf),
                            "context_window": 4096,
                        }
                    ],
                    # A stored pick the measurement must ignore.
                    "knob_selections": {"fake-model": {"kv-cache": "q4_0"}},
                }
            ),
            encoding="utf-8",
        )
        self.__argv_file = tmp_path / "argv.json"
        self.__log_file = tmp_path / "llama.log"
        self.__log_file.write_text(log, encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("FAKE_LLAMA_ARGV_FILE", str(self.__argv_file))
        monkeypatch.setenv("FAKE_LLAMA_LOG_FILE", str(self.__log_file))

    @property
    def launched(self) -> bool:
        """Whether the fake llama-server was ever started."""
        return self.__argv_file.exists()

    @property
    def argv(self) -> list[str]:
        """The arguments the fake llama-server was launched with."""
        return list(json.loads(self.__argv_file.read_text(encoding="utf-8")))


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_prints_total_in_gb_rounded_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    assert main(["fake-model"]) == 0

    out, err = capsys.readouterr()
    # 71572.4 MiB = 75.048 GB, rounded *up* to one decimal.
    assert out == "75.1\n"
    assert "warning" not in err
    assert "overlap" not in err
    assert "8192 MiB" in err


def test_overlapping_mapped_weights_are_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _cli_log_with_mapped(0.5, 1.0))

    assert main(["fake-model"]) == 0

    _, err = capsys.readouterr()
    assert _stderr_row(err, "weights") == "1.0"
    assert "overlap by 0.5 MiB" in err


def test_every_shard_of_a_split_gguf_counts_toward_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _cli_log_with_mapped(0.5, 1.0), shards=2)

    assert main(["fake-model"]) == 0

    _, err = capsys.readouterr()
    assert _stderr_row(err, "weights") == "1.5"
    assert "overlap" not in err


def test_launches_catalog_defaults_with_overrides_on_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    assert main(["fake-model"]) == 0
    defaults = fake.argv
    # The kv-cache knob's default, not the stored q4_0 pick.
    assert _flag(defaults, "--cache-type-k") == "q8_0"
    assert _flag(defaults, "--ctx-size") == "0"
    assert "--rope-scaling" not in defaults

    argv = [
        "fake-model",
        "--ctx-size",
        "65536",
        "--rope-scale",
        "2",
        "--yarn-orig-ctx",
        "32768",
        "-ctk",
        "f16",
        "--cache-type-v",
        "q4_0",
    ]
    assert main(argv) == 0
    overridden = fake.argv
    assert _flag(overridden, "--ctx-size") == "65536"
    assert _flag(overridden, "--rope-scaling") == "yarn"
    assert _flag(overridden, "--rope-scale") == "2.0"
    assert _flag(overridden, "--yarn-orig-ctx") == "32768"
    assert _flag(overridden, "--cache-type-k") == "f16"
    assert _flag(overridden, "--cache-type-v") == "q4_0"


def test_probe_measures_unfitted_at_full_verbosity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    assert main(["fake-model"]) == 0

    argv = fake.argv
    assert _flag(argv, "--fit") == "off"
    # Verbosity must come before any flag that initializes the backends.
    assert argv[:2] == ["-lv", "4"]


def test_warns_on_discrete_gpu_but_still_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CUDA_LOG)

    assert main(["fake-model"]) == 0

    out, err = capsys.readouterr()
    assert float(out) > 0
    assert "warning:" in err
    assert "discrete GPU" in err


def test_llama_server_exiting_before_load_fails_with_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, "llama_model_load: error loading model: unknown arch\n")
    monkeypatch.setenv("FAKE_LLAMA_EXIT_CODE", "1")

    assert main(["fake-model"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "unknown arch" in err


def test_timeout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CLI_LOG.split("model loaded")[0])

    assert main(["fake-model", "--timeout", "1"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "did not load" in err


def test_unknown_model_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    assert main(["no-such-model"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "Unknown local model" in err


def test_rejects_unknown_kv_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    with pytest.raises(SystemExit):
        main(["fake-model", "--cache-type-k", "q3_k"])


# ---------------------------------------------------------------------------
# Projection with llama-fit-params
# ---------------------------------------------------------------------------

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="llama-fit-params is looked up by its .exe name on Windows"
)

_FAKE_FIT_PARAMS = r"""
import os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_FIT_ARGV_FILE"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(argv) + "\n")
for flag in filter(None, os.environ.get("FAKE_FIT_REJECT", "").split(",")):
    if flag in argv:
        print(f"error: invalid argument: {flag}")
        sys.exit(1)
with open(os.environ["FAKE_FIT_LOG_FILE"], encoding="utf-8") as fh:
    sys.stdout.write(fh.read())
"""


def _install_fake_fit_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, *, reject: str = ""
) -> Path:
    """Put a fake ``llama-fit-params`` next to the fake llama-server.

    Returns:
        Path: The file each run appends its argv to, one line per run.
    """
    path = tmp_path / "llama-fit-params"
    path.write_text(f"#!/usr/bin/env python3\n{_FAKE_FIT_PARAMS}\n", encoding="utf-8")
    path.chmod(0o755)
    output_file = tmp_path / "fit.log"
    output_file.write_text(output, encoding="utf-8")
    argv_file = tmp_path / "fit-argv.txt"
    monkeypatch.setenv("FAKE_FIT_LOG_FILE", str(output_file))
    monkeypatch.setenv("FAKE_FIT_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_FIT_REJECT", reject)
    return argv_file


def _huge(fit_log: str) -> str:
    """*fit_log* with a KV cache far larger than any machine's RAM."""
    return fit_log.replace("65536 +    1396", "9965536 +    1396")


@_POSIX_ONLY
def test_dry_run_reports_the_projection_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    _install_fake_fit_params(tmp_path, monkeypatch, _FIT_LOG)

    assert main(["fake-model", "--dry-run"]) == 0

    out, err = capsys.readouterr()
    # 133861 MiB = 140.36 GB, rounded up.
    assert out == "140.4\n"
    assert "nothing was loaded" in err
    assert not fake.launched


@_POSIX_ONLY
def test_a_projection_larger_than_ram_is_reported_instead_of_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    _install_fake_fit_params(tmp_path, monkeypatch, _huge(_FIT_LOG))

    assert main(["fake-model"]) == 0

    out, err = capsys.readouterr()
    assert float(out) > 10_000
    assert "more than this machine's" in err
    assert not fake.launched


@_POSIX_ONLY
def test_a_projection_that_fits_ram_is_then_measured_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    small = _FIT_LOG.replace("65536 +    1396", "1024 +    1396")
    _install_fake_fit_params(tmp_path, monkeypatch, small)

    assert main(["fake-model"]) == 0

    out, _ = capsys.readouterr()
    # The real load's figure (_CLI_LOG), not the projection's.
    assert out == "75.1\n"
    assert fake.launched


@_POSIX_ONLY
def test_flags_llama_fit_params_rejects_are_dropped_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    argv_file = _install_fake_fit_params(
        tmp_path, monkeypatch, _FIT_LOG, reject="--jinja,--reasoning-format"
    )

    assert main(["fake-model", "--dry-run"]) == 0

    out, err = capsys.readouterr()
    assert out == "140.4\n"
    assert "--jinja" in err
    assert "--reasoning-format" in err
    last_run = argv_file.read_text(encoding="utf-8").splitlines()[-1].split()
    assert "--jinja" not in last_run
    assert "--reasoning-format" not in last_run
    assert last_run[:2] == ["-lv", "4"]
    assert "--ctx-size" in last_run


@_POSIX_ONLY
def test_projection_drops_speculative_decoding_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_file = _install_fake_fit_params(
        tmp_path, monkeypatch, _FIT_LOG, reject="--spec-type,--model-draft"
    )
    args = {"--ctx-size": "262144", "--spec-type": "draft-mtp", "--model-draft": "/h.gguf"}
    fit = FitProjection(tmp_path / "llama-fit-params", tmp_path / "m.gguf", args, timeout=60)

    projection = fit.run()

    assert projection.total == _FIT_TOTAL_MIB
    assert set(fit.dropped_flags) == {"--spec-type", "--model-draft"}
    last_run = argv_file.read_text(encoding="utf-8").splitlines()[-1].split()
    assert "--model-draft" not in last_run
    assert "/h.gguf" not in last_run


@_POSIX_ONLY
def test_dry_run_without_a_projection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    _install_fake_fit_params(tmp_path, monkeypatch, "common_fit_params: something went wrong\n")

    assert main(["fake-model", "--dry-run"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "no memory projection" in err
    assert not fake.launched


@_POSIX_ONLY
def test_without_a_projection_the_model_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)
    _install_fake_fit_params(tmp_path, monkeypatch, "common_fit_params: something went wrong\n")

    assert main(["fake-model"]) == 0

    out, _ = capsys.readouterr()
    assert out == "75.1\n"
    assert fake.launched


def test_dry_run_needs_llama_fit_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _CLI_LOG)

    assert main(["fake-model", "--dry-run"]) == 1

    _, err = capsys.readouterr()
    assert "llama-fit-params" in err
    assert not fake.launched
