"""Ornith15-9B GGUF catalog entries.

The small member of the Ornith 1.5 family. Unlike its 35B-A3B sibling this
is a **dense** build: its GGUF records ``general.architecture = qwen35``, not
``qwen35moe``, so it takes :data:`~._knobs_qwen.QWEN_CONTEXT_KNOB` — the
dense Qwen YaRN knob, whose ``--override-kv`` targets
``qwen35.context_length``. Pointing the MoE knob at it would write an
override for a metadata key this GGUF does not have, llama.cpp would ignore
it silently, and the extended 512K/1M options would appear to work while the
KV cache stayed capped at the trained 262144 tokens.

The upstream model is multimodal and bartowski's repo ships
``mmproj-Ornith-1.5-9B-{f16,bf16}.gguf`` companions, but
:class:`~._types.LocalLLMEntry` has no field to declare one, so every entry
here is registered text-only. Unlike the 35B-A3B build there are no MTP
layers in these quants (bartowski's model card lists "Speculative decoding:
no"), so this family declares no MTP knob.

The quant ladder mirrors the five rungs Ornith 1.0 9B ships
(BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M) plus three smaller builds. The tail is
Q3_K_L/IQ3_M/Q2_K rather than the 35B family's Q3_K_XL/IQ3_M/Q2_K_L: at 9B
the embedding and output tensors are a much larger share of the file, so
bartowski's ``_L``/``_XL`` variants — which hold those at Q8_0 — do not
shrink here. Q3_K_XL is 6.00 GB and Q2_K_L is 5.06 GB, both *larger* than
this family's own 5.91 GB Q4_K_M and 5.11 GB Q3_K_L, which would make them
pointless as low-memory rungs.
"""

from __future__ import annotations

from ._knobs_qwen import QWEN_CONTEXT_KNOB
from ._knobs_shared import KV_CACHE_F16_DEFAULT, SHARED_KNOBS
from ._types import LocalLLMEntry


def ornith15_9b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="bartowski-ornith15-9b-bf16",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B BF16 by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-bf16.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            knob_defaults=KV_CACHE_F16_DEFAULT,
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="BF16",
            size_hint="17.9 GB",
            gpu_tip="~22GB total at 128K context — the heaviest way to run this model, but still "
            "small enough to fit entirely on a 24GB GPU (e.g. RTX 4090 or RTX 3090) with no CPU "
            "offload. It is a dense build, so every layer that does spill to system RAM costs "
            "real speed; the quantized builds below leave much more headroom.",
            mac_tip="Needs ~22GB — fits a 24GB MacBook Pro (M4, M4 Pro, M5, or M5 Pro), though "
            "it's close to the limit; a 32GB configuration leaves more headroom.",
            min_memory=24,
            memory=32,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q8-0",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q8_0 by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q8_0.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q8_0",
            size_hint="9.55 GB",
            gpu_tip="~14GB total at 128K context — small enough to fit entirely on a 16GB GPU "
            "(e.g. RTX 4060 Ti 16GB), no CPU offload needed.",
            mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=16,
            memory=24,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q6-k",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q6_K by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q6_K.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q6_K",
            size_hint="7.70 GB",
            gpu_tip="~12GB total at 128K context — comfortably fits a 16GB GPU (e.g. RTX 5070 "
            "Ti), with room to spare.",
            mac_tip="Needs ~12GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=16,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q5-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q5_K_M by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q5_K_M.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q5_K_M",
            size_hint="6.85 GB",
            gpu_tip="~11GB total at 128K context — comfortably fits a 16GB GPU (e.g. RTX 4070 Ti "
            "Super).",
            mac_tip="Needs ~11GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=16,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q4-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q4_K_M by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q4_K_M.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q4_K_M",
            size_hint="5.91 GB",
            gpu_tip="~10GB total at 128K context, and the rung bartowski recommends as the "
            "general-purpose default; fits comfortably on a 16GB GPU (e.g. RX 7800 XT), with "
            "plenty of headroom.",
            mac_tip="Needs ~10GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=12,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q3-k-l",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q3_K_L by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q3_K_L.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q3_K_L",
            size_hint="5.11 GB",
            gpu_tip="~9GB total at 128K context. Lower quality than the Q4 rung but usable, and "
            "it fits an 8GB GPU (e.g. RTX 4060) with only a little spilling to system RAM.",
            mac_tip="Needs ~9GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=12,
            memory=12,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-iq3-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B IQ3_M by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-IQ3_M.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="IQ3_M",
            size_hint="4.72 GB",
            gpu_tip="~8.5GB total at 128K context. An i-quant: smaller than the K-quants at the "
            "same bit width and imatrix-calibrated, at the cost of more CPU work per token, so "
            "it rewards fitting as much as possible on an 8GB GPU.",
            mac_tip="Needs ~8.5GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=8,
            memory=12,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-9b-q2-k",
            kind="hardcoded_hf",
            description="Ornith 1.5 9B Q2_K by bartowski",
            repo_id="bartowski/Ornith-1.5-9B-GGUF",
            filename="Ornith-1.5-9B-Q2_K.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN_CONTEXT_KNOB,),
            base_llm="Ornith15-9B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q2_K",
            size_hint="4.06 GB",
            gpu_tip="~8GB total at 128K context — the smallest Ornith 1.5 9B build, sized to fit "
            "an 8GB GPU (e.g. RTX 3060 Ti) end to end. Quality drops noticeably at 2-bit on a "
            "model this small; prefer IQ3_M or Q3_K_L whenever the memory is there.",
            mac_tip="Needs ~8GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=8,
            memory=12,
            llamacpp_version=10472,
        ),
    ]
