"""Hardcoded Laguna-S-2.1 GGUF catalog entries.

Every entry ships the same six flavors: the shared ``default`` launch config,
plus five sampling presets (:func:`_quant_flavors`) that move exactly one of
two axes — how hard the tail is truncated, and how low the temperature is.
The presets differ from ``default`` only in sampling flags; the KV-cache,
context and offload args are identical, since switching flavors *replaces*
``llama_args`` wholesale rather than layering on top of it
(:class:`~kodo.llms.local_registry.LlamaFlavor`).

The values are **uniform across all 20 quants** — a preset name means the same
numbers on ``UD-Q8_K_XL`` as on ``UD-IQ1_S``. An earlier revision tiered them
by quantization severity; that was speculative, unmeasured, and produced a
temperature (``0.1``) low enough to be a downgrade in practice, so the tiering
was removed rather than re-guessed. Which preset suits which quant is guidance
in ``doc/QUANT_SAMPLING.md`` §4, not something baked into the values.

**No preset enables DRY, or any other repetition penalty.** Repetition control
at the sampler level is structurally incompatible with agentic work: DRY
penalises reproducing a token sequence that is already in context, which is
exactly what quoting back an attachment UUID, a file path, or an identifier
requires. It broke ``read_attachment`` in practice. Loop detection is handled
upstream instead, by the watchdog's
:class:`~kodo.runtime._cyclic_thinking.CyclicThinkingDetector` and the
tool-call cycle detectors (doc/STUCK_DETECTION.md §2.7/§2.10). See
``doc/QUANT_SAMPLING.md`` §3f.

``doc/QUANT_SAMPLING.md`` is the reasoning behind the numbers. Every value
here also sits inside its ``sensible_minimum``/``sensible_maximum`` band
(doc/SAMPLING.md §8d) so that copying a preset into a custom flavor never
lands the user on a flagged field they then have to argue with.
"""

from __future__ import annotations

from ._types import LlamaFlavor, LocalLLMEntry

#: Launch args every Laguna flavor shares — byte-identical to
#: :meth:`LlamaFlavor.make_default_kv_q8`'s, so the five sampling presets
#: differ from ``default`` *only* in their sampling flags. Always splatted
#: into a fresh dict; never handed to a flavor directly.
_BASE_ARGS: dict[str, str] = {
    "--cache-type-k": "q8_0",
    "--cache-type-v": "q8_0",
    "--ctx-size": "0",
    "--n-gpu-layers": "-1",
    "--reasoning-format": "auto",
    "--jinja": "",
}

#: Sampling flags shared by every preset, on top of :data:`_BASE_ARGS`.
#: All three are neutral/off values, set **explicitly** rather than left to
#: llama.cpp's defaults (``top_k 40``, ``top_p 0.95``) so that each preset's
#: remaining flags are the whole story: ``min_p`` — plus ``top_n_sigma`` in
#: the strongest preset — is the only truncation stage in play, and no
#: repetition penalty is active. Exempt from the §8d ⚠ as neutral values.
_SAMPLING_OFF: dict[str, str] = {
    "--top-k": "0",
    "--top-p": "1.0",
    "--repeat-penalty": "1.0",
}

