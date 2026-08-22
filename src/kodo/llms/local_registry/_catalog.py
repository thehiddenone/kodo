"""Assembles the compiled-in GGUF catalog from each ``_local_llm_*`` family module.

Ported from the old flat registry, dropping ``residence``. Add a new
hardcoded model by adding it to (or creating) the relevant
``_local_llm_<family>.py`` module and listing that module's ``*_entries()``
function below — nothing else in this package should construct
``LocalLLMEntry`` literals for a ``hardcoded_hf`` model.
"""

from __future__ import annotations

from ._knobs import validate_knobs
from ._local_llm_gemma4_26b_a4b import gemma4_26b_a4b_entries
from ._local_llm_gemma4_31b import gemma4_31b_entries
from ._local_llm_gpt_oss_20b import gpt_oss_20b_entries
from ._local_llm_gpt_oss_120b import gpt_oss_120b_entries
from ._local_llm_laguna_s_21 import laguna_s_21_entries
from ._local_llm_laguna_xs_21 import laguna_xs_21_entries
from ._local_llm_muse_glimmer_30b import muse_glimmer_30b_entries
from ._local_llm_nanbiege42_3b import nanbiege42_3b_entries
from ._local_llm_nemotron35_30b_a3b import nemotron35_30b_a3b_entries
from ._local_llm_ornith10_9b import ornith10_9b_entries
from ._local_llm_ornith10_35b_a3b import ornith10_35b_a3b_entries
from ._local_llm_qwen3_coder_next_80b import qwen3_coder_next_80b_entries
from ._local_llm_qwen35_9b import qwen35_9b_entries
from ._local_llm_qwen36_27b import qwen36_27b_entries
from ._local_llm_qwen36_35b_a3b import qwen36_35b_a3b_entries
from ._local_llm_qwen38_27b import qwen38_27b_entries
from ._types import LocalLLMEntry

_HARDCODED_LOCAL_MODELS: tuple[LocalLLMEntry, ...] = tuple(
    entry
    for family_entries in (
        qwen38_27b_entries(),
        qwen36_27b_entries(),
        qwen36_35b_a3b_entries(),
        qwen3_coder_next_80b_entries(),
        qwen35_9b_entries(),
        laguna_s_21_entries(),
        laguna_xs_21_entries(),
        ornith10_35b_a3b_entries(),
        ornith10_9b_entries(),
        nemotron35_30b_a3b_entries(),
        muse_glimmer_30b_entries(),
        gemma4_26b_a4b_entries(),
        gemma4_31b_entries(),
        nanbiege42_3b_entries(),
        gpt_oss_120b_entries(),
        gpt_oss_20b_entries(),
    )
    for entry in family_entries
)


def _validate_catalog() -> None:
    """Import-time knob checks across the whole hardcoded catalog.

    Two things, both hard failures at startup rather than mysteries at launch
    time (see :func:`~kodo.llms.local_registry._knobs.validate_knobs`):

    1. **Per entry** — no two of its knobs own the same llama-server flag,
       every knob is structurally coherent, and every ``knob_defaults`` key
       names a knob the entry actually offers whose value is a real option.
    2. **Across entries** — two entries listing a knob under the same id must
       list the identical knob. Knob definitions are deduplicated by id into
       one table on the wire, so a same-id/different-definition pair would
       make one entry's Configure modal silently render the other's options.
    """
    known: dict[str, object] = {}
    for entry in _HARDCODED_LOCAL_MODELS:
        validate_knobs(entry.knobs, context=entry.name)
        by_id = {knob.id: knob for knob in entry.knobs}
        for knob_id, selection in entry.knob_defaults.items():
            knob = by_id.get(knob_id)
            if knob is None:
                raise ValueError(
                    f"{entry.name}: knob_defaults names {knob_id!r}, which this entry "
                    "does not offer"
                )
            if knob.options and knob.option(selection) is None:
                raise ValueError(
                    f"{entry.name}: knob_defaults sets {knob_id!r} to {selection!r}, "
                    "which is not one of its options"
                )
        for knob in entry.knobs:
            previous = known.setdefault(knob.id, knob)
            if previous != knob:
                raise ValueError(
                    f"{entry.name}: knob {knob.id!r} differs from the definition another "
                    "entry uses under the same id"
                )


_validate_catalog()
