"""The Multi-Token Prediction (MTP) speculative-decoding knob.

Unlike the YaRN context knobs (:mod:`._knobs_context`, :mod:`._knobs_qwen`),
this needs no per-architecture ``make_*_knob`` factory: ``--spec-type
draft-mtp`` is the same flag regardless of whether the GGUF's
``general.architecture`` is ``qwen35`` or ``qwen35moe``, so one knob object
is reused by every entry that qualifies. "Qualifies" is decided entirely by
:attr:`~kodo.llms.local_registry.LocalLLMEntry.mtp_supported`, which in turn
is decided entirely by whether that specific GGUF's header actually declares
an ``<arch>.nextn_predict_layers`` count — never by the entry's or repo's
name. Verified this way (range-fetching a quant file's header and grepping
for the metadata key directly, not trusting model cards or ``-MTP-GGUF``
branding), as of 2026-09-22:

- **Has it**: Ornith15-35B-A3B (all entries), Qwen35-9B (all entries),
  Qwen36-35B-A3B (all entries), Qwen38-27B (all entries, confirmed down to
  its most aggressive quant), and Qwen36-27B's four
  ``unsloth/Qwen3.6-27B-MTP-GGUF``-backed entries.
- **Doesn't**, despite looking like a candidate: Qwen36-27B's
  ``atomicchat-qwen36-27b-q8`` entry — a different (community) quant of the
  same model that drops the MTP tensors even though its sibling entries'
  repo keeps them; Ornith15-9B and Ornith10-9B/-35B-A3B (no MTP layers in
  the underlying weights); Qwen38-Flash-Next (has the layers upstream, but
  llama.cpp's draft-head support for its ``qwen4exp`` architecture is an
  open PR, so kodo deliberately launches no ``--spec-type`` regardless);
  Qwen3-Coder-Next-80B (its base model has no MTP head at all, unlike the
  general Qwen3-Next-80B-A3B releases).

``_catalog._validate_catalog()`` enforces that ``mtp_supported`` and this
knob's presence in ``entry.knobs`` never disagree, so a future addition that
gets this wrong fails at import rather than at launch.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob

__all__ = [
    "MTP_SPEC_DECODE_KNOB",
]

#: Speculative decoding off a model's own MTP layers. ``--spec-type
#: draft-mtp`` tells llama.cpp to use the MTP heads baked into the GGUF as
#: the draft model, so unlike the usual speculative-decoding setup there is
#: no second model to download or configure. Verified decoding, so the
#: sampled distribution is unchanged; the only cost is the extra compute
#: when a draft token is rejected.
#:
#: Defaults to ``off``: a user who wants the speedup should opt into it
#: knowingly. Draft-layer quantization quality varies by quant author and
#: rung (e.g. bartowski stores Ornith 1.5's MTP layers at Q4_0 in every
#: imatrix quant except Q8_0), so draft quality is not uniform across an
#: entry's own quant ladder — that is an intended trade, not a defect.
MTP_SPEC_DECODE_KNOB = LlamaKnob(
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
