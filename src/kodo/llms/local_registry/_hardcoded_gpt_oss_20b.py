"""Hardcoded GPT-OSS-20B GGUF catalog entries."""

from __future__ import annotations

from ._types import LlamaFlavor, LocalLLMEntry


def gpt_oss_20b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-gpt-oss-20b-f16",
            kind="hardcoded_hf",
            description="GPT OSS 20B F16 by Unsloth",
            repo_id="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-F16.gguf",
            flavors=(LlamaFlavor.make_default_kv_fp16(),),
            context_window=131_072,
            base_llm="GPT-OSS-20B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="F16",
            size_hint="13.8 GB",
            gpu_tip="~20GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~24GB of DDR5 "
            "system RAM covers it comfortably — llama.cpp's MoE offloading keeps GPT-OSS-20B fast "
            "even on a modest card.",
            mac_tip="Needs ~20GB — fits a 32GB MacBook Pro (M4 or M5) comfortably; a 24GB config is"
            "tight.",
            min_memory=24,
            memory=32,
            llamacpp_version=6098,
        ),
        LocalLLMEntry(
            name="unsloth-gpt-oss-20b-q8-k-xl",
            kind="hardcoded_hf",
            description="GPT OSS 20B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-UD-Q8_K_XL.gguf",
            context_window=131_072,
            base_llm="GPT-OSS-20B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="13.2 GB",
            gpu_tip="~16GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 16GB DDR5 kit "
            "is all this needs — MoE offloading makes the 8GB card feel roomier than the raw total "
            "suggests.",
            mac_tip="Needs ~16GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
            min_memory=16,
            memory=24,
            llamacpp_version=6098,
        ),
        LocalLLMEntry(
            name="unsloth-gpt-oss-20b-q8-0",
            kind="hardcoded_hf",
            description="GPT OSS 20B Q8_0 by Unsloth",
            repo_id="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-Q8_0.gguf",
            context_window=131_072,
            base_llm="GPT-OSS-20B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="Q8_0",
            size_hint="12.1 GB",
            gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 16GB DDR5 "
            "kit covers it easily, with llama.cpp offloading the inactive experts to RAM.",
            mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
            min_memory=16,
            memory=24,
            llamacpp_version=6098,
        ),
        LocalLLMEntry(
            name="unsloth-gpt-oss-20b-q6-k-xl",
            kind="hardcoded_hf",
            description="GPT OSS 20B UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-UD-Q6_K_XL.gguf",
            context_window=131_072,
            base_llm="GPT-OSS-20B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="12.0 GB",
            gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 16GB DDR5 kit "
            "is enough — the sparse MoE architecture keeps offloaded performance close to a "
            "full-VRAM fit.",
            mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
            min_memory=16,
            memory=24,
            llamacpp_version=6098,
        ),
        LocalLLMEntry(
            name="unsloth-gpt-oss-20b-q4-k-xl",
            kind="hardcoded_hf",
            description="GPT OSS 20B UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-UD-Q4_K_XL.gguf",
            context_window=131_072,
            base_llm="GPT-OSS-20B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="11.9 GB",
            gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 16GB DDR5 kit "
            "covers it comfortably via llama.cpp's MoE expert offloading.",
            mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
            min_memory=16,
            memory=24,
            llamacpp_version=6098,
        ),
    ]
