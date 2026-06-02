"""LLM registry: catalogue of all supported models, cloud and local."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "LLMEntry",
    "get_llm_registry",
]


@dataclass(frozen=True)
class LLMEntry:
    """A registered LLM model, either cloud-hosted or locally-inferred.

    Attributes:
        name: Registry key (e.g. ``'claude-sonnet-4-6'``, ``'llamacpp-qwen36-27b'``).
        residence: ``'cloud'`` or ``'local'``.
        module: Dotted module path of the :class:`LLMPlugin` implementation.
        description: Human-readable description.
        model_id: Cloud model identifier sent to the provider API (cloud only).
        repo_id: HuggingFace repository ID (local only).
        filename: GGUF filename inside the HF repository (local only).
        llama_args: Extra CLI flags passed verbatim to ``llama-server`` (local only).
    """

    name: str
    residence: str
    module: str
    description: str
    model_id: str = ""
    repo_id: str = ""
    filename: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)


_REGISTRY: dict[str, LLMEntry] = {
    "claude-opus-4-8": LLMEntry(
        name="claude-opus-4-8",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.8",
        model_id="claude-opus-4-8",
    ),
    "claude-opus-4-7": LLMEntry(
        name="claude-opus-4-7",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.7",
        model_id="claude-opus-4-7",
    ),
    "claude-opus-4-6": LLMEntry(
        name="claude-opus-4-6",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.6",
        model_id="claude-opus-4-6",
    ),
    "claude-sonnet-4-6": LLMEntry(
        name="claude-sonnet-4-6",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Sonnet 4.6",
        model_id="claude-sonnet-4-6",
    ),
    "claude-haiku-4-5": LLMEntry(
        name="claude-haiku-4-5",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Haiku 4.5",
        model_id="claude-haiku-4-5-20251001",
    ),
    "llamacpp-qwen36-27b": LLMEntry(
        name="llamacpp-qwen36-27b",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Qwen 3.6 27B UD-Q4_K_XL — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q4_0", "--cache-type-v": "q4_0"},
    ),
    "llamacpp-qwen35-9b": LLMEntry(
        name="llamacpp-qwen35-9b",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Qwen 3.5 9B UD-Q8_K_XL — local inference via llama-server",
        repo_id="unsloth/Qwen3.5-9B-MTP-GGUF",
        filename="Qwen3.5-9B-UD-Q8_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
    ),
    "llamacpp-gemma4-26b": LLMEntry(
        name="llamacpp-gemma4-26b",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Gemma 4 26B UD-Q4_K_XL — local inference via llama-server",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q4_0", "--cache-type-v": "q4_0"},
    ),
}


def get_llm_registry() -> dict[str, LLMEntry]:
    """Return a shallow copy of the LLM registry.

    Returns:
        dict[str, LLMEntry]: Map of registry key to :class:`LLMEntry`.
    """
    return _REGISTRY.copy()
