"""Hardcoded Ornith10-9B GGUF catalog entries."""

from __future__ import annotations

from ._types import LlamaFlavor, LocalLLMEntry


def ornith10_9b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-bf16",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B BF16 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-bf16.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-9b-bf16-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-9b-bf16-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="BF16",
            size_hint="17.9 GB",
            gpu_tip="",
            mac_tip="",
            min_memory=24,
            memory=24,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q8-0",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q8_0 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q8_0.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-9b-q8-0-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-9b-q8-0-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q8_0",
            size_hint="9.53 GB",
            gpu_tip="",
            mac_tip="",
            min_memory=12,
            memory=16,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-9b-q6-k",
            kind="hardcoded_hf",
            description="Ornith 1.0 9B Q6_K by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-9B-GGUF",
            filename="ornith-1.0-9b-Q6_K.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-9b-q6-k-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-9b-q6-k-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q6_K",
            size_hint="7.36 GB",
            gpu_tip="",
            mac_tip="",
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
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-9b-q5-k-m-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-9b-q5-k-m-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q5_K_M",
            size_hint="6.47 GB",
            gpu_tip="",
            mac_tip="",
            min_memory=8,
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
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-9b-q4-k-m-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-9b-q4-k-m-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-9B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q4_K_M",
            size_hint="5.63 GB",
            gpu_tip="",
            mac_tip="",
            min_memory=8,
            memory=12,
            llamacpp_version=9831,
        ),
    ]
