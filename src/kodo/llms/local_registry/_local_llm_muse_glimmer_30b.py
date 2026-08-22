"""Muse Glimmer 30B GGUF catalog entries.

Meta's dense 29.6B-param open-weight distillation of their cloud "Muse Spark"
model (https://huggingface.co/unsloth/Muse-Glimmer-30B-GGUF), released
2026-08. Architecturally a Gemma/Nemotron-style local/global mix rather than
plain dense attention: 52 layers, 1-in-4 full attention with the rest capped
to a 2048-token sliding window, plus 2 KV heads (GQA) — which is what keeps
the KV cache small relative to file size even at the full 131K context (see
the per-entry ``gpu_tip``/``mac_tip`` notes below).

**Deliberately not in either thinking-tier family** (:mod:`._thinking`).
Muse Glimmer's reasoning strength (low/medium/high/xhigh) is documented as
being set by a literal ``Reasoning strength: <value>`` line in the system
prompt — not a CLI token budget (the Qwen family) and not a
``chat_template_kwargs`` field consumed by the GGUF's own Jinja template (the
GPT-OSS family); the base model's ``tokenizer_config.json`` has no
``chat_template`` field at all. Wiring that would need a third thinking-tier
mechanism (system-prompt injection) touching code well beyond this package
(``_llama.py`` and wherever the request's system prompt gets assembled), and
upstream llama.cpp's own support is still catching up — model support landed
in ggml-org/llama.cpp#26841 (2026-08-10) and a tool-call parsing fix in #26879
(2026-08-11), but the PR that actually sets Muse Glimmer's thinking tags
(#27475) is still open/unmerged as of 2026-08-21. Revisit once that lands and
kodo has a system-prompt-injection mechanism to hang it on.
"""

from __future__ import annotations

from ._knobs_shared import KV_CACHE_F16_DEFAULT, SHARED_KNOBS
from ._types import LocalLLMEntry


