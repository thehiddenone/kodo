"""Behavioral tests for :class:`kodo.runtime.LlmSelection`.

A selection is one sidebar card; the engine overlays it onto the machine-wide
settings so every existing lookup follows the session
(doc/SESSION_LLM_BINDING.md §2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import (
    get_cloud_registry,
    get_cloud_vendor_display_name,
    get_launch_choice,
    get_local_registry,
    set_active_profile,
    set_knobs,
)
from kodo.runtime import LlmSelection, resolve_model_key

_ANTHROPIC = {
    "low": "claude-haiku-4-5-20251001",
    "medium": "claude-sonnet-5",
    "high": "claude-opus-5",
    "max": "claude-fable-5",
}


def _cloud_settings(**overrides: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "mode": "cloud",
        "active_cloud_vendor": "anthropic",
        "models": {
            "local": "some-local",
            "cloud": {"anthropic": dict(_ANTHROPIC), "openai": {"medium": "gpt-5.6-terra"}},
            "cloud_uniform": {"anthropic": {"enabled": False, "model_id": None}},
        },
    }
    settings.update(overrides)
    return settings


def _knobbed_entry(kodo_dir: Path) -> str:
    """A catalog entry with at least one knob that has two options."""
    for entry in get_local_registry(kodo_dir).values():
        for knob in entry.knobs:
            if len(knob.options) >= 2:
                return entry.name
    pytest.fail("no catalog entry has a knob with two options")


# ---------------------------------------------------------------------------
# resolve_model_key
# ---------------------------------------------------------------------------


def test_local_mode_resolves_every_capability_to_the_local_entry() -> None:
    settings = {"mode": "local", "models": {"local": "my-quant"}}
    assert {resolve_model_key(settings, c) for c in ("low", "max")} == {"my-quant"}


def test_cloud_mode_resolves_the_capability_tier() -> None:
    assert resolve_model_key(_cloud_settings(), "high") == "claude-opus-5"


def test_uniform_model_wins_over_tiers() -> None:
    settings = _cloud_settings()
    settings["models"]["cloud_uniform"]["anthropic"] = {  # type: ignore[index]
        "enabled": True,
        "model_id": "claude-sonnet-5",
    }
    assert resolve_model_key(settings, "max") == "claude-sonnet-5"


def test_openrouter_auto_wins_over_everything() -> None:
    settings = _cloud_settings(active_cloud_vendor="openrouter", openrouter_auto_mode=True)
    assert resolve_model_key(settings, "low") == "openrouter/auto"


def test_missing_tier_falls_back_to_medium_then_registry() -> None:
    settings = _cloud_settings(active_cloud_vendor="openai")
    assert resolve_model_key(settings, "low") == "gpt-5.6-terra"
    settings["models"]["cloud"]["openai"] = {}  # type: ignore[index]
    assert resolve_model_key(settings, "low") == get_cloud_registry()["openai"][0].model_id


# ---------------------------------------------------------------------------
# from_settings / overlay / JSON
# ---------------------------------------------------------------------------


def test_cloud_card_snapshots_the_vendor_tiers(tmp_path: Path) -> None:
    selection = LlmSelection.from_settings(_cloud_settings(), tmp_path)

    assert selection.kind == "cloud"
    assert selection.vendor == "anthropic"
    assert dict(selection.tiers) == _ANTHROPIC
    assert selection.uniform_enabled is False


def test_local_card_snapshots_the_entrys_stored_profile_and_knobs(tmp_path: Path) -> None:
    name = _knobbed_entry(tmp_path)
    entry = get_local_registry(tmp_path)[name]
    knob = next(k for k in entry.knobs if len(k.options) >= 2)
    other = next(
        o.id for o in knob.options if o.id != get_launch_choice(tmp_path, entry).knobs[knob.id]
    )
    set_knobs(tmp_path, name, {knob.id: other})

    selection = LlmSelection.from_settings({"mode": "local", "models": {"local": name}}, tmp_path)

    assert selection.entry == name
    assert selection.profile_id == ""
    assert selection.knobs[knob.id] == other


def test_a_snapshot_does_not_follow_later_sidebar_edits(tmp_path: Path) -> None:
    name = _knobbed_entry(tmp_path)
    entry = get_local_registry(tmp_path)[name]
    selection = LlmSelection.from_settings({"mode": "local", "models": {"local": name}}, tmp_path)
    before = dict(selection.knobs)
    knob = next(k for k in entry.knobs if len(k.options) >= 2)
    other = next(o.id for o in knob.options if o.id != before[knob.id])

    set_knobs(tmp_path, name, {knob.id: other})

    assert dict(selection.knobs) == before


def test_json_round_trip(tmp_path: Path) -> None:
    for selection in (
        LlmSelection(kind="local", entry="q", profile_id="p", knobs={"k": "v"}),
        LlmSelection.from_settings(_cloud_settings(), tmp_path),
    ):
        assert LlmSelection.from_json(selection.to_json()) == selection


def test_from_json_ignores_a_bindings_command_and_rejects_garbage() -> None:
    raw = {"kind": "local", "entry": "q", "profile_id": "", "knobs": {}, "command": ["x"]}
    assert LlmSelection.from_json(raw) == LlmSelection(kind="local", entry="q")
    assert LlmSelection.from_json(None) is None
    assert LlmSelection.from_json({"kind": "martian"}) is None


def test_overlay_moves_every_lookup_onto_the_card() -> None:
    global_settings = _cloud_settings()
    local = LlmSelection(kind="local", entry="my-quant")

    overlaid = local.overlay(global_settings)

    assert resolve_model_key(overlaid, "high") == "my-quant"
    assert global_settings["mode"] == "cloud"  # not mutated


def test_cloud_overlay_replaces_only_its_vendor() -> None:
    global_settings = _cloud_settings(mode="local")
    card = LlmSelection(
        kind="cloud", vendor="anthropic", tiers={"high": "claude-sonnet-5"}, uniform_enabled=False
    )

    overlaid = card.overlay(global_settings)

    assert resolve_model_key(overlaid, "high") == "claude-sonnet-5"
    assert overlaid["models"]["cloud"]["openai"] == {"medium": "gpt-5.6-terra"}  # type: ignore[index]


def test_cloud_label_names_the_vendor_and_the_agents_tier_model(tmp_path: Path) -> None:
    card = LlmSelection.from_settings(_cloud_settings(), tmp_path)
    entry = next(m for m in get_cloud_registry()["anthropic"] if m.model_id == "claude-opus-5")

    label = card.label(_cloud_settings(), "high", tmp_path)

    assert label == f"{get_cloud_vendor_display_name('anthropic')} · {entry.name}"


def test_local_label_is_the_entry_description(tmp_path: Path) -> None:
    name = _knobbed_entry(tmp_path)
    entry = get_local_registry(tmp_path)[name]

    label = LlmSelection(kind="local", entry=name).label({}, "medium", tmp_path)

    assert label == (entry.description or entry.name)


def test_profile_choice_is_carried_as_a_launch_choice(tmp_path: Path) -> None:
    card = LlmSelection(kind="local", entry="q", profile_id="tight", knobs={"ctx": "64k"})

    choice = card.launch_choice

    assert choice.profile_id == "tight"
    assert dict(choice.knobs) == {"ctx": "64k"}


def test_from_settings_reads_the_registry_active_profile(tmp_path: Path) -> None:
    from kodo.llms import add_profile

    name = _knobbed_entry(tmp_path)
    profile = add_profile(tmp_path, name, "Tight", llama_args={"--ctx-size": "4096"})
    set_active_profile(tmp_path, name, profile.id)

    selection = LlmSelection.from_settings({"mode": "local", "models": {"local": name}}, tmp_path)

    assert selection.profile_id == profile.id
