"""Ornith10-9B GGUF catalog entries."""

from __future__ import annotations

from ._knobs_qwen import QWEN_MOE_CONTEXT_KNOB
from ._knobs_shared import KV_CACHE_F16_DEFAULT, SHARED_KNOBS
from ._types import LocalLLMEntry


def ornith10_9b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-bf16",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B BF16 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-bf16.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            knob_defaults=KV_CACHE_F16_DEFAULT,
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="DeepReinforce AI",
            quant_type="BF16",
            size_hint="17.9 GB",
            gpu_tip="~22GB total at 128K context — the heaviest way to run this sparse-MoE model, "
            "but still small enough to fit entirely on a 24GB GPU (e.g. RTX 4090 or RTX 3090), no "
            "CPU offload needed. It's a tight fit though; the quantized builds below leave much "
            "more headroom.",
            mac_tip="Needs ~22GB — fits a 24GB MacBook Pro (M4, M4 Pro, M5, or M5 Pro), though "
            "it's close to the limit; a 32GB configuration leaves more headroom.",
            min_memory=24,
            memory=32,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q8-0",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q8_0 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q8_0.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="DeepReinforce AI",
            quant_type="Q8_0",
            size_hint="9.53 GB",
            gpu_tip="~14GB total at 128K context — small enough to fit entirely on a 16GB GPU "
            "(e.g. RTX 4060 Ti 16GB), no CPU offload needed.",
            mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=16,
            memory=24,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q6-k",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q6_K by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q6_K.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="DeepReinforce AI",
            quant_type="Q6_K",
            size_hint="7.36 GB",
            gpu_tip="~11GB total at 128K context — comfortably fits a 16GB GPU (e.g. RTX 5070 Ti), "
            "with room to spare.",
            mac_tip="Needs ~11GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=16,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q5-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q5_K_M by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q5_K_M.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="DeepReinforce AI",
            quant_type="Q5_K_M",
            size_hint="6.47 GB",
            gpu_tip="~10GB total at 128K context — comfortably fits a 16GB GPU (e.g. RTX 4070 Ti "
            "Super).",
            mac_tip="Needs ~10GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=12,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q4-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q4_K_M by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q4_K_M.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB,),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="DeepReinforce AI",
            quant_type="Q4_K_M",
            size_hint="5.63 GB",
            gpu_tip="~9.5GB total at 128K context — the smallest Ornith 1.0 9B build; fits "
            "comfortably on a 16GB GPU (e.g. RX 7800 XT), with plenty of headroom.",
            mac_tip="Needs ~9.5GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=12,
            llamacpp_version=9831,
        ),
    ]
