"""Qwen36-35B-A3B GGUF catalog entries."""

from __future__ import annotations

from ._knobs_qwen import QWEN_MOE_CONTEXT_KNOB
from ._knobs_shared import SHARED_KNOBS
from ._types import LocalLLMEntry


def qwen36_35b_a3b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-qwen36-35b-a3b-q8-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 35B-A3B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
            filename="Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Qwen36-35B-A3B",
            llm_author="Alibaba Cloud",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="39.1 GB",
            gpu_tip="~46GB total at 128K context, but it's a sparse MoE model — most of those "
            "weights sit idle on any given token. An 8GB GPU (e.g. RTX 3060 Ti) keeps the "
            "always-on attention/shared layers at full speed while llama.cpp offloads the inactive "
            "experts to ~48GB of DDR5 system RAM, staying close to full-GPU speed.",
            mac_tip="Needs ~46GB — a 48GB MacBook Pro is close to its limit; a 64GB config "
            "(M4 Pro/Max or M5 Pro/Max) is safer.",
            min_memory=48,
            memory=64,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-35b-a3b-q6-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 35B-A3B UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
            filename="Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Qwen36-35B-A3B",
            llm_author="Alibaba Cloud",
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="32.6 GB",
            gpu_tip="~39GB total at 128K context. Same MoE-offload trick as the Q8_K_XL build: an "
            "8GB GPU (e.g. RTX 5060) handles the shared layers at full speed, and ~48GB of DDR5 "
            "system RAM comfortably holds the offloaded experts.",
            mac_tip="Needs ~39GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-35b-a3b-q5-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 35B-A3B UD-Q5_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
            filename="Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Qwen36-35B-A3B",
            llm_author="Alibaba Cloud",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_XL",
            size_hint="27.2 GB",
            gpu_tip="~34GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~32GB of DDR5 "
            "system RAM is enough — llama.cpp's MoE offloading keeps this close to full-GPU speed.",
            mac_tip="Needs ~34GB — fits a 48GB MacBook Pro comfortably; a 36GB M4 Max or M5 Max is "
            "tight.",
            min_memory=36,
            memory=48,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-35b-a3b-q4-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 35B-A3B UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
            filename="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Qwen36-35B-A3B",
            llm_author="Alibaba Cloud",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="22.9 GB",
            gpu_tip="~30GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 32GB DDR5 kit "
            "covers it — the sparse MoE architecture means llama.cpp's expert offloading barely "
            "costs you any speed.",
            mac_tip="Needs ~30GB — tight on a 32GB MacBook Pro (M4 or M5); a 36GB M4 Max/M5 Max is "
            "the safe choice.",
            min_memory=32,
            memory=36,
            llamacpp_version=3100,
        ),
    ]
