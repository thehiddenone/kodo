"""Behavioral tests for local-LLM knobs and profiles (:mod:`kodo.llms.local_registry`).

An entry is launched under one of two things (doc/LLM_REGISTRY.md §4.6): its
**Default profile** — computed from ``base_llama_args`` plus its knobs, never
stored as a profile — or one of zero or more **user-defined profiles**, raw
arg sets that fully replace it.

These tests exercise the registry-layer CRUD and resolution logic directly
(no WS server) — WS-handler-level behavior (wire payload shape,
restart-on-change) is covered separately in test_server_integration.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms.llamacpp._llama_server import LlamaServer, LlamaServerConfig
from kodo.llms.local_registry import (
    RESERVED_REASONING_CAP_ARGS,
    SHARED_KNOBS,
    KnobKind,
    KnobOption,
    LlamaKnob,
    LlmProfile,
    LocalLLMEntry,
    _catalog,
    add_local_entry,
    add_profile,
    get_active_profile,
    get_knob_selections,
    get_local_registry,
    get_profiles,
    knob_owned_flags,
    knob_selection_args,
    make_yarn_context_knob,
    parse_llama_args_text,
    remove_local_entry,
    remove_profile,
    resolve_context_window,
    resolve_default_profile_args,
    resolve_effective_llama_config,
    set_active_profile,
    set_knobs,
    update_profile,
    validate_knobs,
)

#: A two-option dropdown owning one flag — the smallest knob that can be
#: switched, used wherever a test only needs "some knob that changes an arg".
_MODE_KNOB = LlamaKnob(
    id="test-mode",
    name="Mode",
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(id="off", name="Off"),
        KnobOption(id="fast", name="Fast", llama_args={"--test-mode": "fast"}),
    ),
    default_option="off",
)

_NUMBER_KNOB = LlamaKnob(
    id="test-count",
    name="Count",
    kind=KnobKind.NUMBER,
    flag="--test-count",
    minimum=0,
)

_BASE_ENTRY = LocalLLMEntry(
    name="fake-model",
    kind="hardcoded_hf",
    repo_id="acme/fake-model",
    filename="fake-model.gguf",
    context_window=262_144,
    base_llama_args={"--ctx-size": "0", "--jinja": ""},
    knobs=(_MODE_KNOB, _NUMBER_KNOB),
)


@pytest.fixture(autouse=True)
def _fake_hardcoded_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the real (large, evolving) hardcoded model list.

    One entry with two toy knobs, plus one that overrides a knob's default via
    ``knob_defaults`` — enough to exercise every code path without depending
    on which real models happen to ship which knobs today.
    """
    with_default = LocalLLMEntry(
        name="fake-model-preset",
        kind="hardcoded_hf",
        repo_id="acme/fake-model-2",
        filename="fake-model-2.gguf",
        context_window=131_072,
        base_llama_args={"--ctx-size": "0"},
        knobs=(_MODE_KNOB,),
        knob_defaults={"test-mode": "fast"},
    )
    monkeypatch.setattr(_catalog, "_HARDCODED_LOCAL_MODELS", (_BASE_ENTRY, with_default))


def _entry(kodo_dir: Path, name: str = "fake-model") -> LocalLLMEntry:
    return get_local_registry(kodo_dir)[name]


# ---------------------------------------------------------------------------
# parse_llama_args_text — the profile editor's raw multi-line box
# ---------------------------------------------------------------------------


def test_parse_llama_args_text_one_flag_per_line() -> None:
    text = "--ctx-size 1048576\n--rope-scaling yarn\n--rope-scale 4\n"
    assert parse_llama_args_text(text) == {
        "--ctx-size": "1048576",
        "--rope-scaling": "yarn",
        "--rope-scale": "4",
    }


def test_parse_llama_args_text_bare_flag_gets_empty_value() -> None:
    assert parse_llama_args_text("--jinja\n") == {"--jinja": ""}


def test_parse_llama_args_text_skips_blank_and_non_flag_lines() -> None:
    assert parse_llama_args_text("\n\nnot a flag\n--jinja\n") == {"--jinja": ""}


def test_parse_llama_args_text_non_string_input_is_empty() -> None:
    assert parse_llama_args_text(None) == {}
    assert parse_llama_args_text(42) == {}


