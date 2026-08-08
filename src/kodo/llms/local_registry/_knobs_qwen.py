"""Qwen-family long-context knobs (YaRN rope-scaling).

Replaces the four ``make_qwen_*_kv_q8`` flavor builders this package used to
carry: where each of those produced one *flavor* per (architecture, size) pair
— so a dense Qwen entry shipped a "512K context size" flavor and a "1M context
size" flavor that differed from ``default`` in nothing but their context args —
there are now two *knobs*, each offering all three sizes as options. Everything
else those flavors repeated (KV cache type, GPU offload, ``--jinja``) is
handled by the shared knobs and base args instead, which is what makes the
combination "1M context *and* f16 KV cache" reachable at all.

Two knobs rather than one because dense and MoE Qwen builds record their
context length under different ``--override-kv`` metadata keys
(``qwen35.context_length`` vs ``qwen35moe.context_length``) — model knowledge,
not something derivable from the registry entry. Both scale off the same
262144-token native context.

The ``ornith10`` families are Qwen-architecture MoE builds and use
:data:`QWEN_MOE_CONTEXT_KNOB` too — they always shared the MoE flavor builders
for the same reason.
"""

from __future__ import annotations

from ._knobs_context import make_yarn_context_knob

__all__ = [
    "QWEN_CONTEXT_KNOB",
    "QWEN_MOE_CONTEXT_KNOB",
]

#: Qwen's native (trained) context length, the base for both knobs' YaRN
#: scaling — ``--rope-scale`` 2.0 at 512K and 4.0 at 1M.
_QWEN_NATIVE_CONTEXT = 262_144

#: Extended sizes offered on top of the native one, smallest first.
_QWEN_EXTENDED_SIZES = (524_288, 1_048_576)

#: Dense Qwen builds (Qwen3.5-9B, Qwen3.6-27B).
QWEN_CONTEXT_KNOB = make_yarn_context_knob(
    knob_id="context-qwen35",
    arch_key="qwen35",
    native_context=_QWEN_NATIVE_CONTEXT,
    sizes=_QWEN_EXTENDED_SIZES,
)

#: Sparse-MoE Qwen builds (Qwen3.6-35B-A3B, Ornith10-35B-A3B, Ornith10-9B).
QWEN_MOE_CONTEXT_KNOB = make_yarn_context_knob(
    knob_id="context-qwen35moe",
    arch_key="qwen35moe",
    native_context=_QWEN_NATIVE_CONTEXT,
    sizes=_QWEN_EXTENDED_SIZES,
)
