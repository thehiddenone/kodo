"""Model Importer: read a Hugging Face GGUF repo, add its quants to the user catalog.

The only package that combines :mod:`kodo.llms.local` (the Hub and GGUF headers)
with :mod:`kodo.llms.local_registry` (the catalog format). The engine binds one
:class:`LocalCatalogService` per session and hands it to the importer tools
through ``kodo.tools``' ``LocalCatalogLike`` protocol. Design:
doc/LLM_REGISTRY.md §4.0b.
"""

from ._errors import ModelImportError
from ._hub import HubClient, HuggingFaceHub, RepoFile, RepoSnapshot
from ._service import LocalCatalogService, format_size_hint, guess_quant_type

__all__ = [
    "HubClient",
    "HuggingFaceHub",
    "LocalCatalogService",
    "ModelImportError",
    "RepoFile",
    "RepoSnapshot",
    "format_size_hint",
    "guess_quant_type",
]