def test_parse_llama_args_text_value_keeps_internal_spaces() -> None:
    assert parse_llama_args_text("--chat-template a b c") == {"--chat-template": "a b c"}


# ---------------------------------------------------------------------------
# Knob validation — the "no two knobs own the same flag" invariant
# ---------------------------------------------------------------------------


def test_validate_knobs_accepts_the_shipped_shared_knobs() -> None:
    validate_knobs(SHARED_KNOBS, context="SHARED_KNOBS")


def test_validate_knobs_rejects_two_knobs_owning_the_same_flag() -> None:
    other = LlamaKnob(
        id="test-other",
        name="Other",
        kind=KnobKind.DROPDOWN,
        options=(
            KnobOption(id="off", name="Off"),
            # Same flag as _MODE_KNOB's "fast" option.
            KnobOption(id="on", name="On", llama_args={"--test-mode": "slow"}),
        ),
    )
    with pytest.raises(ValueError, match="both own '--test-mode'"):
        validate_knobs((_MODE_KNOB, other), context="entry")


def test_validate_knobs_compares_reachable_flags_not_just_the_default_state() -> None:
    """A collision only some option pairs produce is still a collision."""
    other = LlamaKnob(
        id="test-other",
        name="Other",
        kind=KnobKind.DROPDOWN,
        options=(
            KnobOption(id="off", name="Off"),
            KnobOption(id="on", name="On", llama_args={"--test-count": "3"}),
        ),
    )
    # Neither knob writes --test-count in its *default* state, but both can.
    with pytest.raises(ValueError, match="both own '--test-count'"):
        validate_knobs((_NUMBER_KNOB, other), context="entry")


def test_validate_knobs_rejects_a_duplicate_knob_id() -> None:
    with pytest.raises(ValueError, match="listed more than once"):
        validate_knobs((_MODE_KNOB, _MODE_KNOB), context="entry")


def test_validate_knobs_rejects_two_different_knobs_under_one_id() -> None:
    impostor = LlamaKnob(
        id="test-mode",
        name="Different",
        kind=KnobKind.DROPDOWN,
        options=(KnobOption(id="off", name="Off"), KnobOption(id="on", name="On")),
    )
    with pytest.raises(ValueError, match="two different knobs share the id"):
        validate_knobs((_MODE_KNOB, impostor), context="entry")


def test_validate_knobs_rejects_a_checkbox_without_off_on_options() -> None:
    bad = LlamaKnob(
        id="test-check",
        name="Check",
        kind=KnobKind.CHECKBOX,
        options=(KnobOption(id="no", name="No"), KnobOption(id="yes", name="Yes")),
    )
    with pytest.raises(ValueError, match="CHECKBOX knob"):
        validate_knobs((bad,), context="entry")


def test_validate_knobs_rejects_a_dropdown_with_one_option() -> None:
    bad = LlamaKnob(
        id="test-one",
        name="One",
        kind=KnobKind.DROPDOWN,
        options=(KnobOption(id="only", name="Only"),),
    )
    with pytest.raises(ValueError, match="at least two options"):
        validate_knobs((bad,), context="entry")


def test_validate_knobs_rejects_a_number_knob_with_no_flag() -> None:
    with pytest.raises(ValueError, match="must name a flag"):
        validate_knobs((LlamaKnob(id="n", name="N", kind=KnobKind.NUMBER),), context="entry")


def test_validate_knobs_rejects_a_default_option_that_does_not_exist() -> None:
    bad = LlamaKnob(
        id="test-bad-default",
        name="Bad",
        kind=KnobKind.DROPDOWN,
        options=(KnobOption(id="a", name="A"), KnobOption(id="b", name="B")),
        default_option="c",
    )
    with pytest.raises(ValueError, match="not one of its options"):
        validate_knobs((bad,), context="entry")


def test_every_shipped_entry_has_a_valid_knob_set() -> None:
    """The real catalog, not the fixture's — the import-time check, re-asserted."""
    from kodo.llms.local_registry._catalog import _HARDCODED_LOCAL_MODELS

    assert _HARDCODED_LOCAL_MODELS
    for entry in _HARDCODED_LOCAL_MODELS:
        validate_knobs(entry.knobs, context=entry.name)


