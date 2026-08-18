"""Tests for ``kodo.llms._context.get_context_window``.

Covers the cloud-registry / OpenRouter-catalog / local-registry lookup order
and the ``_DEFAULT_CONTEXT_WINDOW`` fallback -- see doc/LLM_REGISTRY.md §3a
for why OpenRouter needs its own lookup step (its models aren't in the
compiled-in cloud registry).
"""

from __future__ import annotations

import json
from pathlib import Path

from kodo.llms._context import _DEFAULT_CONTEXT_WINDOW, get_context_window


def test_known_cloud_model_returns_registry_context_window(tmp_path: Path) -> None:
    assert get_context_window("claude-sonnet-5", tmp_path) == 1_000_000


def test_unknown_model_key_falls_back_to_default(tmp_path: Path) -> None:
    assert get_context_window("totally-unknown-model", tmp_path) == _DEFAULT_CONTEXT_WINDOW


def _write_openrouter_cache(kodo_dir: Path, models: list[dict[str, object]]) -> None:
    cache_path = kodo_dir / "etc" / "openrouter-models.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"fetched_at": 0.0, "models": models}), encoding="utf-8")


def test_openrouter_model_returns_its_own_context_length(tmp_path: Path) -> None:
    _write_openrouter_cache(
        tmp_path, [{"id": "anthropic/claude-sonnet-4", "context_length": 1_048_576}]
    )
    assert get_context_window("anthropic/claude-sonnet-4", tmp_path) == 1_048_576


def test_openrouter_auto_with_zero_context_length_falls_back_to_default(tmp_path: Path) -> None:
    """openrouter/auto's real context limit depends on whichever model it
    routes to -- its own catalog entry typically has no fixed context_length."""
    _write_openrouter_cache(tmp_path, [{"id": "openrouter/auto", "context_length": 0}])
    assert get_context_window("openrouter/auto", tmp_path) == _DEFAULT_CONTEXT_WINDOW


def test_openrouter_catalog_not_yet_fetched_falls_back_to_default(tmp_path: Path) -> None:
    assert get_context_window("anthropic/claude-sonnet-4", tmp_path) == _DEFAULT_CONTEXT_WINDOW


def test_cloud_registry_checked_before_openrouter_catalog(tmp_path: Path) -> None:
    """A model id that happens to also appear in the OpenRouter cache (unlikely
    in practice, but the lookup order matters) still resolves via the
    compiled-in cloud registry first."""
    _write_openrouter_cache(tmp_path, [{"id": "claude-sonnet-5", "context_length": 1}])
    assert get_context_window("claude-sonnet-5", tmp_path) == 1_000_000
