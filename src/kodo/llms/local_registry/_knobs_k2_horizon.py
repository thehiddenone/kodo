"""K2-Horizon-32B's private context-window knob (a cap, not an extension).

Every other context knob in the catalog *extends* a model past its trained
length with YaRN (:mod:`._knobs_context`, :mod:`._knobs_laguna`). K2 Horizon
needs the opposite: its GGUF records a native ``k2-horizon.context_length`` of
524288, and kodo's base ``--ctx-size 0`` means "allocate the full trained
length". Since llama.cpp #19070 (2026-01) an explicit ``--ctx-size 0`` also
stops ``--fit`` from shrinking the context to fit memory, so without this knob
every launch would reserve a 512K KV cache — all 64 layers are full GQA
attention (8 KV heads x 128 + 128 dims), about 73GB at q8_0 on top of the
weights.

:data:`K2_HORIZON_CONTEXT_KNOB` therefore defaults to 128K, the length the
catalog's memory totals and tips are quoted at, and offers 256K and the full
native 512K. No option needs rope-scaling or an ``--override-kv``: every size
is within the trained length. That is also why the knob does not appear in
:func:`~._knobs_table.context_knob_architectures`, which maps only the YaRN
knobs' ``--override-kv`` targets. An explicit non-zero ``--ctx-size`` is
honored exactly by ``--fit`` (it offloads layers instead of shrinking the
context), so the chosen size is the size the model runs at.

Catalog entries list it by id (``"context-k2-horizon"``) in their ``knobs``
array — see :mod:`._knobs_table`.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob

__all__ = ["K2_HORIZON_CONTEXT_KNOB"]

#: K2-Horizon-32B's context window: 128K by default, up to its native 512K.
K2_HORIZON_CONTEXT_KNOB = LlamaKnob(
    id="context-k2-horizon",
    name="Context window",
    description=(
        "How many tokens the model can hold at once. K2 Horizon is trained for 512K, but "
        "every one of its 64 layers keeps a full-attention KV cache, so the context size "
        "dominates memory use: about 18GB at 128K with the default q8_0 cache, 37GB at "
        "256K and 73GB at 512K, on top of the weights. No size here needs rope-scaling."
    ),
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(
            id="128k",
            name="128K (default)",
            description=(
                "Enough for a long agent session, and the size the memory estimates on the "
                "model card are quoted at. The right choice unless you genuinely need to hold "
                "more than this at once."
            ),
            llama_args={"--ctx-size": "131072"},
        ),
        KnobOption(
            id="256k",
            name="256K",
            description=(
                "Twice the default, within the model's trained length, so no accuracy "
                "trade-off. The KV cache doubles to about 37GB at q8_0."
            ),
            llama_args={"--ctx-size": "262144"},
        ),
        KnobOption(
            id="512k",
            name="Native (512K)",
            description=(
                "The full length the model was trained at. The KV cache alone is about 73GB at "
                "q8_0 (twice that at f16), so this is only practical on a machine with a very "
                "large unified memory pool."
            ),
        ),
    ),
    default_option="128k",
)
