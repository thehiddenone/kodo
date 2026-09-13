"""Qwen3.8-Flash-Next GGUF catalog entries.

A 125B-parameter sparse MoE (512 experts, 10 routed + 1 shared active, ~6B
activated) on top of a further 51B of n-gram embeddings, built as a *hybrid*
stack: 12 repeats of ``3 x Gated DeltaNet -> 1 x Qwen Sparse Attention``, so
only 12 of its 48 layers carry a real KV cache and the other 36 hold a
constant-size recurrent state instead. That is what makes the memory story
here unusual and why the hardware tips below read the way they do — context
is nearly free compared with a conventional attention model of this size,
while the weights themselves are the whole cost. Every quant is a split GGUF
(3-8 shards); ``filename`` names the first shard, as elsewhere in this
package, and :class:`~kodo.llms.local.LocalModelManager` pulls the rest.

Two things in the repository are deliberately **not** wired up:

- ``mmproj-BF16.gguf``/``mmproj-F16.gguf``. The base model is multimodal
  (``image-text-to-text``), but :class:`~._types.LocalLLMEntry` has no mmproj
  field and mmproj companions are not reachable from the UI at all — see
  doc/LLM_REGISTRY.md §4.3. These entries are text-only, exactly like every
  other hardcoded entry.
- The ``MTP/`` NextN speculative-decoding heads. llama.cpp's draft-head
  support for this architecture is still an open PR (ggml-org/llama.cpp#27836
  / #27842 as of 2026-09-13) and kodo launches no ``--spec-type`` flag.

``llamacpp_version`` is 10829 rather than 10660, the build that first loaded
the architecture (ggml-org/llama.cpp#27742, merged 2026-08-27). Three
correctness follow-ups landed after it — #27941, #28123 (recurrent-state
rollback) and #28068 (Gated DeltaNet normalization ``max`` -> ``rsqrt``, a
real output-quality bug) — and b10829 is the first build containing all
three.
"""

from __future__ import annotations

from ._knobs_qwen import QWEN4EXP_CONTEXT_KNOB
from ._knobs_shared import KV_CACHE_F16_DEFAULT, SHARED_KNOBS
from ._types import LocalLLMEntry

#: Shared by every entry below — the base model's own license, read off
#: ``Qwen/Qwen3.8-Flash-Next``'s model card (``license_name``/``license_link``),
#: not the quant repo.
_LICENSE_NAME = "Qwen Community License 1.0"
_LICENSE_URL = "https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE"


