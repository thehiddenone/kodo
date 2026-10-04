"""Every knob a catalog JSON file can name, keyed by its id.

A catalog entry (``catalog/<base_llm>/<name>.json`` in this package, or the
same layout under ``~/.kodo/local_llms/`` — see :mod:`._catalog_files`)
declares its knobs as a list of **ids**, never as definitions: knobs are code
(:mod:`._knobs`), and this table is the only bridge from a JSON string to a
:class:`~._knobs.LlamaKnob`. An id missing from it makes the file invalid.

That is what keeps the two knob invariants intact once entries become data:

- **No user-defined knob.** A user file can choose *which* of these knobs an
  entry offers, but cannot invent one or change what an existing one does.
- **One definition per id.** Every entry that names ``"context-qwen35"``
  resolves to the very same object, so the wire payload's id-deduplicated
  knob table (``_knob_defs_payload`` in ``kodo.server``) stays lossless no
  matter which files are loaded.

Add a new knob by defining it in its ``_knobs_*`` module and listing it in
:data:`_ALL_KNOBS` below; a duplicate id is a hard failure at import.
"""

from __future__ import annotations

from ._knobs import LlamaKnob
from ._knobs_laguna import LAGUNA_CONTEXT_KNOB
from ._knobs_mtp import MTP_SPEC_DECODE_KNOB
from ._knobs_qwen import QWEN4EXP_CONTEXT_KNOB, QWEN_CONTEXT_KNOB, QWEN_MOE_CONTEXT_KNOB
from ._knobs_shared import SHARED_KNOBS

__all__ = ["KNOBS_BY_ID"]

#: Shared knobs first, then the private per-family ones.
_ALL_KNOBS: tuple[LlamaKnob, ...] = SHARED_KNOBS + (
    QWEN_CONTEXT_KNOB,
    QWEN_MOE_CONTEXT_KNOB,
    QWEN4EXP_CONTEXT_KNOB,
    LAGUNA_CONTEXT_KNOB,
    MTP_SPEC_DECODE_KNOB,
)


def _index(knobs: tuple[LlamaKnob, ...]) -> dict[str, LlamaKnob]:
    by_id: dict[str, LlamaKnob] = {}
    for knob in knobs:
        if knob.id in by_id:
            raise ValueError(f"Two knobs share the id {knob.id!r}; knob ids must be unique")
        by_id[knob.id] = knob
    return by_id


#: ``{knob_id: knob}`` for every knob a catalog entry may list.
KNOBS_BY_ID: dict[str, LlamaKnob] = _index(_ALL_KNOBS)