def test_shared_sampling_knobs_never_enable_a_repetition_penalty() -> None:
    """doc/QUANT_SAMPLING.md §3f — DRY et al. break verbatim identifier recall."""
    banned = {
        "--repeat-penalty",
        "--presence-penalty",
        "--frequency-penalty",
        "--dry-multiplier",
        "--dry-base",
        "--dry-allowed-length",
    }
    for knob in SHARED_KNOBS:
        assert not (knob_owned_flags(knob) & banned), knob.id


# ---------------------------------------------------------------------------
# Knob composition
# ---------------------------------------------------------------------------


def test_knob_selection_args_uses_the_knobs_own_default_when_unset() -> None:
    assert knob_selection_args((_MODE_KNOB,), {}, {}) == {}


def test_knob_selection_args_applies_an_explicit_selection() -> None:
    assert knob_selection_args((_MODE_KNOB,), {"test-mode": "fast"}, {}) == {"--test-mode": "fast"}


def test_knob_selection_args_entry_default_beats_the_knobs_own() -> None:
    assert knob_selection_args((_MODE_KNOB,), {}, {"test-mode": "fast"}) == {"--test-mode": "fast"}


def test_knob_selection_args_explicit_selection_beats_the_entry_default() -> None:
    assert knob_selection_args((_MODE_KNOB,), {"test-mode": "off"}, {"test-mode": "fast"}) == {}


def test_knob_selection_args_ignores_a_knob_the_entry_does_not_offer() -> None:
    assert knob_selection_args((_MODE_KNOB,), {"nonexistent": "x"}, {}) == {}


def test_knob_selection_args_falls_back_when_the_stored_option_is_gone() -> None:
    assert knob_selection_args((_MODE_KNOB,), {"test-mode": "removed-option"}, {}) == {}


def test_number_knob_emits_nothing_when_unset() -> None:
    assert knob_selection_args((_NUMBER_KNOB,), {}, {}) == {}


def test_number_knob_emits_its_flag_when_set() -> None:
    assert knob_selection_args((_NUMBER_KNOB,), {"test-count": "24"}, {}) == {"--test-count": "24"}


def test_yarn_context_knob_native_option_writes_nothing() -> None:
    knob = make_yarn_context_knob(
        knob_id="context-test", arch_key="testarch", native_context=8192, sizes=(524_288,)
    )
    assert knob.llama_args_for("native") == {}


def test_yarn_context_knob_derives_rope_scale_from_the_native_context() -> None:
    knob = make_yarn_context_knob(
        knob_id="context-test", arch_key="testarch", native_context=8192, sizes=(524_288,)
    )
    assert knob.llama_args_for("512k") == {
        "--ctx-size": "524288",
        "--rope-scaling": "yarn",
        "--rope-scale": "64.0",
        "--yarn-orig-ctx": "8192",
        "--override-kv": "testarch.context_length=int:524288",
    }


def test_yarn_context_knob_rejects_a_size_below_the_native_context() -> None:
    with pytest.raises(ValueError, match="not larger than the native context"):
        make_yarn_context_knob(
            knob_id="context-test", arch_key="testarch", native_context=8192, sizes=(4096,)
        )


# ---------------------------------------------------------------------------
# Knob state persistence (set_knobs / get_knob_selections)
# ---------------------------------------------------------------------------


def test_get_knob_selections_is_never_sparse(tmp_path: Path) -> None:
    assert get_knob_selections(tmp_path, _entry(tmp_path)) == {"test-mode": "off", "test-count": ""}


def test_get_knob_selections_honours_the_entrys_knob_defaults(tmp_path: Path) -> None:
    assert get_knob_selections(tmp_path, _entry(tmp_path, "fake-model-preset")) == {
        "test-mode": "fast"
    }


def test_set_knobs_round_trips(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"test-mode": "fast", "test-count": "8"})
    assert get_knob_selections(tmp_path, _entry(tmp_path)) == {
        "test-mode": "fast",
        "test-count": "8",
    }


def test_set_knobs_stores_sparsely(tmp_path: Path) -> None:
    """A selection equal to the default isn't written, so changing the default later reaches it."""
    import json

    set_knobs(tmp_path, "fake-model", {"test-mode": "off", "test-count": "8"})
    stored = json.loads((tmp_path / "etc" / "local-llm-registry.json").read_text())
    assert stored["knob_selections"]["fake-model"] == {"test-count": "8"}


