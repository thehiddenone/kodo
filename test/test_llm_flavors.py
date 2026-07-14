"""Behavioral tests for local-LLM flavors (:mod:`kodo.llms._local_registry`).

Flavors are the *only* source of llama-server launch args now (doc/
LLM_REGISTRY.md §4.6) — no ``LocalLLMEntry`` carries its own ``llama_args``.
Every entry that runs through llama-server has at least one flavor: a
``hardcoded_hf`` entry gets a built-in ``"default"`` one via
``LlamaFlavor.default_flavours_field`` unless it declares its own, and a
``custom_*`` entry gets its ``"default"`` seeded (as a *custom* flavor) from
its "Add local LLM" form when it's created (``_seed_default_flavor`` in
``kodo/server/_app.py`` — covered in test_server_integration.py, not here).

These tests exercise the registry-layer CRUD and resolution logic directly
(no WS server) — WS-handler-level behavior (wire payload shape,
restart-on-change) is covered separately in test_server_integration.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import _local_registry
from kodo.llms._local_registry import (
    LlamaFlavor,
    LocalLLMEntry,
    add_flavor,
    add_local_entry,
    get_active_flavor,
    get_effective_flavor_id,
    get_flavors,
    get_local_registry,
    parse_llama_args_text,
    remove_flavor,
    remove_local_entry,
    resolve_context_window,
    resolve_effective_llama_config,
    set_active_flavor,
    update_flavor,
)
from kodo.llms.llamacpp._llama_server import LlamaServer, LlamaServerConfig

# A plain hardcoded entry with no explicit `flavors=` — gets exactly one
# built-in flavor via the dataclass field's default factory, same as a real
# hardcoded model that doesn't need a different built-in default.
_BASE_ENTRY = LocalLLMEntry(
    name="fake-model",
    kind="hardcoded_hf",
    repo_id="acme/fake-model",
    filename="fake-model.gguf",
    context_window=262_144,
)

_DEFAULT_FLAVOR = LlamaFlavor.make_default_kv_q8()

_PREDEFINED_FLAVOR = LlamaFlavor(
    id="predefined-1m",
    name="Predefined 1M",
    llama_args={"--ctx-size": "1048576"},
)


@pytest.fixture(autouse=True)
def _fake_hardcoded_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the real (large, evolving) hardcoded model list.

    One plain entry (implicit built-in default flavor) plus one carrying an
    explicit predefined flavor — enough to exercise every code path
    (predefined-vs-custom merge/overriding, predefined-removal rejection)
    without depending on which real models happen to ship predefined
    flavors today.
    """
    with_flavor = LocalLLMEntry(
        name="fake-model-with-flavor",
        kind="hardcoded_hf",
        repo_id="acme/fake-model-2",
        filename="fake-model-2.gguf",
        context_window=131_072,
        flavors=(_PREDEFINED_FLAVOR,),
    )
    monkeypatch.setattr(_local_registry, "_HARDCODED_LOCAL_MODELS", (_BASE_ENTRY, with_flavor))


# ---------------------------------------------------------------------------
# parse_llama_args_text — the "manage flavors" modal's raw multi-line box
# ---------------------------------------------------------------------------


def test_parse_llama_args_text_one_flag_per_line() -> None:
    text = "--ctx-size 1048576\n--rope-scaling yarn\n--rope-scale 4\n"
    assert parse_llama_args_text(text) == {
        "--ctx-size": "1048576",
        "--rope-scaling": "yarn",
        "--rope-scale": "4",
    }


def test_parse_llama_args_text_bare_flag_gets_empty_value() -> None:
    assert parse_llama_args_text("--flash-attn") == {"--flash-attn": ""}


def test_parse_llama_args_text_skips_blank_and_non_flag_lines() -> None:
    text = "\n  \n--ctx-size 1048576\nnot a flag\n"
    assert parse_llama_args_text(text) == {"--ctx-size": "1048576"}


