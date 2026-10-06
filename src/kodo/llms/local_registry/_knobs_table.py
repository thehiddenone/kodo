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

import re

from ._knobs import LlamaKnob
from ._knobs_laguna import LAGUNA_CONTEXT_KNOB
from ._knobs_mtp import MTP_SPEC_DECODE_KNOB
from ._knobs_qwen import QWEN4EXP_CONTEXT_KNOB, QWEN_CONTEXT_KNOB, QWEN_MOE_CONTEXT_KNOB
from ._knobs_shared import SHARED_KNOBS

__all__ = ["KNOBS_BY_ID", "context_knob_architectures"]

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


_CONTEXT_OVERRIDE = re.compile(r"^(?P<arch>[A-Za-z0-9_.-]+)\.context_length=int:\d+$")


def context_knob_architectures() -> dict[str, tuple[str, int]]:
    """Which llama.cpp architecture each context-window knob in the table targets.

    A YaRN context knob extends the context with ``--override-kv
    <arch>.context_length=…``, which llama.cpp silently ignores when ``<arch>``
    is not the GGUF's own ``general.architecture`` — so a knob only works for
    models of the architecture it names. Read off the knobs' own options, so
    a new context knob registers here by being added to the table.

    Returns:
        dict[str, tuple[str, int]]: ``{knob_id: (architecture, native_context)}``;
        ``native_context`` is the knob's ``--yarn-orig-ctx``, ``0`` if no option
        sets one.
    """
    found: dict[str, tuple[str, int]] = {}
    for knob in _ALL_KNOBS:
        arch = ""
        native = 0
        for option in knob.options:
            match = _CONTEXT_OVERRIDE.match(option.llama_args.get("--override-kv", ""))
            if match is not None:
                arch = match.group("arch")
            orig = option.llama_args.get("--yarn-orig-ctx", "")
            if orig.isdigit():
                native = int(orig)
        if arch:
            found[knob.id] = (arch, native)
    return found
