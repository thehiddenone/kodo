"""LLM registry: catalogue of all supported models, cloud and local."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "LLMEntry",
    "get_context_window",
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
        context_window: Maximum input-context size of the model, in tokens. This
            is the per-model token budget the engine uses both as the displayed
            "context limit" and as the basis for the auto-compaction threshold;
            switching to a model with a smaller window can trigger a compaction
            immediately (see ``WorkflowEngine.handle_config_changed``). Falls back
            to :data:`_DEFAULT_CONTEXT_WINDOW` when unset/non-positive.
    """

    name: str
    residence: str
    module: str
    description: str
    model_id: str = ""
    repo_id: str = ""
    filename: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)
    context_window: int = 0


# Fallback context window for an entry whose ``context_window`` is unset or for an
# unknown model key — keeps auto-compaction working with a sane budget.
_DEFAULT_CONTEXT_WINDOW = 200_000


_REGISTRY: dict[str, LLMEntry] = {
    "claude-opus-4-8": LLMEntry(
        name="claude-opus-4-8",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.8",
        model_id="claude-opus-4-8",
        context_window=1_000_000,
    ),
    "claude-opus-4-7": LLMEntry(
        name="claude-opus-4-7",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.7",
        model_id="claude-opus-4-7",
        context_window=1_000_000,
    ),
    "claude-opus-4-6": LLMEntry(
        name="claude-opus-4-6",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Opus 4.6",
        model_id="claude-opus-4-6",
        context_window=1_000_000,
    ),
    "claude-sonnet-4-6": LLMEntry(
        name="claude-sonnet-4-6",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Sonnet 4.6",
        model_id="claude-sonnet-4-6",
        context_window=1_000_000,
    ),
    "claude-haiku-4-5": LLMEntry(
        name="claude-haiku-4-5",
        residence="cloud",
        module="kodo.llms.anthropic",
        description="Anthropic Claude Haiku 4.5",
        model_id="claude-haiku-4-5-20251001",
        context_window=200_000,
    ),
    "llamacpp-qwen36-27b-q4-k-xl": LLMEntry(
        name="llamacpp-qwen36-27b-q4-k-xl",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Qwen 3.6 27B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    "llamacpp-qwen35-9b-q8-k-xl": LLMEntry(
        name="llamacpp-qwen35-9b-q8-k-xl",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Qwen 3.5 9B UD-Q8_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.5-9B-MTP-GGUF",
        filename="Qwen3.5-9B-UD-Q8_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    "llamacpp-gemma4-26b-q4-k-xl": LLMEntry(
        name="llamacpp-gemma4-26b-q4-k-xl",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Gemma 4 26B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=131_072,
    ),
    "llamacpp-ornith10-35b-q8-0": LLMEntry(
        name="llamacpp-ornith10-35b-q8-0",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Ornith 1.0 35B Q8_0 by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q8_0.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    "llamacpp-ornith10-35b-q6-k": LLMEntry(
        name="llamacpp-ornith10-35b-q6-k",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Ornith 1.0 35B Q6_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q6_K.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    "llamacpp-ornith10-35b-q5-k-m": LLMEntry(
        name="llamacpp-ornith10-35b-q5-k-m",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Ornith 1.0 35B Q5_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q5_K_m.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    "llamacpp-ornith10-35b-q4-k-m": LLMEntry(
        name="llamacpp-ornith10-35b-q4-k-m",
        residence="local",
        module="kodo.llms.llamacpp",
        description="Ornith 1.0 35B Q4_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q4_K_m.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
}


def get_llm_registry() -> dict[str, LLMEntry]:
    """Return a shallow copy of the LLM registry.

    Returns:
        dict[str, LLMEntry]: Map of registry key to :class:`LLMEntry`.
    """
    return _REGISTRY.copy()


def get_context_window(model_key: str) -> int:
    """Return the maximum context window (in tokens) for *model_key*.

    Looks the key up in the registry and returns its ``context_window``; falls
    back to :data:`_DEFAULT_CONTEXT_WINDOW` for an unknown key or one whose
    ``context_window`` is unset/non-positive.

    Args:
        model_key: A registry key (e.g. ``'claude-opus-4-8'``).

    Returns:
        int: The model's context window in tokens (always > 0).
    """
    entry = _REGISTRY.get(model_key)
    if entry is not None and entry.context_window > 0:
        return entry.context_window
    return _DEFAULT_CONTEXT_WINDOW
