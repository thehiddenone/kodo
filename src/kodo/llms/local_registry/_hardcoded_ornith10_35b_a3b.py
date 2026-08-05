"""Hardcoded Ornith10-35B-A3B GGUF catalog entries."""

from __future__ import annotations

from ._types import LlamaFlavor, LocalLLMEntry


def ornith10_35b_a3b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="deepreinforce-ornith10-35b-a3b-bf16",
            kind="hardcoded_hf",
            description="Ornith 1.0 35B A3B BF16 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
            filename="ornith-1.0-35b-bf16.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-bf16-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-bf16-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-35B-A3B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="BF16",
            size_hint="69.4 GB",
            gpu_tip="~103GB total at 128K context — the BF16 build is the heaviest way to run "
            "Ornith 1.0, and since it's dense (not MoE), every offloaded layer costs real speed. A "
            "16GB GPU (e.g. RTX 4080) plus a 128GB DDR5 kit will run it, but if raw speed matters "
            "more than bit-perfect precision, the quantized builds below hit similar quality at a "
            "fraction of the memory.",
            mac_tip="Needs ~103GB — tight on a 128GB M4 Max or M5 Max.",
            min_memory=128,
            memory=128,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-35b-a3b-q8-0",
            kind="hardcoded_hf",
            description="Ornith 1.0 35B A3B Q8_0 by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
            filename="ornith-1.0-35b-Q8_0.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q8-0-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q8-0-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-35B-A3B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q8_0",
            size_hint="36.9 GB",
            gpu_tip="~54GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) plus ~48GB of "
            "DDR5 system RAM covers it via llama.cpp's layer offloading — no need for the BF16 "
            "build's 128GB ask.",
            mac_tip="Needs ~54GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is the "
            "safe choice.",
            min_memory=64,
            memory=128,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-35b-a3b-q6-k",
            kind="hardcoded_hf",
            description="Ornith 1.0 35B A3B Q6_K by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
            filename="ornith-1.0-35b-Q6_K.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q6-k-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q6-k-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-35B-A3B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q6_K",
            size_hint="28.5 GB",
            gpu_tip="~45GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus ~48GB of DDR5 "
            "system RAM handles it comfortably, with llama.cpp keeping as many layers on the GPU "
            "as VRAM allows.",
            mac_tip="Needs ~45GB — a 48GB MacBook Pro is close to its limit; a 64GB config "
            "(M4 Pro/Max or M5 Pro/Max) is safer.",
            min_memory=48,
            memory=64,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-35b-a3b-q5-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.0 35B A3B Q5_K by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
            filename="ornith-1.0-35b-Q5_K_M.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q5-k-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q5-k-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-35B-A3B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q5_K_M",
            size_hint="24.7 GB",
            gpu_tip="~42GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~48GB of DDR5 "
            "system RAM is enough — llama.cpp's layer offloading fills in the gap without needing "
            "a big card.",
            mac_tip="Needs ~42GB — fits a 64GB MacBook Pro comfortably; a 48GB config is tight.",
            min_memory=48,
            memory=64,
            llamacpp_version=9831,
        ),
        LocalLLMEntry(
            name="deepreinforce-ornith10-35b-a3b-q4-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.0 35B A3B Q4_K by DeepReinforce",
            repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
            filename="ornith-1.0-35b-Q4_K_M.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_moe_512k_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q4-k-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_moe_1m_kv_q8(
                    "deepreinforce-ornith10-35b-a3b-q4-k-1m-kv-q8", "1M context size"
                ),
            ),
            base_llm="Ornith10-35B-A3B",
            llm_author="DeepReinforce AI",
            quant_author="DeepReinforce AI",
            quant_type="Q4_K_M",
            size_hint="21.2 GB",
            gpu_tip="~38GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~48GB of DDR5 "
            "system RAM covers the whole model via llama.cpp's layer offloading.",
            mac_tip="Needs ~38GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=9831,
        ),
    ]
