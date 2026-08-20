"""Tests for ``kodo.llms._cloud_thinking`` -- the per-vendor thinking-tier table.

This table is the single source of truth two very different consumers read:
the engine (validating ``thinking_level.set``, re-deriving the session tier on
a vendor switch, deciding whether to send the tier at all) and the server's
client-facing ``thinking_families`` payload. Every test here is a
cross-consistency check between the table and something else that must agree
with it -- deliberately *not* a second copy of the tier lists, which would
only assert that someone typed the same thing twice.
"""

from __future__ import annotations

from kodo.llms import (
    CLOUD_THINKING_FAMILIES,
    cloud_thinking_default_tier,
    cloud_thinking_family,
    cloud_thinking_tiers,
    get_cloud_registry,
    get_cloud_vendor_module,
    local_thinking_family,
)
from kodo.llms.local_registry._thinking import (
    GPT_OSS_REASONING_EFFORT_FAMILY,
    QWEN_REASONING_BUDGET_FAMILY,
)
from kodo.server._app import _thinking_families_payload

# ---------------------------------------------------------------------------
# Coverage: every vendor kodo can actually talk to has a thinking control
# ---------------------------------------------------------------------------


def test_every_registered_cloud_vendor_has_a_thinking_family() -> None:
    """Read off the vendor registry, not a hardcoded vendor list -- a vendor
    added without an entry here would silently show "Thinking: N/A"."""
    for vendor in get_cloud_registry():
        assert vendor in CLOUD_THINKING_FAMILIES, vendor


def test_openrouter_has_a_thinking_family_despite_no_registry_entry() -> None:
    """OpenRouter is deliberately absent from _CLOUD_REGISTRY (fetched catalog),
    so the check above cannot see it -- it was also the first vendor to get
    this control (doc/LLM_REGISTRY.md §3a)."""
    assert "openrouter" in CLOUD_THINKING_FAMILIES


def test_every_thinking_family_vendor_has_a_plugin_module() -> None:
    """No entry for a vendor that cannot be dispatched to at all."""
    for vendor in CLOUD_THINKING_FAMILIES:
        assert get_cloud_vendor_module(vendor) is not None, vendor


# ---------------------------------------------------------------------------
# Internal consistency of each entry
# ---------------------------------------------------------------------------


def test_every_family_has_at_least_two_tiers() -> None:
    """A one-tier "control" would be a button that cannot change anything."""
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert len(entry.tiers) >= 2, vendor


def test_every_default_tier_is_one_of_that_families_tiers() -> None:
    """The engine seeds a new session with the default and validates every
    later change against the tier list -- a default outside it would be
    rejected by the very command that is supposed to accept it."""
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert entry.default in entry.tiers, vendor


def test_no_family_offers_an_off_tier() -> None:
    """kodo has no non-reasoning tier by design (doc/LLM_REGISTRY.md §4.5a) --
    and two vendors reject "none" with a 400 outright."""
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert "none" not in entry.tiers, vendor
        assert "off" not in entry.tiers, vendor


def test_tiers_have_no_duplicates() -> None:
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert len(set(entry.tiers)) == len(entry.tiers), vendor


# ---------------------------------------------------------------------------
# Namespace safety: cloud families share one payload keyspace with local ones
# ---------------------------------------------------------------------------


def test_family_slugs_are_unique_per_vendor() -> None:
    """The client keys its per-tier help text off `family`, so two vendors
    sharing a slug would show one vendor's tooltips for the other's tiers."""
    slugs = [e.family for e in CLOUD_THINKING_FAMILIES.values()]
    assert len(set(slugs)) == len(slugs)


def test_cloud_family_slugs_never_collide_with_local_ones() -> None:
    local_slugs = {"qwen_reasoning_budget", "gpt_oss_reasoning_effort"}
    assert {e.family for e in CLOUD_THINKING_FAMILIES.values()} & local_slugs == set()


def test_vendor_keys_never_collide_with_a_local_base_llm() -> None:
    """Both are keys in the same `thinking_families` map, and a collision would
    have one silently overwrite the other in the payload."""
    local_base_llms = QWEN_REASONING_BUDGET_FAMILY | GPT_OSS_REASONING_EFFORT_FAMILY
    assert set(CLOUD_THINKING_FAMILIES) & local_base_llms == set()


def test_no_vendor_key_is_a_thinking_capable_local_base_llm() -> None:
    for vendor in CLOUD_THINKING_FAMILIES:
        assert local_thinking_family(vendor) is None, vendor


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_accessors_agree_with_the_table() -> None:
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert cloud_thinking_family(vendor) == entry.family
        assert cloud_thinking_tiers(vendor) == entry.tiers
        assert cloud_thinking_default_tier(vendor) == entry.default


def test_accessors_are_empty_for_an_unknown_vendor() -> None:
    assert cloud_thinking_family("not-a-vendor") is None
    assert cloud_thinking_tiers("not-a-vendor") == ()
    assert cloud_thinking_default_tier("not-a-vendor") == ""


# ---------------------------------------------------------------------------
# The client-facing payload (doc/WS_PROTOCOL.md §5.12a)
# ---------------------------------------------------------------------------


def test_payload_carries_every_cloud_vendor_even_with_an_empty_local_registry() -> None:
    """Cloud entries are static, not derived from installed local models."""
    payload = _thinking_families_payload({})
    assert set(payload) == set(CLOUD_THINKING_FAMILIES)


def test_payload_entries_match_the_table_exactly() -> None:
    payload = _thinking_families_payload({})
    for vendor, entry in CLOUD_THINKING_FAMILIES.items():
        assert payload[vendor] == {
            "family": entry.family,
            "tiers": list(entry.tiers),
            "default": entry.default,
        }


def test_payload_tier_lists_are_json_safe_lists_not_tuples() -> None:
    """They ride a JSON WS frame; a tuple would serialise, but the client's
    ThinkingFamilyInfo.tiers is typed as an array -- keep the shapes aligned."""
    payload = _thinking_families_payload({})
    for value in payload.values():
        assert isinstance(value, dict)
        assert isinstance(value["tiers"], list)
