"""Laguna-S-2.1's private long-context knob (YaRN rope-scaling).

Unlike the Qwen families (:mod:`._knobs_qwen`) — whose GGUFs default to their
real trained length and only need YaRN when explicitly extended — every
Unsloth Laguna-S-2.1 quant ships with YaRN rope-scaling *already baked into
its own GGUF metadata*, defaulting to 256K (rope-scale 32 over the model's
true 8K training context) with no launch args required. Laguna never actually
runs at that unscaled 8K length, so :func:`~._knobs_context.make_yarn_context_knob`
— which always offers an args-free "native" option as the default — does not
fit this model and is not used here. :data:`LAGUNA_CONTEXT_KNOB` is built by
hand instead: its default option ("256K") writes no args, same as the shared
helper's "native" slot does elsewhere, but represents the GGUF's own
pre-scaled default rather than an unscaled floor. The 512K and 1M options
still write explicit YaRN args computed off the real native context of
:data:`_NATIVE_CONTEXT` tokens (8192), same recipe as the Qwen knobs, and key
their ``--override-kv`` metadata override on ``laguna.context_length``,
Laguna-S-2.1's architecture name.

Catalog entries list it by id (``"context-laguna"``) in their ``knobs`` array
— see :mod:`._knobs_table`.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob

__all__ = ["LAGUNA_CONTEXT_KNOB"]

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
#: module docstring for why this can't use the shared ``make_yarn_context_knob``.
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