#: The five presets, as ``(id_suffix, name, description, sampling_args)``.
#: Ordered mildest-first within each axis, tail culling before temperature —
#: this is the order they appear in the flavor dropdown, after ``default``.
#:
#: The three culling presets all sit at llama.cpp's own default temperature
#: (``0.8``), so a comparison between them isolates truncation; the two
#: temperature presets all sit at the mildest culling (``min_p 0.05``), so a
#: comparison between *those* isolates temperature. Keeping one axis fixed per
#: group is the point of the layout — a preset that moved both at once could
#: not tell you which one mattered.
_PRESETS: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    (
        "light-tail-cull",
        "Light tail cull",
        "llama.cpp's own default temperature, with min-p as the only truncation "
        "stage: a token needs 5% of the top token's probability to survive, which "
        "is enough to cut the noise floor a 4-bit-and-below quant leaves in the "
        "tail. The mildest preset — start here and tighten only if you see a "
        "problem.",
        {"--temp": "0.8", "--min-p": "0.05"},
    ),
    (
        "medium-tail-cull",
        "Medium tail cull",
        "The same default temperature with a tighter noise floor (min-p 0.08), "
        "for a quant that produces the occasional wrong-but-plausible token. "
        "Reach for this before reaching for a lower temperature — it removes the "
        "bad candidates rather than merely making them less likely.",
        {"--temp": "0.8", "--min-p": "0.08"},
    ),
    (
        "strong-tail-cull",
        "Strong tail cull",
        "The most aggressive truncation: min-p 0.12 plus top-n-sigma 1.0, which "
        "cuts in logit space — the units quantization error is actually in — and "
        "is temperature-invariant, so it does not re-tune itself if you change "
        "--temp. For heavily quantized builds that still wander under medium "
        "culling.",
        {"--temp": "0.8", "--min-p": "0.12", "--top-nsigma": "1.0"},
    ),
    (
        "low-temperature",
        "Low temperature",
        "Keeps the mildest culling and lowers temperature to 0.3 instead. "
        "Temperature scales the quantization error along with the signal, so "
        "lowering it attenuates the noise floor rather than truncating it. The "
        "right first move when format correctness is what's failing — JSON "
        "tool-call arguments, strict syntax, an exact identifier copied from "
        "context.",
        {"--temp": "0.3", "--min-p": "0.05"},
    ),
    (
        "near-greedy",
        "Near-greedy",
        "Almost deterministic: temperature 0.05, at which a token essentially "
        "cannot win unless it was already the top candidate. Maximum format "
        "reliability, at the cost of variety — and of any chance to escape a bad "
        "opening token by retrying, since the output barely varies between runs.",
        {"--temp": "0.05", "--min-p": "0.02"},
    ),
)


def _quant_flavors(entry_name: str) -> tuple[LlamaFlavor, ...]:
    """``default`` plus the five sampling presets for *entry_name*.

    Flavor ids are ``<entry_name>-<suffix>`` for each :data:`_PRESETS` entry,
    following the ``<entry-name>-<slug>`` convention the 512K/1M context
    flavors already use. All are ``platform=BOTH`` and leave
    ``min_ram``/``min_vram`` at ``0`` — they change no memory-relevant arg, so
    a preset is launchable exactly wherever ``default`` is, and the entry's
    own ``min_memory``/``memory`` remain the only hardware gate.

    Takes no tier argument: every Laguna quant gets identical preset values,
    deliberately (see the module docstring).

    Args:
        entry_name: The :class:`LocalLLMEntry` name these flavors attach to.

    Returns:
        tuple[LlamaFlavor, ...]: Six flavors, ``default`` first.
    """
    return (LlamaFlavor.make_default_kv_q8(),) + tuple(
        LlamaFlavor(
            id=f"{entry_name}-{suffix}",
            name=name,
            description=description,
            llama_args={**_BASE_ARGS, **_SAMPLING_OFF, **sampling},
        )
        for suffix, name, description, sampling in _PRESETS
    )


