"""Model Importer: read a Hugging Face GGUF repo, add its quants to the user catalog.

The only package that combines :mod:`kodo.llms.local` (the Hub and GGUF headers)
with :mod:`kodo.llms.local_registry` (the catalog format). The engine binds one
:class:`LocalCatalogService` per session and hands it to the importer tools
through ``kodo.tools``' ``LocalCatalogLike`` protocol; the server uses one for
the settings dialog's repo search (``local_llm.hf_search``). Design:
doc/LLM_REGISTRY.md §4.0b.
"""

from ._errors import ModelImportError
from ._hub import HubClient, HuggingFaceHub, RepoFile, RepoSearchHit, RepoSnapshot
from ._search import (
    HF_KNOWN_PUBLISHERS,
    HF_TOP_PUBLISHERS,
    SEARCH_MIN_QUERY_LENGTH,
    RankedSearchHit,
    rank_search_hits,
)
from ._service import LocalCatalogService, format_size_hint, guess_quant_type

__all__ = [
    "HF_KNOWN_PUBLISHERS",
    "HF_TOP_PUBLISHERS",
    "SEARCH_MIN_QUERY_LENGTH",
    "HubClient",
    "HuggingFaceHub",
    "LocalCatalogService",
    "ModelImportError",
    "RankedSearchHit",
    "RepoFile",
    "RepoSearchHit",
    "RepoSnapshot",
    "format_size_hint",
    "guess_quant_type",
    "rank_search_hits",
]