def test_parse_llama_args_text_non_string_input_is_empty() -> None:
    assert parse_llama_args_text(None) == {}
    assert parse_llama_args_text(42) == {}


def test_parse_llama_args_text_value_keeps_internal_spaces() -> None:
    # --override-tensor's regex value can itself contain no spaces in
    # practice, but the parser only splits on the *first* run of whitespace,
    # so a value with embedded spaces would still round-trip intact.
    assert parse_llama_args_text("--some-flag a value with spaces") == {
        "--some-flag": "a value with spaces"
    }


# ---------------------------------------------------------------------------
# get_flavors — predefined + custom merge, custom-by-id overrides predefined
# ---------------------------------------------------------------------------


def test_get_flavors_returns_the_built_in_default_for_a_plain_hardcoded_entry(
    tmp_path: Path,
) -> None:
    assert get_flavors(tmp_path, _BASE_ENTRY) == (_DEFAULT_FLAVOR,)


def test_get_flavors_returns_predefined(tmp_path: Path) -> None:
    entry = get_local_registry(tmp_path)["fake-model-with-flavor"]
    assert get_flavors(tmp_path, entry) == (_PREDEFINED_FLAVOR,)


def test_get_flavors_merges_custom_after_predefined(tmp_path: Path) -> None:
    entry = get_local_registry(tmp_path)["fake-model-with-flavor"]
    custom = add_flavor(tmp_path, entry.name, "Custom Flavor")
    flavors = get_flavors(tmp_path, entry)
    assert flavors == (_PREDEFINED_FLAVOR, custom)


def test_get_flavors_custom_id_colliding_with_predefined_overrides_it(tmp_path: Path) -> None:
    # add_flavor always auto-suffixes on a name collision, so provoking an
    # id collision with a predefined flavor requires writing the external
    # file directly rather than going through the public API — this is
    # exactly what update_flavor does when editing a predefined flavor (see
    # its own tests below), just exercised at the storage layer here.
    import json

    registry_file = tmp_path / "etc" / "local-llm-registry.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        json.dumps(
            {
                "flavors": {
                    "fake-model-with-flavor": [
                        {
                            "id": "predefined-1m",
                            "name": "Overridden",
                            "llama_args": {"--ctx-size": "2097152"},
                        }
                    ]
                }
            }
        )
    )
    entry = get_local_registry(tmp_path)["fake-model-with-flavor"]
    flavors = get_flavors(tmp_path, entry)
    # Same slot (single-element tuple, not appended after), but the
    # override's definition — not the predefined literal's.
    assert len(flavors) == 1
    assert flavors[0].id == "predefined-1m"
    assert flavors[0].name == "Overridden"
    assert flavors[0].llama_args == {"--ctx-size": "2097152"}


def test_get_flavors_round_trips_min_ram_and_min_vram_from_disk(tmp_path: Path) -> None:
    import json

    registry_file = tmp_path / "etc" / "local-llm-registry.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        json.dumps(
            {
                "flavors": {
                    "fake-model": [
                        {
                            "id": "mac-flavor",
                            "name": "Mac Flavor",
                            "llama_args": {},
                            "min_ram": 64,
                            "min_vram": 0,
                        }
                    ]
                }
            }
        )
    )
    entry = get_local_registry(tmp_path)["fake-model"]
    custom = next(f for f in get_flavors(tmp_path, entry) if f.id == "mac-flavor")
    assert custom.min_ram == 64
    assert custom.min_vram == 0


def test_get_flavors_empty_after_removing_a_custom_entrys_only_flavor(tmp_path: Path) -> None:
    # add_local_entry forces flavors=() on every custom kind, so a custom
    # entry starts genuinely flavor-less until something (here: the test
    # itself, standing in for _seed_default_flavor) adds one.
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="bare", kind="custom_hf", repo_id="me/bare", filename="bare.gguf"),
    )
    flavor = add_flavor(tmp_path, "bare", "Only One")
    remove_flavor(tmp_path, "bare", flavor.id)
    entry = get_local_registry(tmp_path)["bare"]
    assert get_flavors(tmp_path, entry) == ()