def laguna_s_21_entries() -> list[LocalLLMEntry]:
    return [
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q8-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q8_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q8_K_XL/Laguna-S-2.1-UD-Q8_K_XL-00001-of-00004.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q8-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q8-0"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q6-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q6-k"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q5-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q5-k-m"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q5-k-s"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-Q5_K_S",
            size_hint="82.7 GB",
            gpu_tip="~110GB total at 128K context. A 16GB GPU (e.g. RTX 4070 Ti Super) keeps the "
            "shared layers fast, and llama.cpp's MoE offloading covers the rest with a 128GB DDR5 "
            "kit.",
            mac_tip="Needs ~110GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q4-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q4_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q4_K_XL/Laguna-S-2.1-UD-Q4_K_XL-00001-of-00003.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q4-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-Q4_K_XL",
            size_hint="73.4 GB",
            gpu_tip="~105GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the "
            "shared layers fast while llama.cpp's MoE offloading absorbs the rest into a 128GB "
            "DDR5 kit.",
            mac_tip="Needs ~105GB — fits a 128GB MacBook Pro (M4 Max or M5 Max) comfortably.",
            min_memory=96,
            memory=128,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-mxfp4-moe",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 MXFP4_MOE by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="MXFP4_MOE/Laguna-S-2.1-MXFP4_MOE-00001-of-00003.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-mxfp4-moe"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q4-k-s"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq4-nl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-IQ4_NL",
            size_hint="58.7 GB",
            gpu_tip="~80GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the "
            "shared layers fast, and llama.cpp's MoE expert offloading covers the rest with a "
            "96GB DDR5 kit.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=64,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq4-xs",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ4_XS by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-IQ4_XS/Laguna-S-2.1-UD-IQ4_XS-00001-of-00003.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq4-xs"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-IQ4_XS",
            size_hint="57.6 GB",
            gpu_tip="~80GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) keeps the shared "
            "layers fast, and llama.cpp's MoE expert offloading covers the rest with a 96GB DDR5 "
            "kit.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=64,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q3-k-xl",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q3_K_XL by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q3_K_XL/Laguna-S-2.1-UD-Q3_K_XL-00001-of-00003.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q3-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-Q3_K_XL",
            size_hint="54.1 GB",
            gpu_tip="~80GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 96GB DDR5 "
            "kit is enough — llama.cpp's MoE expert offloading keeps this large model fast "
            "without a workstation card.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=64,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-q3-k-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-Q3_K_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="UD-Q3_K_M/Laguna-S-2.1-UD-Q3_K_M-00001-of-00003.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q3-k-m"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-Q3_K_M",
            size_hint="54 GB",
            gpu_tip="~80GB total at 128K context. Nearly identical footprint to the UD-Q3_K_XL "
            "build: an 8GB GPU (e.g. RTX 3060 Ti) plus a 96GB DDR5 kit covers it via llama.cpp's "
            "MoE expert offloading.",
            mac_tip="Needs ~80GB — fits a 96GB MacBook Pro configuration comfortably (M4 Max or "
            "M5 Max).",
            min_memory=64,
            memory=96,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq3-s",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ3_S by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ3_S.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq3-s"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq3-xxs"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-q2-k-xl"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-Q2_K_XL",
            size_hint="39.7 GB",
            gpu_tip="~55GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 64GB DDR5 kit "
            "is enough — llama.cpp's MoE expert offloading keeps this fast on modest hardware.",
            mac_tip="Needs ~55GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq2-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ2_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ2_M.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq2-m"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
            quant_author="Unsloth",
            quant_type="UD-IQ2_M",
            size_hint="37.3 GB",
            gpu_tip="~53GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 64GB DDR5 kit "
            "covers it comfortably, with llama.cpp's MoE offloading doing the heavy lifting.",
            mac_tip="Needs ~53GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
            min_memory=48,
            memory=64,
            llamacpp_version=10087,
        ),
        LocalLLMEntry(
            name="unsloth-laguna-s-2-1-iq1-m",
            kind="hardcoded_hf",
            description="Laguna-S-2.1 UD-IQ1_M by Unsloth",
            repo_id="unsloth/Laguna-S-2.1-GGUF",
            filename="Laguna-S-2.1-UD-IQ1_M.gguf",
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq1-m"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
            flavors=_quant_flavors("unsloth-laguna-s-2-1-iq1-s"),
            context_window=262_144,
            base_llm="Laguna-S-2.1",
            llm_author="Poolside",
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
