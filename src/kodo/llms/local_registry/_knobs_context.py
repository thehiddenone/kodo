"""Factory for the private per-model "Context window" knob (YaRN rope-scaling).

Extending a GGUF past its trained context length needs three things llama.cpp
cannot infer: the YaRN scale factor (target size over the model's *native*
context), the native size itself (``--yarn-orig-ctx``), and an
``--override-kv`` metadata override so llama.cpp does not cap the KV cache at
the trained length recorded in the file. The last one is keyed by the model's
**architecture name**, which differs per family (``laguna``, ``qwen35``,
``qwen35moe``, …) — that key is model knowledge, never guessable from the
registry entry, which is exactly why this is a *private* knob built per family
rather than one of the shared knobs in :mod:`._knobs_shared`.

The resulting knob owns ``--ctx-size``, ``--rope-scaling``, ``--rope-scale``,
``--yarn-orig-ctx`` and ``--override-kv``. Its "native" option writes nothing
at all, letting the entry's base ``--ctx-size 0`` ("use the GGUF's own trained
length") stand — see :mod:`._knobs` on base args being the floor that knob
args layer over.

Only families with a verified YaRN recipe declare one; an entry without a
context knob simply has no context control in its Configure modal and always
runs at the GGUF's trained length.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob

__all__ = ["make_yarn_context_knob"]


def _format_tokens(tokens: int) -> str:
    """``524288`` -> ``"512K"``, ``1048576`` -> ``"1M"``, anything else verbatim."""
    if tokens >= 1_048_576 and tokens % 1_048_576 == 0:
        return f"{tokens // 1_048_576}M"
    if tokens >= 1024 and tokens % 1024 == 0:
        return f"{tokens // 1024}K"
    return str(tokens)


def make_yarn_context_knob(
    *,
    knob_id: str,
    arch_key: str,
    native_context: int,
    sizes: tuple[int, ...],
) -> LlamaKnob:
    """Build a "Context window" knob offering *sizes* on top of *native_context*.

    Args:
        knob_id: Globally unique knob id. Must encode the family, since two
            families' context knobs differ in *arch_key*/*native_context* and
            knob ids are deduplicated into a single wire-level table — e.g.
            ``"context-laguna"``, ``"context-qwen35moe"``.
        arch_key: The model's llama.cpp architecture name, used to build
            ``--override-kv <arch_key>.context_length=int:<size>``. Model
            knowledge — never derive it from the entry name.
        native_context: The model's trained context length in tokens. Sets
            ``--yarn-orig-ctx`` and is the divisor for each option's
            ``--rope-scale``.
        sizes: Extended context sizes in tokens, smallest first. Each becomes
            one option alongside the default "native" one.

    Returns:
        LlamaKnob: A ``DROPDOWN`` knob whose first option is "native" (no
        args) followed by one option per entry in *sizes*.

    Raises:
        ValueError: If *native_context* is not positive, *sizes* is empty, or
            any size is not larger than *native_context* (a "extension" that
            shrinks the context is a declaration error, not a valid option).
    """
    if native_context <= 0:
        raise ValueError(f"{knob_id}: native_context must be positive, got {native_context}")
    if not sizes:
        raise ValueError(f"{knob_id}: needs at least one extended size")

    options = [
        KnobOption(
            id="native",
            name=f"Native ({_format_tokens(native_context)})",
            description=(
                "The context length the model was trained at. No rope-scaling, no accuracy "
                "trade-off, and the smallest KV cache — the right choice unless you genuinely "
                "need to hold more than this at once."
            ),
        )
    ]
    for size in sizes:
        if size <= native_context:
            raise ValueError(
                f"{knob_id}: extended size {size} is not larger than the native "
                f"context {native_context}"
            )
        label = _format_tokens(size)
        # str(float(...)) so the flag reads "64.0" rather than "64" — llama.cpp
        # accepts both, but the float spelling matches every other rope-scale
        # value in the catalog and in llama.cpp's own documentation.
        rope_scale = str(float(size) / float(native_context))
        options.append(
            KnobOption(
                id=label.lower(),
                name=f"{label} (YaRN-extended)",
                description=(
                    f"Stretches the model's {_format_tokens(native_context)} training context to "
                    f"{label} tokens with YaRN rope-scaling. Recall and reasoning degrade as you "
                    "go further past the native length, and the KV cache grows in proportion — "
                    "at these sizes it is usually only practical on a machine with one large "
                    "unified memory pool (Apple Silicon), not on a discrete GPU plus system RAM."
                ),
                llama_args={
                    "--ctx-size": str(size),
                    "--rope-scaling": "yarn",
                    "--rope-scale": rope_scale,
                    "--yarn-orig-ctx": str(native_context),
                    "--override-kv": f"{arch_key}.context_length=int:{size}",
                },
            )
        )

    return LlamaKnob(
        id=knob_id,
        name="Context window",
        description=(
            "How many tokens the model can hold at once. Anything beyond the native size is "
            "reached with YaRN rope-scaling, which trades some accuracy for reach and costs "
            "proportionally more memory for the KV cache."
        ),
        kind=KnobKind.DROPDOWN,
        options=tuple(options),
        default_option="native",
    )