# ---------------------------------------------------------------------------
# add_flavor — always creates a brand-new flavor slot
# ---------------------------------------------------------------------------


def test_add_flavor_auto_generates_slug_id(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "1M Context")
    assert flavor.id == "1m-context"
    assert flavor.name == "1M Context"


def test_add_flavor_dedupes_id_when_different_names_share_a_slug(tmp_path: Path) -> None:
    first = add_flavor(tmp_path, "fake-model", "Tight VRAM")
    second = add_flavor(tmp_path, "fake-model", "tight vram")
    assert first.id == "tight-vram"
    assert second.id == "tight-vram-2"


def test_add_flavor_rejects_duplicate_name(tmp_path: Path) -> None:
    add_flavor(tmp_path, "fake-model", "Tight VRAM")
    with pytest.raises(ValueError, match="already exists"):
        add_flavor(tmp_path, "fake-model", "Tight VRAM")


def test_add_flavor_rejects_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        add_flavor(tmp_path, "does-not-exist", "Whatever")


def test_add_flavor_rejects_blank_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name is required"):
        add_flavor(tmp_path, "fake-model", "   ")


def test_add_flavor_rejects_custom_server_url_entry(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="remote", kind="custom_server_url", url="http://host:8042"),
    )
    with pytest.raises(ValueError, match="custom_server_url"):
        add_flavor(tmp_path, "remote", "Whatever")


def test_add_flavor_works_on_a_custom_hf_entry_too(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="my-custom", kind="custom_hf", repo_id="me/mine", filename="mine.gguf"),
    )
    flavor = add_flavor(tmp_path, "my-custom", "Tuned")
    assert get_flavors(tmp_path, get_local_registry(tmp_path)["my-custom"]) == (flavor,)


def test_add_flavor_defaults_min_ram_and_min_vram_to_zero(tmp_path: Path) -> None:
    # 0/0 means the hardware-fit check kodo-vsix runs before switching to a
    # flavor is inactive, i.e. "assume it works everywhere" (see
    # LlamaFlavor's docstring and doc/LLM_REGISTRY.md §4.6a).
    flavor = add_flavor(tmp_path, "fake-model", "New")
    assert flavor.min_ram == 0
    assert flavor.min_vram == 0


def test_add_flavor_can_set_min_ram_and_min_vram(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "Mac Flavor", min_ram=64, min_vram=0)
    assert flavor.min_ram == 64
    assert flavor.min_vram == 0


# ---------------------------------------------------------------------------
# update_flavor — in-place edit (custom) or override (predefined), same id
# ---------------------------------------------------------------------------


def test_update_flavor_edits_a_custom_flavor_in_place(tmp_path: Path) -> None:
    a = add_flavor(tmp_path, "fake-model", "A", llama_args={"--n-gpu-layers": "10"})
    b = add_flavor(tmp_path, "fake-model", "B")
    updated = update_flavor(
        tmp_path,
        "fake-model",
        a.id,
        "A renamed",
        description="new desc",
        llama_args={"--n-gpu-layers": "99"},
    )
    assert updated.id == a.id
    assert updated.name == "A renamed"
    assert updated.description == "new desc"
    assert updated.llama_args == {"--n-gpu-layers": "99"}
    # Position preserved — updated flavor stays where `a` was, ahead of `b`.
    entry = get_local_registry(tmp_path)["fake-model"]
    flavors = get_flavors(tmp_path, entry)
    assert [f.id for f in flavors] == [_DEFAULT_FLAVOR.id, a.id, b.id]
    assert flavors[1] == updated


