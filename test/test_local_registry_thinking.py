"""Behavioral tests for the local-LLM thinking-tier lookups in :mod:`kodo.llms.local_registry`.

Each catalog family (``base_llm``) belongs to at most one reasoning-tiering
mechanism; the family decides the ordered tier list and the default tier the
chat UI offers. Custom entries (``base_llm == ""``) and unknown families have
none.
"""

from __future__ import annotations

import pytest

from kodo.llms.local_registry import (
    GPT_OSS_REASONING_EFFORT_FAMILY,
    QWEN4EXP_REASONING_EFFORT_FAMILY,
    QWEN_REASONING_BUDGET_FAMILY,
    QWEN_TIER_TOKEN_BUDGETS,
    UNLIMITED_THINKING_TIER,
    local_thinking_default_tier,
    local_thinking_family,
    local_thinking_tiers,
)

#: (family set, family id, expected default tier) — defaults per the
#: ``local_thinking_default_tier`` contract.
_FAMILIES: tuple[tuple[frozenset[str], str, str], ...] = (
    (QWEN_REASONING_BUDGET_FAMILY, "qwen_reasoning_budget", "high"),
    (GPT_OSS_REASONING_EFFORT_FAMILY, "gpt_oss_reasoning_effort", "medium"),
    (QWEN4EXP_REASONING_EFFORT_FAMILY, "qwen4exp_reasoning_effort", "xhigh"),
)

_CASES: tuple[tuple[str, str, str], ...] = tuple(
    (base_llm, family, default)
    for members, family, default in _FAMILIES
    for base_llm in sorted(members)
)


def test_every_family_has_members() -> None:
    """Guards the parametrized cases below against silently running zero times."""
    for members, family, _ in _FAMILIES:
        assert members, family


def test_no_base_llm_belongs_to_two_families() -> None:
    members = [base_llm for base_llm, _, _ in _CASES]

    assert len(members) == len(set(members))


@pytest.mark.parametrize(("base_llm", "family", "default"), _CASES)
def test_a_family_member_reports_its_family_tiers_and_default(
    base_llm: str, family: str, default: str
) -> None:
    tiers = local_thinking_tiers(base_llm)

    assert local_thinking_family(base_llm) == family
    assert tiers
    assert len(tiers) == len(set(tiers))
    assert local_thinking_default_tier(base_llm) == default
    assert default in tiers


@pytest.mark.parametrize("base_llm", ["", "No-Such-Family"])
def test_a_base_llm_outside_every_family_has_no_thinking_control(base_llm: str) -> None:
    assert local_thinking_family(base_llm) is None
    assert local_thinking_tiers(base_llm) == ()
    assert local_thinking_default_tier(base_llm) == ""


@pytest.mark.parametrize("base_llm", sorted(QWEN_REASONING_BUDGET_FAMILY))
def test_qwen_budget_tiers_end_unlimited_and_have_budgets_for_the_rest(base_llm: str) -> None:
    tiers = local_thinking_tiers(base_llm)

    assert tiers[-1] == UNLIMITED_THINKING_TIER
    budgets = QWEN_TIER_TOKEN_BUDGETS.get(base_llm)
    if budgets is not None:
        assert set(budgets) == set(tiers[:-1])
        finite = [budgets[tier] for tier in tiers[:-1]]
        assert finite == sorted(finite), "tiers are ordered lowest intensity first"


@pytest.mark.parametrize(
    "members", [GPT_OSS_REASONING_EFFORT_FAMILY, QWEN4EXP_REASONING_EFFORT_FAMILY]
)
def test_effort_families_have_no_unlimited_tier(members: frozenset[str]) -> None:
    for base_llm in members:
        assert UNLIMITED_THINKING_TIER not in local_thinking_tiers(base_llm), base_llm
