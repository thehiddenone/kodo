"""``kodo-memcheck`` — empirical memory measurement of a local quant (doc/MEMCHECK.md).

The log parser is fed excerpts of real llama-server b11552 logs (``-lv 4``).
The CLI runs end to end against a fake "llama-server" executable that records
its argv, prints a canned log, and then waits to be stopped — the same
throwaway-``HOME`` approach as ``test_llamaserver_standalone.py``: llama.cpp
"installed" via ``llama-meta.json`` and one ``custom_file`` registry entry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kodo.memcheck import LlamaMemoryLog, main

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


def _parse(text: str) -> LlamaMemoryLog:
    log = LlamaMemoryLog()
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

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log: str) -> None:
        """Build the fake home and point ``HOME`` at it.

        Args:
            tmp_path (Path): Per-test directory.
            monkeypatch (pytest.MonkeyPatch): Undoes the environment changes.
            log (str): What the fake llama-server prints before waiting.
        """
        home = tmp_path / "home"
        kodo_dir = home / ".kodo"
        (kodo_dir / "llama.cpp").mkdir(parents=True)
        (kodo_dir / "etc").mkdir(parents=True)
        executable = _make_fake_executable(tmp_path)
        (kodo_dir / "llama.cpp" / "llama-meta.json").write_text(
            json.dumps({"build": 1, "executable": str(executable)}), encoding="utf-8"
        )
        gguf = tmp_path / "model.gguf"
        gguf.write_bytes(b"GGUF")
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
    def argv(self) -> list[str]:
        """The arguments the fake llama-server was launched with."""
        return list(json.loads(self.__argv_file.read_text(encoding="utf-8")))


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_prints_total_in_gb_rounded_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG)

    assert main(["fake-model"]) == 0

    out, err = capsys.readouterr()
    # 71572.4 MiB = 75.048 GB, rounded *up* to one decimal.
    assert out == "75.1\n"
    assert "warning" not in err
    assert "8192 MiB" in err


def test_launches_catalog_defaults_with_overrides_on_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG)

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
    fake = _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG)

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
    assert out.strip() == "12.9"
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
    _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG.split("model loaded")[0])

    assert main(["fake-model", "--timeout", "1"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "did not load" in err


def test_unknown_model_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG)

    assert main(["no-such-model"]) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "Unknown local model" in err


def test_rejects_unknown_kv_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLlama(tmp_path, monkeypatch, _METAL_MTP_LOG)

    with pytest.raises(SystemExit):
        main(["fake-model", "--cache-type-k", "q3_k"])
