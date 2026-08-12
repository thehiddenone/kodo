"""Laguna-S-2.1 GGUF catalog entries.

Every entry offers the shared knobs plus one private one:
:data:`LAGUNA_CONTEXT_KNOB`, reaching 512K or 1M tokens on top of a 256K
default.

This module used to carry a lot more. Each of its twenty quants shipped eight
predefined flavors — ``default``, five fixed sampling presets, and two context
variants — which between them enumerated the handful of combinations someone
had thought to write down. All eight are gone: the sampling presets became the
shared :data:`~kodo.llms.local_registry._knobs_shared.TAIL_CULLING_KNOB` and
:data:`~kodo.llms.local_registry._knobs_shared.TEMPERATURE_KNOB` (two
independent axes now, so "strong culling at a low temperature" — a combination
no preset offered — is simply two dropdowns), and the context variants became
the knob below. ``doc/QUANT_SAMPLING.md`` still explains which sampling
settings suit which quant; it is guidance, not something baked into per-quant
values, and never was (an earlier revision tiered the presets by quantization
severity, which was speculative and unmeasured, and was removed).

Unlike the Qwen families (:mod:`._knobs_qwen`) — whose GGUFs default to their
real trained length and only need YaRN when explicitly extended — every
Unsloth Laguna-S-2.1 quant ships with YaRN rope-scaling *already baked into
its own GGUF metadata*, defaulting to 256K (rope-scale 32 over the model's
true 8K training context) with no launch args required. Laguna never actually
runs at that unscaled 8K length, so :func:`~._knobs_context.make_yarn_context_knob`
— which always offers an args-free "native" option as the default — does not
fit this model and is not used here. ``LAGUNA_CONTEXT_KNOB`` is built by hand
instead: its default option ("256K") writes no args, same as the shared
helper's "native" slot does elsewhere, but represents the GGUF's own
pre-scaled default rather than an unscaled floor. The 512K and 1M options
still write explicit YaRN args computed off the real native context of
:data:`_NATIVE_CONTEXT` tokens (8192), same recipe as the Qwen knobs, and key
their ``--override-kv`` metadata override on ``laguna.context_length``,
Laguna-S-2.1's architecture name.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob
from ._knobs_shared import SHARED_KNOBS
from ._types import LocalLLMEntry

#: Laguna-S-2.1's true trained context length — the ``--yarn-orig-ctx`` and
#: the divisor for each extended option's ``--rope-scale`` (64.0 at 512K,
#: 128.0 at 1M) below. Not reachable as a knob option itself: every quant's
#: GGUF metadata already applies YaRN scaling by default (factor 32, to
#: 256K), so this model never actually runs at its unscaled native length.
_NATIVE_CONTEXT = 8192

#: Laguna-S-2.1's long-context knob. ``laguna`` is the architecture key the
#: GGUF records its context length under; it is model knowledge, not something
#: derived from the entry name. Default option writes no args at all, relying
#: on the GGUF's own baked-in 256K/×32 YaRN scaling — see the module
#: docstring for why this can't use the shared ``make_yarn_context_knob``.
LAGUNA_CONTEXT_KNOB = LlamaKnob(
    id="context-laguna",
    name="Context window",
    description=(
        "How many tokens the model can hold at once. Every Laguna-S-2.1 quant ships "
        "pre-scaled: the GGUF's own metadata defaults to 256K via YaRN rope-scaling "
        "(factor 32 over the model's 8K training context), so this option needs no launch "
        "args. The model never runs at its unscaled 8K length. Going further trades more "
        "accuracy for reach and grows the KV cache proportionally."
    ),
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(
            id="256k",
            name="256K (default)",
            description=(
                "The context every Laguna-S-2.1 quant ships pre-scaled to, via YaRN "
                "rope-scaling baked into the GGUF's own metadata (factor 32 over the 8K "
                "training context) — no launch args needed. The right choice unless you "
                "genuinely need to hold more than this at once."
            ),
        ),
        KnobOption(
            id="512k",
            name="512K (YaRN-extended)",
            description=(
                "Stretches the model's 8K training context to 512K tokens with YaRN "
                "rope-scaling. Recall and reasoning degrade as you go further past the "
                "native length, and the KV cache grows in proportion — at this size it is "
                "usually only practical on a machine with one large unified memory pool "
                "(Apple Silicon), not on a discrete GPU plus system RAM."
            ),
            llama_args={
                "--ctx-size": "524288",
                "--rope-scaling": "yarn",
                "--rope-scale": "64.0",
                "--yarn-orig-ctx": str(_NATIVE_CONTEXT),
                "--override-kv": "laguna.context_length=int:524288",
            },
        ),
        KnobOption(
            id="1m",
            name="1M (YaRN-extended)",
            description=(
                "Stretches the model's 8K training context to 1M tokens with YaRN "
                "rope-scaling. Recall and reasoning degrade further than at 512K, and the "
                "KV cache grows in proportion — practical only on a machine with one large "
                "unified memory pool (Apple Silicon), not on a discrete GPU plus system RAM."
            ),
            llama_args={
                "--ctx-size": "1048576",
                "--rope-scaling": "yarn",
                "--rope-scale": "128.0",
                "--yarn-orig-ctx": str(_NATIVE_CONTEXT),
                "--override-kv": "laguna.context_length=int:1048576",
            },
        ),
    ),
    default_option="256k",
)


def laguna_s_21_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q8-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q8_K_XL/Laguna-S-2.1-UD-Q8_K_XL-00001-of-00004.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q8_K_XL",
            size_hint="128 GB",
            gpu_tip="~165GB total at 128K context — the biggest Laguna-S-2.1 build. It's a sparse "
            "MoE model, so a 16GB GPU (e.g. RTX 4080) still handles the always-on shared layers at "
            "full speed while llama.cpp offloads the mostly-idle experts to a 192GB DDR5 "
            "workstation kit.",
            mac_tip="Needs ~165GB — beyond even the largest 128GB MacBook Pro; a Mac Studio "
            "(M3 Ultra with 192GB+ unified memory) or a Linux/Windows workstation is the "
            "realistic option.",
            min_memory=192,
            memory=192,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q8-0",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q8_0 by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Q8_0/Laguna-S-2.1-Q8_0-00001-of-00004.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q8_0",
            size_hint="125 GB",
            gpu_tip="~165GB total at 128K context. Same MoE-offload story as the UD-Q8_K_XL build: "
            "a 16GB GPU (e.g. RTX 5080) keeps the shared layers fast, with the offloaded experts "
            "spread across a 192GB DDR5 workstation kit.",
            mac_tip="Needs ~165GB — exceeds a 128GB MacBook Pro; a Mac Studio (M3 Ultra) "
            "or a workstation-class machine is required.",
            min_memory=192,
            memory=192,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q6-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q6_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q6_K_XL/Laguna-S-2.1-UD-Q6_K_XL-00001-of-00004.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q6_K_XL",
            size_hint="107 GB",
            gpu_tip="~120GB total at 128K context. A 16GB GPU (e.g. RTX 4070 Ti Super) handles the "
            "shared layers, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 kit.",
            mac_tip="Needs ~120GB — right at the edge of a 128GB MacBook Pro (M4 Max or M5 Max).",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q6-k",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q6_K by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q6_K/Laguna-S-2.1-UD-Q6_K-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q6_K",
            size_hint="97.9 GB",
            gpu_tip="~115GB total at 128K context. A 16GB GPU (e.g. RTX 4070 Ti Super) handles the "
            "shared layers, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 kit.",
            mac_tip="Needs ~115GB — fits a 128GB MacBook Pro (M4 Max or M5 Max), with little "
            "headroom to spare.",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q5-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q5_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q5_K_XL/Laguna-S-2.1-UD-Q5_K_XL-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_XL",
            size_hint="88.1 GB",
            gpu_tip="~110GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) keeps the shared "
            "layers fast, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 kit.",
            mac_tip="Needs ~110GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q5-k-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q5_K_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q5_K_M/Laguna-S-2.1-UD-Q5_K_M-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_M",
            size_hint="87.9 GB",
            gpu_tip="~110GB total at 128K context. Nearly identical footprint to the UD-Q5_K_XL "
            "build: a 16GB GPU (e.g. RTX 5070 Ti) handles the shared layers, with llama.cpp's MoE "
            "offloading covering the rest via a 128GB DDR5 kit.",
            mac_tip="Needs ~110GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q5-k-s",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q5_K_S by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q5_K_S/Laguna-S-2.1-UD-Q5_K_S-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_S",
            size_hint="82.7 GB",
            gpu_tip="~110GB total at 128K context. A 16GB GPU (e.g. RTX 4070 Ti Super) keeps the "
            "shared layers fast, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 "
            "kit.",
            mac_tip="Needs ~110GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q4-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q4_K_XL/Laguna-S-2.1-UD-Q4_K_XL-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="73.4 GB",
            gpu_tip="~105GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the "
            "shared layers fast while llama.cpp's MoE offloading absorbs the rest into a 128GB "
            "DDR5 kit.",
            mac_tip="Needs ~105GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=128,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-mxfp4-moe",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 MXFP4_MOE by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="MXFP4_MOE/Laguna-S-2.1-MXFP4_MOE-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="MXFP4_MOE",
            size_hint="71.1 GB",
            gpu_tip="~105GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) handles the "
            "always-on layers, and ~128GB of DDR5 system RAM covers the offloaded MXFP4 experts — "
            "llama.cpp's native MXFP4 support keeps this close to a full-VRAM fit.",
            mac_tip="Needs ~105GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q4-k-s",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q4_K_S by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q4_K_S/Laguna-S-2.1-UD-Q4_K_S-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_S",
            size_hint="68.6 GB",
            gpu_tip="~105GB total at 128K context. A 16GB GPU (e.g. RTX 4070) handles the shared "
            "layers, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 kit.",
            mac_tip="Needs ~105GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq4-nl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ4_NL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-IQ4_NL/Laguna-S-2.1-UD-IQ4_NL-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ4_NL",
            size_hint="58.7 GB",
            gpu_tip="~80GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the "
            "shared layers fast, and llama.cpp's MoE expert offloading covers the rest with a "
            "96GB DDR5 kit.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=96,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq4-xs",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ4_XS by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-IQ4_XS/Laguna-S-2.1-UD-IQ4_XS-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ4_XS",
            size_hint="57.6 GB",
            gpu_tip="~80GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) keeps the shared "
            "layers fast, and llama.cpp's MoE expert offloading covers the rest with a 96GB DDR5 "
            "kit.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=96,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q3-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q3_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q3_K_XL/Laguna-S-2.1-UD-Q3_K_XL-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q3_K_XL",
            size_hint="54.1 GB",
            gpu_tip="~80GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 96GB DDR5 "
            "kit is enough — llama.cpp's MoE expert offloading keeps this large model fast "
            "without a workstation card.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=96,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q3-k-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q3_K_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q3_K_M/Laguna-S-2.1-UD-Q3_K_M-00001-of-00003.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q3_K_M",
            size_hint="54 GB",
            gpu_tip="~80GB total at 128K context. Nearly identical footprint to the UD-Q3_K_XL "
            "build: an 8GB GPU (e.g. RTX 3060 Ti) plus a 96GB DDR5 kit covers it via llama.cpp's "
            "MoE expert offloading.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=96,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq3-s",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ3_S by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ3_S.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ3_S",
            size_hint="48.4 GB",
            gpu_tip="~58GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 64GB DDR5 "
            "kit is enough, with llama.cpp's MoE offloading handling the rest.",
            mac_tip="Needs ~58GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=64,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq3-xxs",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ3_XXS by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ3_XXS.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ3_XXS",
            size_hint="44.3 GB",
            gpu_tip="~56GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 64GB DDR5 kit "
            "covers it, with llama.cpp's MoE offloading barely costing any speed.",
            mac_tip="Needs ~56GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=64,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q2-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q2_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-Q2_K_XL.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-Q2_K_XL",
            size_hint="39.7 GB",
            gpu_tip="~55GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 64GB DDR5 kit "
            "is enough — llama.cpp's MoE expert offloading keeps this fast on modest hardware.",
            mac_tip="Needs ~55GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=64,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq2-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ2_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ2_M.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ2_M",
            size_hint="37.3 GB",
            gpu_tip="~53GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 64GB DDR5 kit "
            "covers it comfortably, with llama.cpp's MoE offloading doing the heavy lifting.",
            mac_tip="Needs ~53GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=64,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq1-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ1_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ1_M.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ1_M",
            size_hint="35.6 GB",
            gpu_tip="~43GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 48GB DDR5 kit "
            "is enough, with llama.cpp's MoE offloading filling in the gap.",
            mac_tip="Needs ~43GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq1-s",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ1_S by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ1_S.gguf",
            knobs=SHARED_KNOBS + (LAGUNA_CONTEXT_KNOB,),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            license_name="OpenMDW-1.1",
            license_url="https://openmdw.ai/license/1-1/",
            quant_author="Unsloth",
            quant_type="UD-IQ1_MS",
            size_hint="33.8 GB",
            gpu_tip="~42GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 48GB DDR5 "
            "kit is enough — the smallest of the Laguna-S-2.1 builds, and llama.cpp's MoE "
            "offloading keeps it fast even on modest hardware.",
            mac_tip="Needs ~42GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=48,
            llamacpp_version=10087,
        ),
    ]