def test_update_flavor_rejects_a_predefined_id(tmp_path: Path) -> None:
    # Predefined flavors are strictly read-only — no way to edit them in
    # place any more; copy into a new custom flavor via add_flavor instead.
    with pytest.raises(ValueError, match="predefined flavor"):
        update_flavor(
            tmp_path,
            "fake-model-with-flavor",
            "predefined-1m",
            "Predefined 1M (tuned)",
            llama_args={"--ctx-size": "2097152"},
        )
    entry = get_local_registry(tmp_path)["fake-model-with-flavor"]
    assert entry.flavors == (_PREDEFINED_FLAVOR,)
    assert get_flavors(tmp_path, entry) == (_PREDEFINED_FLAVOR,)


def test_update_flavor_rejects_unknown_flavor_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown flavor"):
        update_flavor(tmp_path, "fake-model", "nonexistent", "New name")


def test_update_flavor_rejects_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        update_flavor(tmp_path, "does-not-exist", "default", "New name")


def test_update_flavor_rejects_blank_name(tmp_path: Path) -> None:
    # Must use a *custom* flavor id — "default" on "fake-model" is
    # predefined, which is rejected for an entirely different reason (see
    # test_update_flavor_rejects_a_predefined_id).
    custom = add_flavor(tmp_path, "fake-model", "Custom")
    with pytest.raises(ValueError, match="name is required"):
        update_flavor(tmp_path, "fake-model", custom.id, "   ")


def test_update_flavor_rejects_custom_server_url_entry(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="remote", kind="custom_server_url", url="http://host:8042"),
    )
    with pytest.raises(ValueError, match="custom_server_url"):
        update_flavor(tmp_path, "remote", "default", "Whatever")


def test_update_flavor_rejects_duplicate_name(tmp_path: Path) -> None:
    a = add_flavor(tmp_path, "fake-model", "A")
    add_flavor(tmp_path, "fake-model", "B")
    with pytest.raises(ValueError, match="already exists"):
        update_flavor(tmp_path, "fake-model", a.id, "B")


def test_update_flavor_allows_keeping_its_own_unchanged_name(tmp_path: Path) -> None:
    a = add_flavor(tmp_path, "fake-model", "A", description="old")
    updated = update_flavor(tmp_path, "fake-model", a.id, "A", description="new")
    assert updated.description == "new"


def test_update_flavor_can_set_min_ram_and_min_vram(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "GPU Flavor", min_ram=16, min_vram=8)
    updated = update_flavor(
        tmp_path,
        "fake-model",
        flavor.id,
        "GPU Flavor (v2)",
        llama_args={"--n-gpu-layers": "10"},
        min_ram=24,
        min_vram=12,
    )
    assert updated.min_ram == 24
    assert updated.min_vram == 12


def test_update_flavor_resets_min_ram_and_min_vram_to_zero_if_not_resent(tmp_path: Path) -> None:
    # Unlike the old carry-forward-from-original behavior, an update that
    # omits min_ram/min_vram resets them to 0 rather than preserving the
    # previous value — the "Manage flavors" modal always resends its own
    # min-RAM/min-VRAM fields' current contents, so this only matters for a
    # direct (non-UI) caller.
    flavor = add_flavor(tmp_path, "fake-model", "GPU Flavor", min_ram=16, min_vram=8)
    updated = update_flavor(tmp_path, "fake-model", flavor.id, "GPU Flavor (v2)")
    assert updated.min_ram == 0
    assert updated.min_vram == 0


# ---------------------------------------------------------------------------
# add_local_entry — custom entries never carry a predefined flavor
# ---------------------------------------------------------------------------


def test_add_local_entry_forces_flavors_empty_even_if_caller_passed_some(tmp_path: Path) -> None:
    # A caller-supplied non-empty `flavors=` (e.g. a copy-paste from a
    # hardcoded entry literal) must not survive — it would otherwise shadow
    # any later custom flavor sharing its id. See add_local_entry's docstring.
    add_local_entry(
        tmp_path,
        LocalLLMEntry(
            name="sneaky",
            kind="custom_hf",
            repo_id="me/sneaky",
            filename="sneaky.gguf",
            flavors=(_PREDEFINED_FLAVOR,),
        ),
    )
    entry = get_local_registry(tmp_path)["sneaky"]
    assert entry.flavors == ()
    assert get_flavors(tmp_path, entry) == ()