def test_set_knobs_replaces_the_whole_selection(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"test-mode": "fast", "test-count": "8"})
    set_knobs(tmp_path, "fake-model", {"test-count": "8"})
    assert get_knob_selections(tmp_path, _entry(tmp_path))["test-mode"] == "off"


def test_set_knobs_rejects_an_unknown_option(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="has no option"):
        set_knobs(tmp_path, "fake-model", {"test-mode": "nope"})


def test_set_knobs_rejects_a_non_numeric_number_knob_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="takes a number"):
        set_knobs(tmp_path, "fake-model", {"test-count": "many"})


def test_set_knobs_ignores_an_unknown_knob_id(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"no-such-knob": "x"})
    assert get_knob_selections(tmp_path, _entry(tmp_path))["test-mode"] == "off"


def test_set_knobs_rejects_an_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        set_knobs(tmp_path, "nope", {})


def test_set_knobs_rejects_a_custom_server_url_entry(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="remote", kind="custom_server_url", url="http://x:1", knobs=()),
    )
    with pytest.raises(ValueError, match="do not support profiles"):
        set_knobs(tmp_path, "remote", {})


# ---------------------------------------------------------------------------
# Default profile resolution
# ---------------------------------------------------------------------------


def test_default_profile_args_are_base_args_when_every_knob_is_default(tmp_path: Path) -> None:
    assert resolve_default_profile_args(tmp_path, _entry(tmp_path)) == {
        "--ctx-size": "0",
        "--jinja": "",
    }


def test_default_profile_args_layer_knob_args_over_base(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"test-mode": "fast"})
    assert resolve_default_profile_args(tmp_path, _entry(tmp_path)) == {
        "--ctx-size": "0",
        "--jinja": "",
        "--test-mode": "fast",
    }


def test_a_knob_arg_overrides_the_same_base_arg(tmp_path: Path) -> None:
    """Base args are the floor; a context knob's --ctx-size must win over the base 0."""
    knob = make_yarn_context_knob(
        knob_id="context-test", arch_key="testarch", native_context=8192, sizes=(524_288,)
    )
    entry = LocalLLMEntry(
        name="ctx-model",
        kind="hardcoded_hf",
        base_llama_args={"--ctx-size": "0"},
        knobs=(knob,),
    )
    args = dict(entry.base_llama_args)
    args.update(knob_selection_args(entry.knobs, {"context-test": "512k"}, {}))
    assert args["--ctx-size"] == "524288"


# ---------------------------------------------------------------------------
# User-defined profiles
# ---------------------------------------------------------------------------


def test_an_entry_starts_with_no_user_defined_profiles(tmp_path: Path) -> None:
    assert get_profiles(tmp_path, _entry(tmp_path)) == ()


def test_add_profile_auto_generates_slug_id(tmp_path: Path) -> None:
    profile = add_profile(tmp_path, "fake-model", "My Profile")
    assert profile.id == "my-profile"
    assert get_profiles(tmp_path, _entry(tmp_path)) == (profile,)


def test_add_profile_dedupes_id_when_different_names_share_a_slug(tmp_path: Path) -> None:
    add_profile(tmp_path, "fake-model", "my profile")
    assert add_profile(tmp_path, "fake-model", "My  Profile").id == "my-profile-2"


def test_add_profile_rejects_duplicate_name(tmp_path: Path) -> None:
    add_profile(tmp_path, "fake-model", "Same")
    with pytest.raises(ValueError, match="already exists"):
        add_profile(tmp_path, "fake-model", "Same")


def test_add_profile_rejects_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        add_profile(tmp_path, "nope", "P")


def test_add_profile_rejects_blank_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name is required"):
        add_profile(tmp_path, "fake-model", "   ")


def test_add_profile_rejects_custom_server_url_entry(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="remote", kind="custom_server_url", url="http://x:1", knobs=()),
    )
    with pytest.raises(ValueError, match="do not support profiles"):
        add_profile(tmp_path, "remote", "P")


