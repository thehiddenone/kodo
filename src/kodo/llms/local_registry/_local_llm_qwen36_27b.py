"""Qwen36-27B GGUF catalog entries.

Support for Multi-Token Prediction splits **within** this family, by quant
repo rather than by model — the first trap of its kind in this catalog, so
it's called out explicitly rather than left to be rediscovered. The four
``unsloth/Qwen3.6-27B-MTP-GGUF``-backed entries have it: their GGUF header
declares ``qwen35.nextn_predict_layers = 1`` (checked directly on the
UD-Q8_K_XL quant). The ``atomicchat-qwen36-27b-q8`` entry does not: it's a
*different* quant of the same base model, from ``AlexAtomic/qwen36-27b-GGUF``,
whose own GGUF header has no ``nextn_predict_layers`` key at all — that
quantization pipeline simply didn't retain the MTP tensors. So only the
Unsloth-backed entries list :data:`~._knobs_mtp.MTP_SPEC_DECODE_KNOB` and set
``mtp_supported=True``; the AtomicChat entry stays at the default
``mtp_supported=False`` with no MTP knob.
"""

from __future__ import annotations

from ._knobs_mtp import MTP_SPEC_DECODE_KNOB
from ._knobs_qwen import QWEN_CONTEXT_KNOB
from ._knobs_shared import SHARED_KNOBS
from ._types import LocalLLMEntry

#: The Unsloth-backed entries in this family offer the shared knobs, the
#: dense Qwen YaRN context knob, and the MTP knob; the AtomicChat entry
#: (different quant repo, verified to lack the MTP tensors) offers only the
#: first two.
_QWEN36_27B_KNOBS = SHARED_KNOBS + (QWEN_CONTEXT_KNOB,)
_QWEN36_27B_MTP_KNOBS = _QWEN36_27B_KNOBS + (MTP_SPEC_DECODE_KNOB,)


def qwen36_27b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="atomicchat-qwen36-27b-q8",
            kind="hardcoded_hf",
            description="Qwen 3.6 27B Q8_0 by AtomicChat",
            repo_id="AlexAtomic/qwen36-27b-GGUF",
            filename="qwen36-27b-Q8_0.gguf",
            context_window=262_144,
            knobs=_QWEN36_27B_KNOBS,
            base_llm="Qwen36-27B",
            llm_author="Alibaba Cloud",
            license_name="Apache License 2.0",
            license_url="https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/LICENSE",
            quant_author="AtomicChat",
            quant_type="Q8_0",
            size_hint="28.6 GB",
            gpu_tip="~43GB total at 128K context — no need to hunt for a giant workstation card. "
            "llama.cpp splits dense models layer-by-layer between GPU and CPU, so an 8GB GPU "
            "(e.g. RTX 4060) carries a solid share of the layers at full speed, with ~48GB of "
            "ordinary DDR5 system RAM covering the rest.",
            mac_tip="Needs ~43GB — comfortable on a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max); "
            "a 48GB config is tight.",
            min_memory=48,
            memory=64,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-27b-q8-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 27B UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
            filename="Qwen3.6-27B-UD-Q8_K_XL.gguf",
            context_window=262_144,
            knobs=_QWEN36_27B_MTP_KNOBS,
            mtp_supported=True,
            base_llm="Qwen36-27B",
            llm_author="Alibaba Cloud",
            license_name="Apache License 2.0",
            license_url="https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/LICENSE",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="35.8 GB",
            gpu_tip="~50GB total at 128K context — the biggest of the Qwen 3.6 27B builds, but "
            "still no reason to chase a 64GB+ card. An 8GB GPU (e.g. RTX 3060 Ti) plus ~64GB of "
            "everyday DDR5 system RAM covers it via llama.cpp's layer offloading.",
            mac_tip="Needs ~50GB — a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) is tight; a 128GB "
            "M4 Max or M5 Max gives more headroom.",
            min_memory=48,
            memory=64,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-27b-q6-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 27B UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
            filename="Qwen3.6-27B-UD-Q6_K_XL.gguf",
            context_window=262_144,
            knobs=_QWEN36_27B_MTP_KNOBS,
            mtp_supported=True,
            base_llm="Qwen36-27B",
            llm_author="Alibaba Cloud",
            license_name="Apache License 2.0",
            license_url="https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/LICENSE",
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="26.0 GB",
            gpu_tip="~40GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~48GB of DDR5 "
            "system RAM covers the whole model — llama.cpp keeps as many layers on the GPU as fit "
            "and runs the rest from RAM.",
            mac_tip="Needs ~40GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
            "a 48GB config is tight.",
            min_memory=48,
            memory=48,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-27b-q5-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 27B UD-Q5_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
            filename="Qwen3.6-27B-UD-Q5_K_XL.gguf",
            context_window=262_144,
            knobs=_QWEN36_27B_MTP_KNOBS,
            mtp_supported=True,
            base_llm="Qwen36-27B",
            llm_author="Alibaba Cloud",
            license_name="Apache License 2.0",
            license_url="https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/LICENSE",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_XL",
            size_hint="20.4 GB",
            gpu_tip="~35GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 32GB DDR5 kit "
            "is enough, with llama.cpp's layer offloading filling in the gap.",
            mac_tip="Needs ~35GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=3100,
        ),
        LocalLLMEntry(
            name="unsloth-qwen36-27b-q4-k-xl",
            kind="hardcoded_hf",
            description="Qwen 3.6 27B UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
            filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
            context_window=262_144,
            knobs=_QWEN36_27B_MTP_KNOBS,
            mtp_supported=True,
            base_llm="Qwen36-27B",
            llm_author="Alibaba Cloud",
            license_name="Apache License 2.0",
            license_url="https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/LICENSE",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="17.9 GB",
            gpu_tip="~32GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 32GB DDR5 kit "
            "covers it comfortably — well within reach of a typical gaming rig once llama.cpp "
            "splits the layers.",
            mac_tip="Needs ~32GB — fits a 32GB MacBook Pro (M4 or M5) if you trim context a bit, "
            "or a 48GB config (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=32,
            memory=36,
            llamacpp_version=3100,
        ),
    ]