def muse_glimmer_30b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-bf16",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B BF16 by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="BF16/Muse-Glimmer-30B-BF16-00001-of-00002.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            knob_defaults=KV_CACHE_F16_DEFAULT,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="BF16",
            size_hint="55.7 GB",
            gpu_tip="~59GB total, even at the full 131K context — Muse Glimmer only runs full "
            "attention on 1 in 4 layers (the rest use a 2048-token sliding window) and has just 2 "
            "KV heads, so the cache barely grows with context; the total is close to the BF16 file "
            "size plus a small margin. A 16GB GPU (e.g. RTX 4080) plus a 48GB DDR5 kit covers it, "
            "with llama.cpp offloading the remaining layers to system RAM.",
            mac_tip="Needs ~59GB — comfortable on a 96GB M4 Max/M5 Max; a 64GB config is tight.",
            min_memory=64,
            memory=96,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q8-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q8_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="32.3 GB",
            gpu_tip="~35GB total, and that number barely moves with context length — the sliding-"
            "window/GQA combo keeps the cache small even at the full 131K window. An 8GB GPU (e.g. "
            "RTX 4060 Ti) plus ~32GB of DDR5 system RAM covers it.",
            mac_tip="Needs ~35GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q8-0",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B Q8_0 by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-Q8_0.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="Q8_0",
            size_hint="29.6 GB",
            gpu_tip="~33GB total at the full 131K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a "
            "32GB DDR5 kit is enough — the sliding-window layers keep the cache from growing much "
            "past its short-context footprint.",
            mac_tip="Needs ~33GB — fits a 48GB MacBook Pro comfortably.",
            min_memory=32,
            memory=48,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q6-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q6_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="26.3 GB",
            gpu_tip="~29GB total, close to the file size even at 131K context thanks to the "
            "sliding-window attention pattern. An 8GB GPU (e.g. RTX 5060) plus ~24GB of DDR5 "
            "system RAM covers it.",
            mac_tip="Needs ~29GB — fits a 32GB MacBook Pro comfortably; a 48GB config gives more "
            "headroom.",
            min_memory=32,
            memory=48,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q5-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q5_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q5_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_XL",
            size_hint="21.8 GB",
            gpu_tip="~25GB total at the full 131K context. An 8GB GPU (e.g. RX 7600) plus a 24GB "
            "DDR5 kit is enough, with the sliding-window layers keeping the cache from growing "
            "much.",
            mac_tip="Needs ~25GB — fits a 36GB MacBook Pro comfortably; a 32GB config is tight.",
            min_memory=32,
            memory=36,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q5-k-l",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q5_K_L by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q5_K_L.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_L",
            size_hint="19.8 GB",
            gpu_tip="~23GB total at the full 131K context. An 8GB GPU (e.g. RTX 4060) plus a 24GB "
            "DDR5 kit covers it comfortably.",
            mac_tip="Needs ~23GB — fits a 32GB MacBook Pro comfortably.",
            min_memory=32,
            memory=32,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q5-k-m",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q5_K_M by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q5_K_M.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_M",
            size_hint="19.2 GB",
            gpu_tip="~22GB total at the full 131K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a "
            "24GB DDR5 kit covers it comfortably.",
            mac_tip="Needs ~22GB — fits a 32GB MacBook Pro comfortably.",
            min_memory=24,
            memory=32,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q4-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q4_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="15.9 GB",
            gpu_tip="~19GB total at the full 131K context. An 8GB GPU (e.g. RTX 4060) plus a 24GB "
            "DDR5 kit covers it comfortably — a typical gaming rig once llama.cpp splits the "
            "layers.",
            mac_tip="Needs ~19GB — fits a 32GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=24,
            memory=32,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-iq3-m",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-IQ3_M by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-IQ3_M.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-IQ3_M",
            size_hint="14.1 GB",
            gpu_tip="~17GB total at the full 131K context. An 8GB GPU (e.g. RX 7600) plus a 24GB "
            "DDR5 kit covers it comfortably.",
            mac_tip="Needs ~17GB — fits a 24GB MacBook Pro comfortably.",
            min_memory=24,
            memory=24,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q3-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q3_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q3_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q3_K_XL",
            size_hint="13.4 GB",
            gpu_tip="~16GB total at the full 131K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a "
            "16GB DDR5 kit covers it, with llama.cpp's layer offloading handling the rest.",
            mac_tip="Needs ~16GB — fits a 24GB MacBook Pro comfortably.",
            min_memory=24,
            memory=24,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-q2-k-xl",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-Q2_K_XL by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-Q2_K_XL.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-Q2_K_XL",
            size_hint="12.4 GB",
            gpu_tip="~15GB total at the full 131K context. An 8GB GPU (e.g. RTX 5060) plus a 16GB "
            "DDR5 kit covers it comfortably.",
            mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably.",
            min_memory=16,
            memory=24,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-iq2-xs",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-IQ2_XS by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-IQ2_XS.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-IQ2_XS",
            size_hint="11.5 GB",
            gpu_tip="~15GB total at the full 131K context. Any 8GB GPU (e.g. RTX 4060) plus a 16GB "
            "DDR5 kit handles this.",
            mac_tip="Needs ~15GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=16,
            memory=16,
            llamacpp_version=10549,
        ),
        LocalLLMEntry(
            name="unsloth-muse-glimmer-30b-iq2-xxs",
            kind="hardcoded_hf",
            description="Muse Glimmer 30B UD-IQ2_XXS by Unsloth",
            repo_id="unsloth/Muse-Glimmer-30B-GGUF",
            filename="Muse-Glimmer-30B-UD-IQ2_XXS.gguf",
            context_window=131_072,
            knobs=SHARED_KNOBS,
            base_llm="MuseGlimmer-30B",
            llm_author="Meta",
            license_name="Apache License 2.0",
            license_url="https://www.apache.org/licenses/LICENSE-2.0",
            quant_author="Unsloth",
            quant_type="UD-IQ2_XXS",
            size_hint="10.7 GB",
            gpu_tip="~14GB total at the full 131K context. Any 8GB GPU (e.g. RTX 4060) plus a 16GB "
            "DDR5 kit handles this, the smallest of the Muse Glimmer 30B builds.",
            mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
            min_memory=16,
            memory=16,
            llamacpp_version=10549,
        ),
    ]