# ---------------------------------------------------------------------------
# remove_flavor
# ---------------------------------------------------------------------------


def test_remove_flavor_rejects_predefined(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="predefined flavor"):
        remove_flavor(tmp_path, "fake-model-with-flavor", "predefined-1m")


def test_remove_flavor_rejects_predefined_even_when_overridden(tmp_path: Path) -> None:
    # A same-id override can now only exist as legacy data from before
    # predefined flavors became strictly read-only (update_flavor can no
    # longer create one — get_flavors still merges a pre-existing one in for
    # resilience, see its docstring). It must not be a backdoor for
    # "removing" a predefined flavor — that would silently revert it to the
    # hardcoded definition instead of actually removing anything, which
    # isn't what "Remove" (disabled in the UI for these ids) should ever do.
    import json

    registry_file = tmp_path / "etc" / "local-llm-registry.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        json.dumps(
            {
                "flavors": {
                    "fake-model-with-flavor": [
                        {"id": "predefined-1m", "name": "Overridden", "llama_args": {}}
                    ]
                }
            }
        )
    )
    with pytest.raises(ValueError, match="predefined flavor"):
        remove_flavor(tmp_path, "fake-model-with-flavor", "predefined-1m")


def test_remove_flavor_rejects_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No custom flavor"):
        remove_flavor(tmp_path, "fake-model", "nonexistent")


def test_remove_flavor_removes_custom_and_leaves_others(tmp_path: Path) -> None:
    a = add_flavor(tmp_path, "fake-model", "A")
    b = add_flavor(tmp_path, "fake-model", "B")
    remove_flavor(tmp_path, "fake-model", a.id)
    entry = get_local_registry(tmp_path)["fake-model"]
    assert get_flavors(tmp_path, entry) == (_DEFAULT_FLAVOR, b)


def test_remove_flavor_resets_active_selection_to_default(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "A")
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    assert get_active_flavor(tmp_path, "fake-model") == flavor.id

    remove_flavor(tmp_path, "fake-model", flavor.id)
    assert get_active_flavor(tmp_path, "fake-model") == ""


def test_remove_flavor_leaves_active_selection_alone_if_a_different_flavor_was_removed(
    tmp_path: Path,
) -> None:
    a = add_flavor(tmp_path, "fake-model", "A")
    b = add_flavor(tmp_path, "fake-model", "B")
    set_active_flavor(tmp_path, "fake-model", a.id)
    remove_flavor(tmp_path, "fake-model", b.id)
    assert get_active_flavor(tmp_path, "fake-model") == a.id


# ---------------------------------------------------------------------------
# set_active_flavor / get_active_flavor
# ---------------------------------------------------------------------------


def test_active_flavor_defaults_to_empty(tmp_path: Path) -> None:
    assert get_active_flavor(tmp_path, "fake-model") == ""


def test_set_active_flavor_rejects_unknown_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown local model"):
        set_active_flavor(tmp_path, "does-not-exist", "")


def test_set_active_flavor_rejects_unknown_flavor_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown flavor"):
        set_active_flavor(tmp_path, "fake-model", "nonexistent")


def test_set_active_flavor_accepts_empty_string_for_default(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "A")
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    set_active_flavor(tmp_path, "fake-model", "")
    assert get_active_flavor(tmp_path, "fake-model") == ""


def test_set_active_flavor_accepts_a_predefined_flavor(tmp_path: Path) -> None:
    set_active_flavor(tmp_path, "fake-model-with-flavor", "predefined-1m")
    assert get_active_flavor(tmp_path, "fake-model-with-flavor") == "predefined-1m"


# ---------------------------------------------------------------------------
# get_effective_flavor_id
# ---------------------------------------------------------------------------


