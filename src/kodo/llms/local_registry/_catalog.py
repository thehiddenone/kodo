"""Assembles the compiled-in GGUF catalog from each ``_local_llm_*`` family module.

Ported from the old flat registry, dropping ``residence``. Add a new
hardcoded model by adding it to (or creating) the relevant
``_local_llm_<family>.py`` module and listing that module's ``*_entries()``
function below — nothing else in this package should construct
``LocalLLMEntry`` literals for a ``hardcoded_hf`` model.
"""

from __future__ import annotations

from ._local_llm_gemma4_26b_a4b import gemma4_26b_a4b_entries
from ._local_llm_gemma4_31b import gemma4_31b_entries
from ._local_llm_gpt_oss_20b import gpt_oss_20b_entries
from ._local_llm_gpt_oss_120b import gpt_oss_120b_entries
from ._local_llm_laguna_s_21 import laguna_s_21_entries
from ._local_llm_ornith10_9b import ornith10_9b_entries
from ._local_llm_ornith10_35b_a3b import ornith10_35b_a3b_entries
from ._local_llm_qwen3_coder_next_80b import qwen3_coder_next_80b_entries
from ._local_llm_qwen35_9b import qwen35_9b_entries
from ._local_llm_qwen36_27b import qwen36_27b_entries
from ._local_llm_qwen36_35b_a3b import qwen36_35b_a3b_entries
from ._types import LocalLLMEntry

_HARDCODED_LOCAL_MODELS: tuple[LocalLLMEntry, ...] = tuple(
    entry
    for family_entries in (
        qwen36_27b_entries(),
        qwen36_35b_a3b_entries(),
        qwen3_coder_next_80b_entries(),
        qwen35_9b_entries(),
        laguna_s_21_entries(),
        gpt_oss_120b_entries(),
        gpt_oss_20b_entries(),
        gemma4_26b_a4b_entries(),
        gemma4_31b_entries(),
        ornith10_35b_a3b_entries(),
        ornith10_9b_entries(),
    )
    for entry in family_entries
)
