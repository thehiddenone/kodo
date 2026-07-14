"""Behavioral tests for the reasoning-budget / thinking-tier mechanism.

Covers three things the flavors refactor put at risk (see doc/
LOCAL_INFERENCE.md §2a):

1. ``QWEN_TIER_TOKEN_BUDGETS["unlimited"]`` is a real finite cap (1.5x
   "huge"), not the old ``-1``/no-limit sentinel.
2. ``_build_thinking_extra_body`` (``kodo/llms/llamacpp/_llama.py``) sizes
   per-request ``max_tokens`` against the resolved tier's budget plus
   headroom, instead of a flat constant that could collide with (or be
   smaller than) the budget itself.
3. ``ensure_llama_running`` (``kodo/llms/llamacpp/_manager.py``) force-
   assigns ``--reasoning-budget``/``--reasoning-budget-message`` at launch
   regardless of what a flavor's own ``llama_args`` says — the second line of
   defense behind ``add_flavor``/``update_flavor`` stripping them at save
   time (covered in test_llm_flavors.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.llms import (
    QWEN_REASONING_BUDGET_FAMILY,
    QWEN_TIER_TOKEN_BUDGETS,
    REASONING_BUDGET_MESSAGE,
    LocalLLMEntry,
)
from kodo.llms.llamacpp import _manager
from kodo.llms.llamacpp._llama import _DEFAULT_MAX_TOKENS, _build_thinking_extra_body

# ---------------------------------------------------------------------------
# QWEN_TIER_TOKEN_BUDGETS — "unlimited" is 1.5x "huge" for every family member
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("base_llm", sorted(QWEN_REASONING_BUDGET_FAMILY))
def test_unlimited_tier_is_one_and_a_half_times_huge(base_llm: str) -> None:
    tiers = QWEN_TIER_TOKEN_BUDGETS[base_llm]
    assert tiers["unlimited"] == int(tiers["huge"] * 1.5)


@pytest.mark.parametrize("base_llm", sorted(QWEN_REASONING_BUDGET_FAMILY))
def test_every_tier_is_a_positive_finite_budget(base_llm: str) -> None:
    # No -1 sentinel left anywhere in the table — every tier, including
    # "unlimited", is a real number now.
    assert all(budget > 0 for budget in QWEN_TIER_TOKEN_BUDGETS[base_llm].values())


# ---------------------------------------------------------------------------
# _build_thinking_extra_body — (extra_body, max_tokens) sizing
# ---------------------------------------------------------------------------


def test_qwen_max_tokens_is_budget_plus_headroom() -> None:
    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier="high")
    budget = QWEN_TIER_TOKEN_BUDGETS["Qwen36-27B"]["high"]
    assert extra_body["thinking_budget_tokens"] == budget
    assert max_tokens == budget + 8192
    # The old flat cap (8192) must never be able to equal or exceed the
    # resolved max_tokens minus headroom — i.e. there must always be real
    # room left over beyond the full thinking budget.
    assert max_tokens > _DEFAULT_MAX_TOKENS


def test_qwen_unlimited_tier_max_tokens_has_headroom_too() -> None:
    # Before this fix "unlimited" resolved to a literal -1 (uncapped)
    # thinking_budget_tokens with a flat 8192 max_tokens — meaning an
    # unbounded amount of reasoning could consume the entire response with
    # zero room for the exhaustion message. It must now be a large but finite
    # number with real headroom on top.
    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier="unlimited")
    budget = QWEN_TIER_TOKEN_BUDGETS["Qwen36-27B"]["unlimited"]
    assert budget > 0
    assert extra_body["thinking_budget_tokens"] == budget
    assert max_tokens == budget + 8192


def test_qwen35_9b_still_forces_enable_thinking() -> None:
    extra_body, _ = _build_thinking_extra_body("Qwen35-9B", override_tier="medium")
    assert extra_body["chat_template_kwargs"] == {"enable_thinking": True}


def test_gpt_oss_has_no_numeric_budget_and_keeps_flat_max_tokens() -> None:
    extra_body, max_tokens = _build_thinking_extra_body("GPT-OSS-120B", override_tier="high")
    assert extra_body == {"chat_template_kwargs": {"reasoning_effort": "high"}}
    assert max_tokens == _DEFAULT_MAX_TOKENS


def test_no_thinking_family_keeps_flat_max_tokens() -> None:
    extra_body, max_tokens = _build_thinking_extra_body("", override_tier=None)
    assert extra_body == {}
    assert max_tokens == _DEFAULT_MAX_TOKENS


def test_invalid_override_tier_falls_back_to_family_default_budget() -> None:
    extra_body, max_tokens = _build_thinking_extra_body("Qwen36-27B", override_tier="not-a-tier")
    default_budget = QWEN_TIER_TOKEN_BUDGETS["Qwen36-27B"]["unlimited"]
    assert extra_body["thinking_budget_tokens"] == default_budget
    assert max_tokens == default_budget + 8192


# ---------------------------------------------------------------------------
# ensure_llama_running — forces the reasoning-cap args regardless of a
# flavor's own (legacy, pre-restriction) llama_args
# ---------------------------------------------------------------------------


class _FakeInstall:
    def __init__(self, executable: Path) -> None:
        self.executable = executable


class _FakeLlamaServer:
    """Stands in for LlamaServer: records the llama_args it was launched
    with instead of actually spawning a process."""

    last_llama_args: dict[str, str] | None = None

    def __init__(self, config: object, llama_args: dict[str, str], *, flavor_id: str = "") -> None:
        type(self).last_llama_args = dict(llama_args)
        self.model_name = getattr(config, "model_name", "")

    @classmethod
    def get_active_llama_server(cls) -> _FakeLlamaServer | None:
        return None

    async def start(self) -> None:
        return None


async def test_ensure_llama_running_forces_reasoning_cap_args_over_a_legacy_flavor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a flavor saved before add_flavor/update_flavor stripped
    # RESERVED_REASONING_CAP_ARGS — written directly to the registry file,
    # bypassing add_flavor, the same technique test_llm_flavors.py uses to
    # simulate other pre-restriction legacy data. Flavor/active-flavor
    # storage is keyed purely by entry name (see get_flavors/
    # get_effective_flavor_id, both kodo_dir + entry-attribute based, no
    # registry lookup) — the entry passed to ensure_llama_running never has
    # to be a real registered entry, only to share the same name.
    model_path = tmp_path / "fake-model.gguf"
    model_path.write_text("fake gguf")

    registry_file = tmp_path / "etc" / "local-llm-registry.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        json.dumps(
            {
                "flavors": {
                    "legacy-qwen": [
                        {
                            "id": "legacy",
                            "name": "Legacy",
                            "llama_args": {
                                "--reasoning-budget": "4096",
                                "--reasoning-budget-message": "some stale message",
                                "--n-gpu-layers": "20",
                            },
                        }
                    ]
                },
                "active_flavors": {"legacy-qwen": "legacy"},
            }
        )
    )

    entry = LocalLLMEntry(
        name="legacy-qwen",
        kind="custom_file",
        path=str(model_path),
        base_llm="Qwen36-27B",
    )

    monkeypatch.setattr(
        _manager, "find_installed", lambda kodo_dir: _FakeInstall(Path("/fake/llama-server"))
    )
    monkeypatch.setattr(_manager, "LlamaServer", _FakeLlamaServer)

    await _manager.ensure_llama_running(entry, tmp_path)

    captured = _FakeLlamaServer.last_llama_args
    assert captured is not None
    assert captured["--reasoning-budget"] == "-1"
    assert captured["--reasoning-budget-message"] == REASONING_BUDGET_MESSAGE
    # The flavor's own unrelated arg must still survive — only the two
    # reserved keys are forced.
    assert captured["--n-gpu-layers"] == "20"
