"""Ornith15-35B-A3B GGUF catalog entries.

The sparse-MoE member of the Ornith 1.5 family, and architecturally the
direct successor to Ornith 1.0 35B-A3B: the same ``qwen35moe`` GGUF
architecture key, the same 262144-token native context, and the same
attention shape (2 KV heads, 256-wide keys and values, full attention on
every fourth layer), so it reuses
:data:`~._knobs_qwen.QWEN_MOE_CONTEXT_KNOB` unchanged and its KV-cache
footprint at 128K context matches the 1.0 entries' (~17GB at the default
q8_0 cache, ~34GB at f16).

Two things separate it from the 1.0 family:

- **MTP.** These quants carry Multi-Token Prediction layers (the GGUF's
  ``qwen35moe.nextn_predict_layers``), which llama.cpp can drive as a
  built-in draft model for speculative decoding. That is what
  :data:`ORNITH15_MTP_KNOB` below exposes; it is declared here rather than
  in :mod:`._knobs_shared` because no other model in the catalog ships MTP
  layers, and ``--spec-type`` on a GGUF without them is not a no-op.
- **Vision.** The upstream model is multimodal and bartowski's repo ships
  ``mmproj-Ornith-1.5-35B-A3B-{f16,bf16}.gguf`` companions.
  :class:`~._types.LocalLLMEntry` has no field to declare an mmproj
  companion, so every entry here is registered text-only and the projector
  files are simply not downloaded. Wiring mmproj through the catalog,
  the download manager and the ``--mmproj`` launch arg is a separate piece
  of work.

The quant ladder mirrors the five rungs the Ornith 1.0 families ship
(BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M), extended downward with three smaller builds
so a 32-36GB machine can run the model at all.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob
from ._knobs_qwen import QWEN_MOE_CONTEXT_KNOB
from ._knobs_shared import KV_CACHE_F16_DEFAULT, SHARED_KNOBS
from ._types import LocalLLMEntry

#: Speculative decoding off the model's own MTP layers. Unique to this family
#: — ``--spec-type draft-mtp`` tells llama.cpp to use the MTP heads baked into
#: the GGUF as the draft model, so unlike the usual speculative-decoding setup
#: there is no second model to download or configure. Verified decoding, so
#: the sampled distribution is unchanged; the only cost is the extra compute
#: when a draft token is rejected.
#:
#: Defaults to ``off``: no other entry in the catalog launches ``--spec-type``,
#: so this is the one place the flag is exercised at all, and a user who wants
#: the speedup should opt into it knowingly. Note that bartowski stores the
#: MTP layers at Q4_0 in every imatrix quant except Q8_0 (imatrix calibration
#: does not exercise them, and Q4_0's speed is what makes drafting pay off),
#: so draft quality is deliberately low across most of the ladder — that is
#: the intended trade, not a defect.
ORNITH15_MTP_KNOB = LlamaKnob(
    id="spec-decoding-mtp",
    name="Speculative decoding (MTP)",
    description=(
        "Uses the Multi-Token Prediction layers built into this model's GGUF as a draft "
        "model, letting llama.cpp guess several tokens ahead and verify them in one pass. "
        "Generation gets faster on accepted guesses and the output is unchanged either way, "
        "since every drafted token is verified against the full model. There is nothing extra "
        "to download — the draft layers ship inside the quant."
    ),
    kind=KnobKind.CHECKBOX,
    options=(
        KnobOption(
            id="off",
            name="Off",
            description=(
                "Plain single-token decoding. Pick this if you see instability, or if you are "
                "comparing timings against another model that has no MTP layers."
            ),
        ),
        KnobOption(
            id="on",
            name="On",
            description=(
                "Draft with the model's own MTP layers. Faster on predictable text (code, "
                "structured output, long tool-call arguments) and roughly neutral on text where "
                "few guesses are accepted."
            ),
            llama_args={"--spec-type": "draft-mtp"},
        ),
    ),
    default_option="off",
)

#: Every entry in this family offers the shared knobs, the MoE YaRN context
#: knob, and the MTP knob above.
_ORNITH15_35B_KNOBS = SHARED_KNOBS + (QWEN_MOE_CONTEXT_KNOB, ORNITH15_MTP_KNOB)


def ornith15_35b_a3b_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-bf16",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B BF16 by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-bf16/Ornith-1.5-35B-A3B-bf16-00001-of-00002.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            knob_defaults=KV_CACHE_F16_DEFAULT,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="BF16",
            size_hint="71.1 GB",
            gpu_tip="~105GB total at 128K context — the heaviest way to run Ornith 1.5, and the "
            "only build here that arrives as two files. It is a sparse 3B-active MoE though, so "
            "most of those weights are idle on any given token: a 16GB GPU (e.g. RTX 4080) holds "
            "the always-on attention and shared layers at full speed while llama.cpp offloads "
            "the inactive experts to a 128GB DDR5 kit. If speed matters more than bit-perfect "
            "precision, the quantized builds below get close at a fraction of the memory.",
            mac_tip="Needs ~105GB — tight on a 128GB M4 Max or M5 Max.",
            min_memory=128,
            memory=128,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q8-0",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q8_0 by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q8_0.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q8_0",
            size_hint="37.8 GB",
            gpu_tip="~55GB total at 128K context, and the only rung whose MTP draft layers are "
            "not themselves quantized to Q4_0. A 16GB GPU (e.g. RTX 4060 Ti 16GB) plus ~48GB of "
            "DDR5 system RAM covers it via llama.cpp's MoE offloading — no need for the BF16 "
            "build's 128GB ask.",
            mac_tip="Needs ~55GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is the "
            "safe choice.",
            min_memory=64,
            memory=96,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q6-k",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q6_K by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q6_K.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q6_K",
            size_hint="30.5 GB",
            gpu_tip="~47GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus ~48GB of "
            "DDR5 system RAM handles it comfortably — the sparse MoE layout means the experts "
            "sitting in system RAM cost far less speed than offloading whole dense layers would.",
            mac_tip="Needs ~47GB — a 48GB MacBook Pro is close to its limit; a 64GB config "
            "(M4 Pro/Max or M5 Pro/Max) is safer.",
            min_memory=48,
            memory=64,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q5-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q5_K_M by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q5_K_M.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q5_K_M",
            size_hint="25.5 GB",
            gpu_tip="~42GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~48GB of DDR5 "
            "system RAM is enough — llama.cpp's expert offloading fills in the gap without "
            "needing a big card.",
            mac_tip="Needs ~42GB — fits a 64GB MacBook Pro comfortably; a 48GB config is tight.",
            min_memory=48,
            memory=64,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q4-k-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q4_K_M by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q4_K_M.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q4_K_M",
            size_hint="21.9 GB",
            gpu_tip="~39GB total at 128K context, and the rung bartowski recommends as the "
            "general-purpose default. An 8GB GPU (e.g. RX 7600) plus ~48GB of DDR5 system RAM "
            "covers the whole model via llama.cpp's expert offloading.",
            mac_tip="Needs ~39GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q3-k-xl",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q3_K_XL by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q3_K_XL.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q3_K_XL",
            size_hint="17.8 GB",
            gpu_tip="~35GB total at 128K context. Keeps the embedding and output weights at Q8_0 "
            "while dropping the rest to 3-bit, which holds quality up better than a plain Q3 "
            "build at almost the same size. An 8GB GPU plus a 32GB DDR5 kit runs it.",
            mac_tip="Needs ~35GB — fits a 36GB MacBook Pro; a 48GB config leaves real headroom.",
            min_memory=36,
            memory=48,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-iq3-m",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B IQ3_M by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-IQ3_M.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="IQ3_M",
            size_hint="17.4 GB",
            gpu_tip="~34GB total at 128K context. An i-quant: smaller than the K-quants at the "
            "same bit width and imatrix-calibrated, at the cost of more CPU work per token, so "
            "it rewards putting as many layers on the GPU as will fit. An 8GB GPU plus a 32GB "
            "DDR5 kit runs it.",
            mac_tip="Needs ~34GB — fits a 36GB MacBook Pro; a 48GB config leaves real headroom.",
            min_memory=36,
            memory=48,
            llamacpp_version=10472,
        ),
        LocalLLMEntry(
            name="bartowski-ornith15-35b-a3b-q2-k-l",
            kind="hardcoded_hf",
            description="Ornith 1.5 35B A3B Q2_K_L by bartowski",
            repo_id="bartowski/Ornith-1.5-35B-A3B-GGUF",
            filename="Ornith-1.5-35B-A3B-Q2_K_L.gguf",
            context_window=262_144,
            knobs=_ORNITH15_35B_KNOBS,
            base_llm="Ornith15-35B-A3B",
            llm_author="Ornith AI",
            license_name="MIT License",
            license_url="https://opensource.org/license/mit",
            quant_author="bartowski",
            quant_type="Q2_K_L",
            size_hint="13.6 GB",
            gpu_tip="~31GB total at 128K context — the smallest Ornith 1.5 35B build, and the "
            "one that makes a 32GB machine viable at all. Quality drops noticeably at 2-bit; "
            "keeping the embedding and output weights at Q8_0 softens that, but prefer Q3_K_XL "
            "or IQ3_M whenever the memory is there.",
            mac_tip="Needs ~31GB — a 32GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) is right at the "
            "limit; a 36GB config is safer.",
            min_memory=32,
            memory=36,
            llamacpp_version=10472,
        ),
    ]
