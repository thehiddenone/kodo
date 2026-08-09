"""GPT-OSS-120B GGUF catalog entries."""

from __future__ import annotations

from ._knobs_shared import KV_CACHE_F16_DEFAULT
from ._types import LocalLLMEntry


def gpt_oss_120b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-gpt-oss-120b-f16",
            kind="hardcoded_hf",
            description="GPT OSS 120B F16 by Unsloth",
            repo_id="unsloth/gpt-oss-120b-GGUF",
            filename="gpt-oss-120b-F16.gguf",
            knob_defaults=KV_CACHE_F16_DEFAULT,
            context_window=131_072,
            base_llm="GPT-OSS-120B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="F16",
            size_hint="65.4 GB",
            gpu_tip="~75GB total at 128K context — this is a big one, but it's GPT-OSS's sparse "
            "MoE architecture at its best. A 16GB GPU (e.g. RX 7800 XT) runs the shared layers at "
            "full speed while llama.cpp offloads the experts to ~96GB of DDR5 system RAM — no "
            "datacenter card required.",
            mac_tip="Needs ~75GB — a 128GB MacBook Pro (M4 Max or M5 Max) is required, and it's "
            "right at the edge even there.",
            min_memory=128,
            memory=128,
            llamacpp_version=6098,
        ),
        LocalLLMEntry(
            name="unsloth-gpt-oss-120b-ud-q8-k-xl",
            kind="hardcoded_hf",
            description="GPT OSS 120B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/gpt-oss-120b-GGUF",
            filename="UD-Q8_K_XL/gpt-oss-120b-UD-Q8_K_XL-00001-of-00002.gguf",
            context_window=131_072,
            base_llm="GPT-OSS-120B",
            llm_author="OpenAI",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="64.5 GB",
            gpu_tip="~75GB total at 128K context — this is a big one, but it's GPT-OSS's sparse "
            "MoE architecture at its best. A 16GB GPU (e.g. RX 7800 XT) runs the shared layers at "
            "full speed while llama.cpp offloads the experts to ~96GB of DDR5 system RAM — no "
            "datacenter card required.",
            mac_tip="Needs ~75GB — a 128GB MacBook Pro (M4 Max or M5 Max) is required, and it's "
            "right at the edge even there.",
            min_memory=128,
            memory=128,
            llamacpp_version=6098,
        ),
    ]