def test_add_profile_works_on_a_custom_hf_entry_too(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="mine", kind="custom_hf", repo_id="a/b", filename="c.gguf"),
    )
    assert add_profile(tmp_path, "mine", "P").id == "p"


def test_add_profile_strips_reserved_reasoning_cap_args(tmp_path: Path) -> None:
    profile = add_profile(
        tmp_path,
        "fake-model",
        "P",
        llama_args={"--ctx-size": "8", RESERVED_REASONING_CAP_ARGS[0]: "-1"},
    )
    assert profile.llama_args == {"--ctx-size": "8"}


def test_add_profile_strips_server_managed_args(tmp_path: Path) -> None:
    profile = add_profile(
        tmp_path,
        "fake-model",
        "P",
        llama_args={"--ctx-size": "8", "--port": "9999", "--model": "/x.gguf"},
    )
    assert profile.llama_args == {"--ctx-size": "8"}


def test_update_profile_edits_in_place_keeping_its_id(tmp_path: Path) -> None:
    created = add_profile(tmp_path, "fake-model", "P", llama_args={"--ctx-size": "8"})
    updated = update_profile(
        tmp_path, "fake-model", created.id, "Renamed", llama_args={"--ctx-size": "16"}
    )
    assert updated.id == created.id
    assert get_profiles(tmp_path, _entry(tmp_path)) == (
        LlmProfile(id=created.id, name="Renamed", llama_args={"--ctx-size": "16"}),
    )


def test_update_profile_rejects_unknown_profile_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown profile"):
        update_profile(tmp_path, "fake-model", "nope", "P")


def test_update_profile_rejects_blank_name(tmp_path: Path) -> None:
    created = add_profile(tmp_path, "fake-model", "P")
    with pytest.raises(ValueError, match="name is required"):
        update_profile(tmp_path, "fake-model", created.id, "  ")


def test_update_profile_rejects_duplicate_name(tmp_path: Path) -> None:
    add_profile(tmp_path, "fake-model", "A")
    b = add_profile(tmp_path, "fake-model", "B")
    with pytest.raises(ValueError, match="already exists"):
        update_profile(tmp_path, "fake-model", b.id, "A")


def test_update_profile_allows_keeping_its_own_unchanged_name(tmp_path: Path) -> None:
    created = add_profile(tmp_path, "fake-model", "A")
    assert update_profile(tmp_path, "fake-model", created.id, "A").name == "A"


def test_remove_profile_rejects_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No profile"):
        remove_profile(tmp_path, "fake-model", "nope")


def test_remove_profile_removes_only_that_one(tmp_path: Path) -> None:
    a = add_profile(tmp_path, "fake-model", "A")
    b = add_profile(tmp_path, "fake-model", "B")
    remove_profile(tmp_path, "fake-model", a.id)
    assert [p.id for p in get_profiles(tmp_path, _entry(tmp_path))] == [b.id]


def test_remove_profile_resets_the_active_selection_to_default(tmp_path: Path) -> None:
    created = add_profile(tmp_path, "fake-model", "A")
    set_active_profile(tmp_path, "fake-model", created.id)
    remove_profile(tmp_path, "fake-model", created.id)
    assert get_active_profile(tmp_path, "fake-model") == ""


def test_remove_profile_leaves_an_unrelated_active_selection_alone(tmp_path: Path) -> None:
    a = add_profile(tmp_path, "fake-model", "A")
    b = add_profile(tmp_path, "fake-model", "B")
    set_active_profile(tmp_path, "fake-model", b.id)
    remove_profile(tmp_path, "fake-model", a.id)
    assert get_active_profile(tmp_path, "fake-model") == b.id


# ---------------------------------------------------------------------------
# Active profile selection
# ---------------------------------------------------------------------------


def test_active_profile_defaults_to_empty(tmp_path: Path) -> None:
    assert get_active_profile(tmp_path, "fake-model") == ""


def test_set_active_profile_rejects_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        set_active_profile(tmp_path, "nope", "")


def test_set_active_profile_rejects_unknown_profile_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown profile"):
        set_active_profile(tmp_path, "fake-model", "nope")


def test_set_active_profile_accepts_empty_string_for_default(tmp_path: Path) -> None:
    created = add_profile(tmp_path, "fake-model", "A")
    set_active_profile(tmp_path, "fake-model", created.id)
    set_active_profile(tmp_path, "fake-model", "")
    assert get_active_profile(tmp_path, "fake-model") == ""


