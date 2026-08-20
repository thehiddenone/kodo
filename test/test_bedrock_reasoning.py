"""Tests for ``kodo.llms.bedrock._reasoning`` -- tier -> request-field dispatch.

The rule this pins is the one that makes Bedrock different from every other
vendor: ``additionalModelRequestFields`` is validated against the target
model, so an unrecognised family must get **no** reasoning fields rather than
a best guess -- a wrong guess is a hard ``ValidationException``, not a no-op.
"""

from __future__ import annotations

import pytest

from kodo.llms import cloud_thinking_default_tier, cloud_thinking_tiers
from kodo.llms.bedrock._reasoning import (
    DEFAULT_TIER,
    TIERS,
    max_tokens_for,
    reasoning_fields_for,
)

# ---------------------------------------------------------------------------
# Spec agreement with the one thinking-family table
# ---------------------------------------------------------------------------


def test_tiers_match_the_cloud_thinking_table() -> None:
    """A tier advertised to the client but unknown here would silently degrade."""
    assert cloud_thinking_tiers("bedrock") == TIERS


def test_default_tier_matches_the_cloud_thinking_table() -> None:
    assert cloud_thinking_default_tier("bedrock") == DEFAULT_TIER


# ---------------------------------------------------------------------------
# reasoning_fields_for
# ---------------------------------------------------------------------------


def test_adaptive_claude_model_gets_thinking_and_effort() -> None:
    fields = reasoning_fields_for("anthropic.claude-opus-5", "low")
    assert fields == {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}


def test_cross_region_profile_prefix_still_matches() -> None:
    """Bedrock ids carry a us./eu./apac. inference-profile prefix."""
    fields = reasoning_fields_for("us.anthropic.claude-opus-5", "medium")
    assert fields["output_config"] == {"effort": "medium"}


def test_version_suffix_still_matches() -> None:
    fields = reasoning_fields_for("us.anthropic.claude-opus-4-6-v1:0", "high")
    assert fields["output_config"] == {"effort": "high"}


@pytest.mark.parametrize(
    "model",
    [
        "amazon.nova-pro-v1:0",
        "meta.llama4-maverick",
        "us.deepseek.r1-v1:0",
        "mistral.mistral-large-2407-v1:0",
    ],
)
def test_non_claude_models_get_no_reasoning_fields(model: str) -> None:
    """An unknown field here is a 400 from Bedrock, not an ignored parameter."""
    assert reasoning_fields_for(model, "max") == {}


def test_older_claude_without_adaptive_support_gets_nothing() -> None:
    assert reasoning_fields_for("anthropic.claude-3-5-sonnet-20241022-v2:0", "high") == {}


def test_unknown_tier_falls_back_to_default() -> None:
    fields = reasoning_fields_for("anthropic.claude-opus-5", "nonsense")
    assert fields["output_config"] == {"effort": DEFAULT_TIER}


def test_none_tier_falls_back_to_default() -> None:
    fields = reasoning_fields_for("anthropic.claude-opus-5", None)
    assert fields["output_config"] == {"effort": DEFAULT_TIER}


@pytest.mark.parametrize("tier", ["xhigh", "max"])
def test_deep_tiers_clamp_on_models_that_reject_them(tier: str) -> None:
    """xhigh/max are documented as Opus 5 / Opus 4.6 only."""
    fields = reasoning_fields_for("anthropic.claude-sonnet-4-6", tier)
    assert fields["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("tier", ["xhigh", "max"])
def test_deep_tiers_pass_through_on_models_that_accept_them(tier: str) -> None:
    fields = reasoning_fields_for("us.anthropic.claude-opus-5", tier)
    assert fields["output_config"] == {"effort": tier}


# ---------------------------------------------------------------------------
# max_tokens_for
# ---------------------------------------------------------------------------


def test_default_tier_leaves_max_tokens_to_the_model() -> None:
    assert max_tokens_for("anthropic.claude-opus-5", "high") is None


def test_deep_tiers_raise_the_ceiling() -> None:
    assert max_tokens_for("anthropic.claude-opus-5", "xhigh") == 16384
    assert max_tokens_for("anthropic.claude-opus-5", "max") == 32768


def test_clamped_model_keeps_the_model_default_ceiling() -> None:
    """The tier was clamped to `high`, so there is nothing extra to make room for."""
    assert max_tokens_for("anthropic.claude-sonnet-4-6", "max") is None


def test_non_claude_model_never_gets_a_ceiling() -> None:
    assert max_tokens_for("amazon.nova-pro-v1:0", "max") is None
