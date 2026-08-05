"""Hardcoded Qwen35-9B GGUF catalog entries."""

from __future__ import annotations

from ._types import LlamaFlavor, LocalLLMEntry


def qwen35_9b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-qwen35-9b-q8-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.5 9B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.5-9B-MTP-GGUF",
            filename="Qwen3.5-9B-UD-Q8_K_XL.gguf",
            context_window=262_144,
            flavors=(
                LlamaFlavor.make_default_kv_q8(),
                LlamaFlavor.make_qwen_512k_kv_q8(
                    "unsloth-qwen35-9b-q8-k-xl-512k-kv-q8", "512K context size"
                ),
                LlamaFlavor.make_qwen_1m_kv_q8(
                    "unsloth-qwen35-9b-q8-k-xl-kv-q8", "1M context size"
                ),
            ),
            base_llm="Qwen35-9B",
            llm_author="Alibaba Cloud",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="13.2 GB",
            gpu_tip="~17GB total at 128K context. Any 8GB GPU (e.g. RTX 5060) plus a basic 16GB "
            "DDR5 kit is plenty — this one barely needs the offloading trick at all.",
            mac_tip="Needs ~17GB — fits a 24GB MacBook Pro (M4, M4 Pro, M5, or M5 Pro) "
            "comfortably.",
            min_memory=24,
            memory=24,
            llamacpp_version=5092,
        ),
    ]