def test_get_active_profile_falls_back_to_default_when_the_selection_is_stale(
    tmp_path: Path,
) -> None:
    """A profile removed out from under the selection resolves to Default, not a neighbour."""
    import json

    a = add_profile(tmp_path, "fake-model", "A")
    add_profile(tmp_path, "fake-model", "B")
    set_active_profile(tmp_path, "fake-model", a.id)
    path = tmp_path / "etc" / "local-llm-registry.json"
    data = json.loads(path.read_text())
    data["profiles"]["fake-model"] = [p for p in data["profiles"]["fake-model"] if p["id"] != a.id]
    path.write_text(json.dumps(data))
    assert get_active_profile(tmp_path, "fake-model") == ""


# ---------------------------------------------------------------------------
# Context-window resolution
# ---------------------------------------------------------------------------


def test_get_context_size_reads_ctx_size_flag() -> None:
    assert (
        LlmProfile(id="x", name="x", llama_args={"--ctx-size": "1024"}).get_context_size() == 1024
    )


def test_get_context_size_reads_short_c_flag() -> None:
    assert LlmProfile(id="x", name="x", llama_args={"-c": "2048"}).get_context_size() == 2048


def test_get_context_size_prefers_ctx_size_over_short_c() -> None:
    profile = LlmProfile(id="x", name="x", llama_args={"--ctx-size": "1024", "-c": "2048"})
    assert profile.get_context_size() == 1024


def test_get_context_size_is_zero_when_ctx_size_is_zero() -> None:
    assert LlmProfile(id="x", name="x", llama_args={"--ctx-size": "0"}).get_context_size() == 0


def test_get_context_size_is_zero_when_absent() -> None:
    assert LlmProfile(id="x", name="x").get_context_size() == 0


def test_get_context_size_is_zero_when_unparseable() -> None:
    assert LlmProfile(id="x", name="x", llama_args={"--ctx-size": "big"}).get_context_size() == 0


def test_resolve_context_window_reads_ctx_size_flag() -> None:
    assert resolve_context_window(_BASE_ENTRY, {"--ctx-size": "4096"}) == 4096


def test_resolve_context_window_falls_back_when_ctx_size_is_zero() -> None:
    assert resolve_context_window(_BASE_ENTRY, {"--ctx-size": "0"}) == 262_144


def test_resolve_context_window_falls_back_when_absent() -> None:
    assert resolve_context_window(_BASE_ENTRY, {}) == 262_144


def test_resolve_context_window_falls_back_when_unparseable() -> None:
    assert resolve_context_window(_BASE_ENTRY, {"--ctx-size": "big"}) == 262_144


# ---------------------------------------------------------------------------
# resolve_effective_llama_config — what actually launches
# ---------------------------------------------------------------------------


def test_effective_config_with_no_active_profile_uses_the_default_profile(tmp_path: Path) -> None:
    args, ctx = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert args == {"--ctx-size": "0", "--jinja": ""}
    assert ctx == 262_144


def test_effective_config_reflects_a_knob_change(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"test-mode": "fast"})
    args, _ = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert args["--test-mode"] == "fast"


def test_effective_config_full_replace_not_merge(tmp_path: Path) -> None:
    """An active profile replaces the Default profile's args wholesale."""
    created = add_profile(tmp_path, "fake-model", "P", llama_args={"--ctx-size": "4096"})
    set_active_profile(tmp_path, "fake-model", created.id)
    args, ctx = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert args == {"--ctx-size": "4096"}
    assert "--jinja" not in args
    assert ctx == 4096


def test_effective_config_ignores_knobs_while_a_profile_is_active(tmp_path: Path) -> None:
    set_knobs(tmp_path, "fake-model", {"test-mode": "fast"})
    created = add_profile(tmp_path, "fake-model", "P", llama_args={"--ctx-size": "4096"})
    set_active_profile(tmp_path, "fake-model", created.id)
    args, _ = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert "--test-mode" not in args