def qwen38_flash_next_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-bf16",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next BF16 by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="BF16/Qwen3.8-Flash-Next-BF16-00001-of-00008.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            knob_defaults=KV_CACHE_F16_DEFAULT,
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="BF16",
            size_hint="354 GB",
            gpu_tip="~360GB total at 128K context — the unquantized build, and far beyond any "
            "single machine most people own. The hybrid design means context adds barely 6GB on "
            "top of the weights; the weights are the entire problem. Realistically this is a "
            "multi-GPU server or a 384GB+ workstation, not a desktop with an offload strategy.",
            mac_tip="Needs ~360GB — beyond every MacBook Pro and beyond a 192GB Mac Studio; only "
            "a 512GB M3 Ultra Mac Studio can hold it.",
            min_memory=384,
            memory=384,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-q8-0",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next Q8_0 by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="Q8_0/Qwen3.8-Flash-Next-Q8_0-00001-of-00006.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="Q8_0",
            size_hint="188 GB",
            gpu_tip="~194GB total at 128K context. With 512 experts and only 10 routed + 1 shared "
            "active per token, llama.cpp's MoE expert offloading works about as well here as it "
            "ever does — a 16GB GPU (e.g. RTX 4080) carries the always-on layers at full speed "
            "while a 256GB DDR5 workstation kit holds the mostly-idle experts.",
            mac_tip="Needs ~194GB — past a 192GB Mac Studio once macOS takes its share; a 256GB "
            "or 512GB M3 Ultra is the realistic option.",
            min_memory=192,
            memory=256,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-q6-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-Q6_K_XL/Qwen3.8-Flash-Next-UD-Q6_K_XL-00001-of-00006.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="169 GB",
            gpu_tip="~175GB total at 128K context. A 16GB GPU (e.g. RTX 5080) plus a 192GB DDR5 "
            "kit covers it — the experts are sparse enough that offloading most of them to system "
            "RAM costs much less speed than the size suggests.",
            mac_tip="Needs ~175GB — exceeds every MacBook Pro; a Mac Studio (M3 Ultra with 192GB+ "
            "unified memory) is required.",
            min_memory=192,
            memory=192,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-q5-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-Q5_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-Q5_K_XL/Qwen3.8-Flash-Next-UD-Q5_K_XL-00001-of-00006.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-Q5_K_XL",
            size_hint="158 GB",
            gpu_tip="~164GB total at 128K context. A 16GB GPU (e.g. RTX 4080) keeps the always-on "
            "layers fast, with llama.cpp's MoE offloading spreading the experts across a 192GB "
            "DDR5 kit.",
            mac_tip="Needs ~164GB — exceeds every MacBook Pro; a Mac Studio (M3 Ultra with 192GB+ "
            "unified memory) is required.",
            min_memory=192,
            memory=192,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-q4-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="111 GB",
            gpu_tip="~117GB total at 128K context — the sweet spot of this family. A 16GB GPU "
            "(e.g. RTX 4070 Ti Super) plus a 128GB DDR5 kit is enough, and because only 12 of the "
            "48 layers hold a real KV cache, pushing the context out costs very little on top.",
            mac_tip="Needs ~117GB — fits a 128GB MacBook Pro (M4 Max or M5 Max), with modest "
            "headroom to spare.",
            min_memory=128,
            memory=128,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-iq4-xs",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-IQ4_XS by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-IQ4_XS",
            size_hint="93.7 GB",
            gpu_tip="~99GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) plus a 128GB DDR5 "
            "kit covers it comfortably; a 96GB kit leaves nothing for the operating system.",
            mac_tip="Needs ~99GB — a 96GB MacBook Pro is just short once macOS takes its share; a "
            "128GB M4 Max or M5 Max fits it comfortably.",
            min_memory=96,
            memory=128,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-q3-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-Q3_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-Q3_K_XL/Qwen3.8-Flash-Next-UD-Q3_K_XL-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-Q3_K_XL",
            size_hint="90 GB",
            gpu_tip="~96GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 128GB DDR5 "
            "kit is the comfortable pairing — llama.cpp's MoE expert offloading does the heavy "
            "lifting here, so a bigger card buys less than it usually would.",
            mac_tip="Needs ~96GB — a 96GB MacBook Pro is right at the edge; a 128GB M4 Max or M5 "
            "Max is the safer configuration.",
            min_memory=96,
            memory=128,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-iq3-xxs",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-IQ3_XXS by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-IQ3_XXS/Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-IQ3_XXS",
            size_hint="82 GB",
            gpu_tip="~88GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 96GB DDR5 kit "
            "is enough, with llama.cpp's MoE expert offloading covering the rest.",
            mac_tip="Needs ~88GB — fits a 96GB MacBook Pro (M4 Max or M5 Max) with a little room "
            "to spare.",
            min_memory=96,
            memory=96,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-q2-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-Q2_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-Q2_K_XL",
            size_hint="78.9 GB",
            gpu_tip="~85GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 96GB DDR5 kit "
            "covers it — the experts are sparse enough that most of them can sit in system RAM "
            "without much speed cost.",
            mac_tip="Needs ~85GB — fits a 96GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=96,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-iq1-m",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-IQ1_M by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-IQ1_M/Qwen3.8-Flash-Next-UD-IQ1_M-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-IQ1_M",
            size_hint="74.5 GB",
            gpu_tip="~80GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 96GB DDR5 kit "
            "is enough, with llama.cpp's MoE offloading absorbing the rest.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=96,
            llamacpp_version=10829,
        ),
        LocalLLMEntry(
            name="unsloth-qwen38-flash-next-ud-iq1-s",
            kind="hardcoded_hf",
            description="Qwen 3.8 Flash Next UD-IQ1_S by Unsloth",
            repo_id="unsloth/Qwen3.8-Flash-Next-GGUF",
            filename="UD-IQ1_S/Qwen3.8-Flash-Next-UD-IQ1_S-00001-of-00003.gguf",
            context_window=262_144,
            knobs=SHARED_KNOBS + (QWEN4EXP_CONTEXT_KNOB,),
            base_llm="Qwen38-Flash-Next",
            llm_author="Alibaba Cloud",
            license_name=_LICENSE_NAME,
            license_url=_LICENSE_URL,
            quant_author="Unsloth",
            quant_type="UD-IQ1_S",
            size_hint="72.5 GB",
            gpu_tip="~78GB total at 128K context — the smallest Qwen3.8-Flash-Next build, and "
            "still a 125B model. An 8GB GPU (e.g. RTX 3060 Ti) plus a 96GB DDR5 kit runs it via "
            "llama.cpp's MoE expert offloading.",
            mac_tip="Needs ~78GB — fits a 96GB MacBook Pro (M4 Max or M5 Max) comfortably; a 64GB "
            "configuration is not enough.",
            min_memory=96,
            memory=96,
            llamacpp_version=10829,
        ),
    ]