def test_get_effective_flavor_id_falls_back_to_first_available_when_unset(tmp_path: Path) -> None:
    assert get_effective_flavor_id(tmp_path, _BASE_ENTRY) == _DEFAULT_FLAVOR.id


def test_get_effective_flavor_id_returns_the_explicit_active_one(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "A")
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    entry = get_local_registry(tmp_path)["fake-model"]
    assert get_effective_flavor_id(tmp_path, entry) == flavor.id


def test_get_effective_flavor_id_falls_back_when_active_selection_is_stale(tmp_path: Path) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "Gone Soon")
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    (tmp_path / "etc" / "local-llm-registry.json").unlink()
    assert get_effective_flavor_id(tmp_path, _BASE_ENTRY) == _DEFAULT_FLAVOR.id


def test_get_effective_flavor_id_empty_when_entry_has_no_flavors(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="bare", kind="custom_hf", repo_id="me/bare", filename="bare.gguf"),
    )
    entry = get_local_registry(tmp_path)["bare"]
    assert get_effective_flavor_id(tmp_path, entry) == ""


# ---------------------------------------------------------------------------
# resolve_context_window — deduced from the flavor's own -c/--ctx-size
# ---------------------------------------------------------------------------


def test_resolve_context_window_reads_ctx_size_flag(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"--ctx-size": "1048576"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == 1_048_576


def test_resolve_context_window_reads_short_c_flag(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"-c": "65536"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == 65_536


def test_resolve_context_window_prefers_ctx_size_over_short_c(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"--ctx-size": "1048576", "-c": "65536"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == 1_048_576


def test_resolve_context_window_falls_back_when_ctx_size_is_zero(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"--ctx-size": "0"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == _BASE_ENTRY.context_window


def test_resolve_context_window_falls_back_when_absent(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"--n-gpu-layers": "20"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == _BASE_ENTRY.context_window


def test_resolve_context_window_falls_back_when_unparseable(tmp_path: Path) -> None:
    flavor = LlamaFlavor(id="f", name="f", llama_args={"--ctx-size": "not-a-number"})
    assert resolve_context_window(_BASE_ENTRY, flavor) == _BASE_ENTRY.context_window


def test_resolve_context_window_falls_back_when_flavor_is_none(tmp_path: Path) -> None:
    assert resolve_context_window(_BASE_ENTRY, None) == _BASE_ENTRY.context_window


# ---------------------------------------------------------------------------
# resolve_effective_llama_config
# ---------------------------------------------------------------------------


def test_resolve_effective_config_with_no_active_flavor_uses_the_built_in_default(
    tmp_path: Path,
) -> None:
    args, ctx = resolve_effective_llama_config(tmp_path, _BASE_ENTRY)
    assert args == _DEFAULT_FLAVOR.llama_args
    assert ctx == _BASE_ENTRY.context_window


def test_resolve_effective_config_full_replace_not_merge(tmp_path: Path) -> None:
    # The flavor's llama_args has none of the built-in default's KV-cache
    # flags — full replace means they must NOT survive into the effective
    # config.
    flavor = add_flavor(
        tmp_path,
        "fake-model",
        "1M Context",
        llama_args={"--ctx-size": "1048576", "--rope-scaling": "yarn"},
    )
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    args, ctx = resolve_effective_llama_config(tmp_path, _BASE_ENTRY)
    assert args == {"--ctx-size": "1048576", "--rope-scaling": "yarn"}
    assert ctx == 1_048_576


def test_resolve_effective_config_context_window_zero_inherits_entrys_own(
    tmp_path: Path,
) -> None:
    flavor = add_flavor(
        tmp_path, "fake-model", "VRAM Tight", llama_args={"--n-gpu-layers": "20"}
    )  # no --ctx-size/-c at all
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    args, ctx = resolve_effective_llama_config(tmp_path, _BASE_ENTRY)
    assert args == {"--n-gpu-layers": "20"}
    assert ctx == _BASE_ENTRY.context_window


def test_resolve_effective_config_falls_back_to_the_first_available_flavor_if_active_was_deleted(
    tmp_path: Path,
) -> None:
    flavor = add_flavor(tmp_path, "fake-model", "Gone Soon")
    set_active_flavor(tmp_path, "fake-model", flavor.id)
    # Deleting the external registry file entirely simulates the active
    # flavor id becoming stale without going through remove_flavor (which
    # itself resets the selection) — resolve must not blow up either way,
    # and falls back to the entry's own built-in default flavor (the only
    # one left once the custom-flavor store is gone).
    (tmp_path / "etc" / "local-llm-registry.json").unlink()
    args, ctx = resolve_effective_llama_config(tmp_path, _BASE_ENTRY)
    assert args == _DEFAULT_FLAVOR.llama_args
    assert ctx == _BASE_ENTRY.context_window


def test_resolve_effective_config_with_zero_flavors_returns_empty_args(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="bare", kind="custom_hf", repo_id="me/bare", filename="bare.gguf"),
    )
    entry = get_local_registry(tmp_path)["bare"]
    assert get_flavors(tmp_path, entry) == ()
    args, ctx = resolve_effective_llama_config(tmp_path, entry)
    assert args == {}
    assert ctx == entry.context_window


# ---------------------------------------------------------------------------
# remove_local_entry cleans up a removed custom entry's own flavor data
# ---------------------------------------------------------------------------


def test_remove_local_entry_cleans_up_its_flavors_and_active_selection(tmp_path: Path) -> None:
    add_local_entry(
        tmp_path,
        LocalLLMEntry(name="temp", kind="custom_hf", repo_id="me/temp", filename="temp.gguf"),
    )
    flavor = add_flavor(tmp_path, "temp", "A")
    set_active_flavor(tmp_path, "temp", flavor.id)

    remove_local_entry(tmp_path, "temp")

    import json

    data = json.loads((tmp_path / "etc" / "local-llm-registry.json").read_text())
    assert "temp" not in data.get("flavors", {})
    assert "temp" not in data.get("active_flavors", {})


# ---------------------------------------------------------------------------
# LlamaServer.__build_command — server-management flags plus whatever
# llama_args were resolved from the active flavor, verbatim (no merging with
# any other default — see LlamaServerConfig, which carries none of its own).
# ---------------------------------------------------------------------------


def _build_command(**llama_args: str) -> list[str]:
    cfg = LlamaServerConfig(
        executable=Path("/fake/llama-server"),
        model_path=Path("/fake/model.gguf"),
        kodo_dir=Path("/fake/kodo"),
    )
    server = LlamaServer(cfg, llama_args)
    return server._LlamaServer__build_command()  # type: ignore[attr-defined]


def test_build_command_with_no_llama_args_carries_only_server_flags() -> None:
    cmd = _build_command()
    assert "--model" in cmd
    assert "--host" in cmd
    assert "--port" in cmd
    assert "--ctx-size" not in cmd
    assert "--jinja" not in cmd


def test_build_command_includes_every_resolved_llama_arg_verbatim() -> None:
    cmd = _build_command(**{"--ctx-size": "1048576", "--n-gpu-layers": "20"})
    assert cmd.count("--ctx-size") == 1
    assert cmd[cmd.index("--ctx-size") + 1] == "1048576"
    assert cmd.count("--n-gpu-layers") == 1
    assert cmd[cmd.index("--n-gpu-layers") + 1] == "20"


def test_build_command_bare_flag_has_no_trailing_empty_value() -> None:
    cmd = _build_command(**{"--flash-attn": ""})
    # A bare flag with an empty value must not push a stray "" element after
    # it — it's the last (and only) resolved arg, so it must be cmd's last
    # element outright.
    assert cmd[-1] == "--flash-attn"


def test_build_command_default_flavor_args_include_jinja() -> None:
    cmd = _build_command(**_DEFAULT_FLAVOR.llama_args)
    assert "--jinja" in cmd