def test_effective_config_profile_context_window_zero_inherits_the_entrys_own(
    tmp_path: Path,
) -> None:
    created = add_profile(tmp_path, "fake-model", "P", llama_args={"--ctx-size": "0"})
    set_active_profile(tmp_path, "fake-model", created.id)
    _, ctx = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert ctx == 262_144


def test_effective_config_for_a_custom_server_url_entry_is_empty(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(
            name="remote", kind="custom_server_url", url="http://x:1", knobs=(), context_window=99
        ),
    )
    assert resolve_effective_llama_config(tmp_path, _entry(tmp_path, "remote")) == ({}, 99)


# ---------------------------------------------------------------------------
# Custom entries: shared knobs attached on load, form args as base args
# ---------------------------------------------------------------------------


def test_a_custom_entry_gets_the_shared_knobs_on_load(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="mine", kind="custom_hf", repo_id="a/b", filename="c.gguf"),
    )
    assert _entry(tmp_path, "mine").knobs == SHARED_KNOBS


def test_a_custom_entrys_form_args_become_base_args_over_the_shared_ones(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(
            name="mine",
            kind="custom_hf",
            repo_id="a/b",
            filename="c.gguf",
            base_llama_args={"--threads": "4"},
        ),
    )
    args = resolve_default_profile_args(tmp_path, _entry(tmp_path, "mine"))
    assert args["--threads"] == "4"
    # The shared base args still apply — without --jinja, tool calling breaks.
    assert args["--jinja"] == ""


def test_a_custom_server_url_entry_gets_no_knobs(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="remote", kind="custom_server_url", url="http://x:1", knobs=()),
    )
    assert _entry(tmp_path, "remote").knobs == ()


def test_add_local_entry_never_persists_knobs(tmp_path: Path) -> None:
    """Knobs are code, re-attached on load — so a kodo upgrade reaches existing entries."""
    import json

    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="mine", kind="custom_hf", repo_id="a/b", filename="c.gguf"),
    )
    stored = json.loads((tmp_path / "etc" / "local-llm-registry.json").read_text())
    assert "knobs" not in stored["entries"][0]


def test_remove_local_entry_cleans_up_its_profiles_knobs_and_selection(tmp_path: Path) -> None:
    import json

    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="mine", kind="custom_hf", repo_id="a/b", filename="c.gguf"),
    )
    created = add_profile(tmp_path, "mine", "P")
    set_active_profile(tmp_path, "mine", created.id)
    set_knobs(tmp_path, "mine", {"tail-culling": "light"})
    remove_local_entry(tmp_path, "mine")
    stored = json.loads((tmp_path / "etc" / "local-llm-registry.json").read_text())
    assert "mine" not in stored.get("profiles", {})
    assert "mine" not in stored.get("active_profiles", {})
    assert "mine" not in stored.get("knob_selections", {})


# ---------------------------------------------------------------------------
# The resolved args reach llama-server's command line verbatim
# ---------------------------------------------------------------------------


def _cmd(llama_args: dict[str, str]) -> list[str]:
    server = LlamaServer(
        LlamaServerConfig(
            executable=Path("/bin/llama-server"),
            model_path=Path("/models/m.gguf"),
            kodo_dir=Path("/kodo"),
        ),
        llama_args,
    )
    return server._LlamaServer__build_command()  # type: ignore[attr-defined]


def test_build_command_with_no_llama_args_carries_only_server_flags() -> None:
    cmd = _cmd({})
    assert "--model" in cmd and "--port" in cmd
    assert "--ctx-size" not in cmd


def test_build_command_includes_every_resolved_llama_arg_verbatim() -> None:
    cmd = _cmd({"--ctx-size": "4096", "--n-gpu-layers": "-1"})
    assert cmd[cmd.index("--ctx-size") + 1] == "4096"
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "-1"


def test_build_command_bare_flag_has_no_trailing_empty_value() -> None:
    cmd = _cmd({"--jinja": "", "--ctx-size": "8"})
    after_jinja = cmd[cmd.index("--jinja") + 1 :]
    assert after_jinja == ["--ctx-size", "8"]
    assert "" not in cmd


def test_build_command_carries_the_default_profiles_jinja_flag(tmp_path: Path) -> None:
    args, _ = resolve_effective_llama_config(tmp_path, _entry(tmp_path))
    assert "--jinja" in _cmd(args)
