"""Built-in registry of known local-inference models.

Maps short names (e.g. ``'default'``) to HuggingFace repository IDs and
specific GGUF filenames so callers never hard-code model coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["LLM_REGISTRY", "ModelEntry", "get_model"]


@dataclass(frozen=True)
class ModelEntry:
    """A registered local-inference model.

    Attributes:
        name: Short registry key used to look the entry up (e.g. ``'default'``).
        repo_id: HuggingFace repository ID (e.g. ``'unsloth/Qwen3-27B-GGUF'``).
        filename: Specific GGUF filename inside the repository.
        description: Human-readable description shown in help text.
    """

    name: str
    repo_id: str
    filename: str
    description: str
    llama_args: dict[str, str] = field(default_factory=dict)


LLM_REGISTRY: dict[str, ModelEntry] = {
    "default": ModelEntry(
        name="default",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
        description="Qwen 3.6 27B MTP, UD-Q4_K_XL quantization",
        llama_args={
            "--cache-type-k": "q4_0",
            "--cache-type-v": "q4_0",
        },
    ),
}


def get_model(name: str = "default") -> ModelEntry:
    """Return the registry entry for *name*.

    Args:
        name (str): Registry key.  Defaults to ``'default'``.

    Returns:
        ModelEntry: Metadata for the requested model.

    Raises:
        KeyError: If *name* is not present in the registry.
    """
    if name not in LLM_REGISTRY:
        available = ", ".join(f"'{k}'" for k in sorted(LLM_REGISTRY))
        raise KeyError(f"Unknown model {name!r}. Available: {available}")
    return LLM_REGISTRY[name]
