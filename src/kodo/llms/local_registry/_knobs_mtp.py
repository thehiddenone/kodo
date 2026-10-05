"""The Multi-Token Prediction (MTP) speculative-decoding knobs.

Two shapes, one per way a family can carry MTP layers:

- :data:`MTP_SPEC_DECODE_KNOB` — the **built-in** checkbox, for an entry whose
  own GGUF carries the layers and whose family ships no standalone heads.
- :func:`make_mtp_head_knob` — the **head picker** dropdown, for every entry
  of a family that ships standalone MTP heads (``mtp_sidecars.json``, see
  :mod:`._mtp_sidecars`): *Off*, *Built-in* (only when the entry's own GGUF
  has the layers) and one option per head, most precise first. It replaces the
  checkbox on those entries — both write ``--spec-type``, and two knobs on one
  entry may never own the same flag (:mod:`._knobs`). Its options depend on
  the family's data, so it is built per family rather than listed in
  :data:`~._knobs_table.KNOBS_BY_ID`, and its id names the family
  (:func:`mtp_head_knob_id`) so the wire's id-deduplicated knob table stays
  lossless.

Unlike the YaRN context knobs (:mod:`._knobs_context`, :mod:`._knobs_qwen`),
neither needs a per-architecture ``--override-kv`` target: ``--spec-type
draft-mtp`` is the same flag regardless of whether the GGUF's
``general.architecture`` is ``qwen35`` or ``qwen35moe``. Whether an entry's
own GGUF qualifies is decided entirely by
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
  the underlying weights); Qwen38-Flash-Next (48 blocks, no
  ``nextn_predict_layers`` — its MTP head ships only as standalone files,
  so it gets MTP through the head picker instead); Qwen3-Coder-Next-80B (its
  base model has no MTP head at all, unlike the general Qwen3-Next-80B-A3B
  releases).

Families with standalone heads, as of 2026-10-04: Qwen38-27B (one Q4_0 head,
on top of its built-in layers) and Qwen38-Flash-Next (BF16/Q8_0/Q4_K_M).

``_catalog_files.validate_catalog_entry`` enforces that ``mtp_supported``
agrees with the knobs an entry offers — the checkbox, or a head picker with a
*Built-in* option — so a future addition that gets this wrong fails at import
rather than at launch.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from ._knobs import KnobKind, KnobOption, LlamaKnob
from ._mtp_sidecars import MtpSidecar

__all__ = [
    "MTP_BUILTIN_OPTION_ID",
    "MTP_DRAFT_MODEL_FLAG",
    "MTP_HEAD_KNOB_ID_PREFIX",
    "MTP_SPEC_DECODE_KNOB",
    "is_mtp_head_knob",
    "make_mtp_head_knob",
    "mtp_head_knob_id",
]

#: Every head picker's id starts with this, followed by ``:<base_llm>``.
MTP_HEAD_KNOB_ID_PREFIX = "spec-decoding-mtp-head"

#: The head picker's option for drafting with the model's own MTP layers.
MTP_BUILTIN_OPTION_ID = "builtin"

#: The flag a head option points at its head file with. Its value in the knob
#: is the head's bare file name — the absolute path depends on where the head
#: was downloaded and is filled in at launch
#: (:func:`kodo.llms.llamacpp.resolve_llama_launch`). ``--model-draft`` rather
#: than the newer ``--spec-draft-model`` spelling because every llama.cpp build
#: still accepts it.
MTP_DRAFT_MODEL_FLAG = "--model-draft"

_SPEC_TYPE_MTP = {"--spec-type": "draft-mtp"}

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


def mtp_head_knob_id(base_llm: str, *, builtin: bool) -> str:
    """The head picker's id for *base_llm*'s entries.

    The family is part of the id because the options (its heads) are; the
    *builtin* flag is too, because an entry without built-in layers must not
    offer the *Built-in* option, and one id must always mean one definition.
    """
    suffix = f":{MTP_BUILTIN_OPTION_ID}" if builtin else ""
    return f"{MTP_HEAD_KNOB_ID_PREFIX}:{base_llm}{suffix}"


def is_mtp_head_knob(knob: LlamaKnob) -> bool:
    """Whether *knob* is a head picker built by :func:`make_mtp_head_knob`."""
    return knob.id.startswith(f"{MTP_HEAD_KNOB_ID_PREFIX}:")


def _head_option(sidecar: MtpSidecar) -> KnobOption:
    size = f" ({sidecar.size_hint})" if sidecar.size_hint else ""
    requirement = (
        f" Needs llama.cpp b{sidecar.llamacpp_version} or newer; on an older build the model "
        "starts without speculative decoding."
        if sidecar.llamacpp_version
        else ""
    )
    return KnobOption(
        id=sidecar.id,
        name=f"{sidecar.quant_type} head{size}",
        description=(
            f"Draft with a separate MTP head quantized at {sidecar.quant_type}, whatever quant "
            "of the model is running. Downloaded together with the model and shared by all of "
            f"its quants.{requirement}"
        ),
        llama_args={
            **_SPEC_TYPE_MTP,
            MTP_DRAFT_MODEL_FLAG: PurePosixPath(sidecar.filename).name,
        },
    )


def make_mtp_head_knob(
    base_llm: str, sidecars: tuple[MtpSidecar, ...], *, builtin: bool
) -> LlamaKnob:
    """The MTP dropdown for an entry of *base_llm*, a family that ships standalone heads.

    Args:
        base_llm: The family — part of the knob id.
        sidecars: The family's heads, already in display order (most precise
            first, as :func:`~._mtp_sidecars.load_mtp_sidecars_file` returns
            them). Must not be empty.
        builtin: Whether the entry's own GGUF carries MTP layers
            (:attr:`~kodo.llms.local_registry.LocalLLMEntry.mtp_supported`) —
            adds the *Built-in* option.

    Returns:
        LlamaKnob: A ``DROPDOWN`` defaulting to ``off``.
    """
    options = [MTP_SPEC_DECODE_KNOB.options[0]]
    if builtin:
        options.append(
            KnobOption(
                id=MTP_BUILTIN_OPTION_ID,
                name="Built-in layers",
                description=(
                    "Draft with the MTP layers inside this quant's own GGUF — nothing extra to "
                    "load. Their quantization follows the quant author's choice for this file."
                ),
                llama_args=dict(_SPEC_TYPE_MTP),
            )
        )
    options.extend(_head_option(sidecar) for sidecar in sidecars)
    return LlamaKnob(
        id=mtp_head_knob_id(base_llm, builtin=builtin),
        name=MTP_SPEC_DECODE_KNOB.name,
        description=(
            "Lets llama.cpp guess several tokens ahead with a Multi-Token Prediction head and "
            "verify them in one pass. Generation gets faster on accepted guesses and the output "
            "is unchanged either way, since every drafted token is verified against the full "
            "model. Pick which head drafts: one baked into this quant, or a separate head file "
            "— any of them works with any quant of this model."
        ),
        kind=KnobKind.DROPDOWN,
        options=tuple(options),
        default_option=MTP_SPEC_DECODE_KNOB.options[0].id,
    )
