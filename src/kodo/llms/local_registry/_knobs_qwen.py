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

The ``ornith`` families split across both knobs **by GGUF architecture key,
not by family name** — the 35B-A3B and 9B builds of one generation are not
the same architecture. ``Ornith-1.0-35B-A3B`` and ``Ornith-1.5-35B-A3B``
record ``general.architecture = qwen35moe`` and take
:data:`QWEN_MOE_CONTEXT_KNOB`; ``Ornith-1.0-9B`` and ``Ornith-1.5-9B`` are
*dense*, record ``general.architecture = qwen35``, and take
:data:`QWEN_CONTEXT_KNOB`.

``Ornith-1.0-9B`` listed the MoE knob until 2026-09-18 — inherited from the
era when both ornith10 sizes shared one MoE flavor builder. The symptom is
worth remembering, because nothing fails loudly: ``--override-kv`` named
``qwen35moe.context_length`` on a GGUF with no such key, llama.cpp dropped
the override without a word, and the entry's 512K/1M options silently did
not extend the KV cache past the trained 262144 tokens. Always read the
architecture key off the GGUF header; never infer it from the entry name.

Qwen3.8-Flash-Next is a third architecture again (``qwen4exp``), which is why
it gets :data:`QWEN4EXP_CONTEXT_KNOB` rather than reusing the MoE knob above:
same 262144-token native context and the same YaRN factors, but its GGUF
records the context length under its own architecture key, so an
``--override-kv`` aimed at ``qwen35moe.context_length`` would be silently
ignored and llama.cpp would keep capping the KV cache at the trained length.
"""

from __future__ import annotations

from ._knobs_context import make_yarn_context_knob

__all__ = [
    "QWEN4EXP_CONTEXT_KNOB",
    "QWEN_CONTEXT_KNOB",
    "QWEN_MOE_CONTEXT_KNOB",
]

#: Qwen's native (trained) context length, the base for both knobs' YaRN
#: scaling — ``--rope-scale`` 2.0 at 512K and 4.0 at 1M.
_QWEN_NATIVE_CONTEXT = 262_144

#: Extended sizes offered on top of the native one, smallest first.
_QWEN_EXTENDED_SIZES = (524_288, 1_048_576)

#: Dense Qwen builds (Qwen3.5-9B, Qwen3.6-27B, Ornith-1.0-9B, Ornith-1.5-9B).
QWEN_CONTEXT_KNOB = make_yarn_context_knob(
    knob_id="context-qwen35",
    arch_key="qwen35",
    native_context=_QWEN_NATIVE_CONTEXT,
    sizes=_QWEN_EXTENDED_SIZES,
)

#: Sparse-MoE Qwen builds (Qwen3.6-35B-A3B, Ornith10-35B-A3B,
#: Ornith15-35B-A3B).
QWEN_MOE_CONTEXT_KNOB = make_yarn_context_knob(
    knob_id="context-qwen35moe",
    arch_key="qwen35moe",
    native_context=_QWEN_NATIVE_CONTEXT,
    sizes=_QWEN_EXTENDED_SIZES,
)

#: Qwen3.8-Flash-Next (``qwen4exp``) — a hybrid Gated DeltaNet / Qwen Sparse
#: Attention MoE build. The model card documents the same YaRN recipe as the
#: other Qwen families (``factor`` 2.0 for 512K, 4.0 for 1M over the native
#: 262144), and only 12 of its 48 layers carry a real KV cache, so the extended
#: sizes cost far less memory here than the shared description implies.
QWEN4EXP_CONTEXT_KNOB = make_yarn_context_knob(
    knob_id="context-qwen4exp",
    arch_key="qwen4exp",
    native_context=_QWEN_NATIVE_CONTEXT,
    sizes=_QWEN_EXTENDED_SIZES,
)
