"""Behavioral tests for request-level sampling parameters (doc/SAMPLING.md).

Covers the three things the feature rests on:

1. ``SamplingParams`` is **sparse** — an unset parameter is omitted from the
   request body rather than sent as a default, because llama-server treats an
   omitted field as "use whatever the CLI launched me with" (doc/SAMPLING.md
   §1). Everything else in this file follows from that.
2. Untrusted JSON never crashes a session: unknown/reserved/wrong-typed values
   are dropped and out-of-range numbers clamped.
3. A flavor's defaults survive a persistence round-trip and are overlaid
   per-parameter by a session's overrides.

Spec-driven throughout (see the ``feedback_spec_driven_tests`` convention):
assertions about which parameters are curated/advanced/reserved read the live
``SAMPLING_PARAM_SPECS`` table rather than hardcoding a second copy of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import _local_registry
from kodo.llms._local_registry import (
    LlamaFlavor,
    LocalLLMEntry,
    add_flavor,
    get_flavors,
    resolve_flavor_sampling,
    set_active_flavor,
    update_flavor,
)
from kodo.llms._sampling import (
    RESERVED_SAMPLING_FIELDS,
    SAMPLER_NAMES,
    SAMPLING_PARAM_SPECS,
    SamplingParams,
    cli_flag_conflicts,
    sampling_param_spec,
    sampling_specs_to_json,
)

_ENTRY = LocalLLMEntry(
    name="fake-model",
    kind="hardcoded_hf",
    repo_id="acme/fake-model",
    filename="fake-model.gguf",
    context_window=262_144,
)


@pytest.fixture(autouse=True)
def _fake_hardcoded_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the real (large, evolving) hardcoded model list."""
    monkeypatch.setattr(_local_registry, "_HARDCODED_LOCAL_MODELS", (_ENTRY,))


# ---------------------------------------------------------------------------
# The spec table — the single source of truth every other layer reads
# ---------------------------------------------------------------------------


def test_spec_names_are_unique() -> None:
    names = [s.name for s in SAMPLING_PARAM_SPECS]
    assert len(names) == len(set(names))


def test_no_spec_names_a_reserved_field() -> None:
    """A parameter can't be both tunable and reserved — that would let the UI
    offer a knob `from_json` then silently drops."""
    for spec in SAMPLING_PARAM_SPECS:
        assert spec.name not in RESERVED_SAMPLING_FIELDS


def test_spec_bounds_are_ordered() -> None:
    for spec in SAMPLING_PARAM_SPECS:
        if spec.minimum is not None and spec.maximum is not None:
            assert spec.minimum <= spec.maximum, spec.name


def test_curated_and_advanced_sets_are_both_populated() -> None:
    """The modal's two-tier layout only makes sense if both tiers exist."""
    assert any(not s.advanced for s in SAMPLING_PARAM_SPECS)
    assert any(s.advanced for s in SAMPLING_PARAM_SPECS)


def test_specs_to_json_round_trips_every_spec() -> None:
    payload = sampling_specs_to_json()
    assert len(payload) == len(SAMPLING_PARAM_SPECS)
    assert [p["name"] for p in payload] == [s.name for s in SAMPLING_PARAM_SPECS]


def test_sampling_param_spec_lookup() -> None:
    first = SAMPLING_PARAM_SPECS[0]
    assert sampling_param_spec(first.name) is first
    assert sampling_param_spec("no_such_parameter") is None
    assert sampling_param_spec("max_tokens") is None


# ---------------------------------------------------------------------------
# from_json — untrusted input must degrade, never raise
# ---------------------------------------------------------------------------


def test_empty_by_default() -> None:
    assert SamplingParams().is_empty
    assert SamplingParams().to_request_body() == {}


def test_unset_parameters_are_omitted_not_defaulted() -> None:
    """The central invariant: only what was set goes on the wire."""
    params = SamplingParams.from_json({"temperature": 0.2})
    assert params.to_request_body() == {"temperature": 0.2}


def test_from_json_drops_unknown_fields() -> None:
    params = SamplingParams.from_json({"temperature": 0.2, "not_a_sampler": 1})
    assert params.to_request_body() == {"temperature": 0.2}


def test_from_json_drops_every_reserved_field() -> None:
    raw: dict[str, object] = dict.fromkeys(RESERVED_SAMPLING_FIELDS, 1)
    raw["temperature"] = 0.2
    assert SamplingParams.from_json(raw).to_request_body() == {"temperature": 0.2}


def test_from_json_accepts_numeric_strings() -> None:
    """Webview number inputs hand back strings, not numbers."""
    params = SamplingParams.from_json({"temperature": "0.4", "top_k": "40"})
    assert params.to_request_body() == {"temperature": 0.4, "top_k": 40}


def test_from_json_drops_non_numeric_values() -> None:
    assert SamplingParams.from_json({"temperature": "warm"}).is_empty
    assert SamplingParams.from_json({"temperature": True}).is_empty
    assert SamplingParams.from_json({"temperature": None}).is_empty


def test_from_json_clamps_out_of_range_numbers() -> None:
    spec = sampling_param_spec("mirostat")
    assert spec is not None and spec.maximum is not None
    params = SamplingParams.from_json({"mirostat": spec.maximum + 5})
    assert params.to_request_body() == {"mirostat": int(spec.maximum)}


def test_from_json_ignores_non_dict_input() -> None:
    assert SamplingParams.from_json(None).is_empty
    assert SamplingParams.from_json([1, 2]).is_empty
    assert SamplingParams.from_json("temperature=1").is_empty


def test_int_kind_truncates_a_float() -> None:
    assert SamplingParams.from_json({"top_k": 40.9}).to_request_body() == {"top_k": 40}


def test_samplers_list_drops_unknown_stage_names() -> None:
    """One bad name makes llama-server reject the whole request, so unknown
    stages are dropped rather than forwarded."""
    known = sorted(SAMPLER_NAMES)[0]
    params = SamplingParams.from_json({"samplers": [known, "not_a_stage"]})
    assert params.to_request_body() == {"samplers": [known]}


def test_samplers_list_of_only_unknown_names_is_dropped_entirely() -> None:
    assert SamplingParams.from_json({"samplers": ["not_a_stage"]}).is_empty


def test_str_list_rejects_a_non_list() -> None:
    assert SamplingParams.from_json({"dry_sequence_breakers": "\n"}).is_empty


def test_dry_sequence_breakers_pass_through_verbatim() -> None:
    breakers = ["\n", ";", "}"]
    params = SamplingParams.from_json({"dry_sequence_breakers": breakers})
    assert params.to_request_body() == {"dry_sequence_breakers": breakers}


def test_to_json_is_a_copy() -> None:
    params = SamplingParams.from_json({"temperature": 0.2})
    params.to_json()["temperature"] = 99
    assert params.to_request_body() == {"temperature": 0.2}


# ---------------------------------------------------------------------------
# merged_with — flavor defaults overlaid by session overrides
# ---------------------------------------------------------------------------


def test_merge_is_per_parameter_not_wholesale() -> None:
    defaults = SamplingParams.from_json({"temperature": 0.8, "top_p": 0.95})
    overrides = SamplingParams.from_json({"temperature": 0.1})
    assert defaults.merged_with(overrides).to_request_body() == {
        "temperature": 0.1,
        "top_p": 0.95,
    }


def test_merge_with_empty_override_keeps_defaults() -> None:
    defaults = SamplingParams.from_json({"temperature": 0.8})
    assert defaults.merged_with(SamplingParams()).to_request_body() == {"temperature": 0.8}


def test_merge_does_not_mutate_either_side() -> None:
    defaults = SamplingParams.from_json({"temperature": 0.8})
    overrides = SamplingParams.from_json({"top_k": 40})
    defaults.merged_with(overrides)
    assert defaults.to_request_body() == {"temperature": 0.8}
    assert overrides.to_request_body() == {"top_k": 40}


# ---------------------------------------------------------------------------
# cli_flag_conflicts — the flavor editor's warning
# ---------------------------------------------------------------------------


def test_conflict_detected_for_a_knob_set_in_both_layers() -> None:
    spec = next(s for s in SAMPLING_PARAM_SPECS if s.cli_flags)
    conflicts = cli_flag_conflicts(
        {spec.cli_flags[0]: "1"}, SamplingParams.from_json({spec.name: 1})
    )
    assert conflicts == {spec.cli_flags[0]: spec.name}


def test_no_conflict_when_only_the_cli_side_is_set() -> None:
    spec = next(s for s in SAMPLING_PARAM_SPECS if s.cli_flags)
    assert cli_flag_conflicts({spec.cli_flags[0]: "1"}, SamplingParams()) == {}


def test_no_conflict_for_a_non_sampling_flag() -> None:
    assert cli_flag_conflicts(
        {"--ctx-size": "0"}, SamplingParams.from_json({"temperature": 0.2})
    ) == {}


def test_every_cli_alias_of_a_knob_is_recognised() -> None:
    """A flavor may spell temperature `--temp` or `--temperature`; both count."""
    spec = next(s for s in SAMPLING_PARAM_SPECS if len(s.cli_flags) > 1)
    sampling = SamplingParams.from_json({spec.name: 1})
    for flag in spec.cli_flags:
        assert cli_flag_conflicts({flag: "1"}, sampling) == {flag: spec.name}


# ---------------------------------------------------------------------------
# Flavor persistence + resolution
# ---------------------------------------------------------------------------


def test_flavor_defaults_to_no_sampling() -> None:
    """Every built-in flavor sends nothing, so an untouched install is inert."""
    assert LlamaFlavor(id="x", name="x").sampling.is_empty
    assert LlamaFlavor.make_default_kv_q8().sampling.is_empty


def test_add_flavor_persists_sampling(tmp_path: Path) -> None:
    add_flavor(
        tmp_path,
        "fake-model",
        "Precise",
        sampling=SamplingParams.from_json({"temperature": 0.1, "top_k": 0}),
    )
    flavor = next(f for f in get_flavors(tmp_path, _ENTRY) if f.name == "Precise")
    assert flavor.sampling.to_request_body() == {"temperature": 0.1, "top_k": 0}


def test_add_flavor_without_sampling_is_empty(tmp_path: Path) -> None:
    add_flavor(tmp_path, "fake-model", "Plain")
    flavor = next(f for f in get_flavors(tmp_path, _ENTRY) if f.name == "Plain")
    assert flavor.sampling.is_empty


def test_update_flavor_replaces_sampling_wholesale(tmp_path: Path) -> None:
    """Not carried forward — the editor must resend the full set, same caveat
    as min_ram/min_vram/platform."""
    created = add_flavor(
        tmp_path, "fake-model", "Precise", sampling=SamplingParams.from_json({"temperature": 0.1})
    )
    update_flavor(tmp_path, "fake-model", created.id, "Precise")
    flavor = next(f for f in get_flavors(tmp_path, _ENTRY) if f.id == created.id)
    assert flavor.sampling.is_empty


def test_flavor_persisted_before_sampling_existed_loads_empty(tmp_path: Path) -> None:
    """A `sampling`-less JSON blob must load as "send nothing", not crash."""
    legacy = _local_registry._flavor_from_json(
        {"id": "legacy", "name": "Legacy", "llama_args": {"--temp": "0.6"}}
    )
    assert legacy is not None
    assert legacy.sampling.is_empty


def test_resolve_flavor_sampling_follows_the_active_flavor(tmp_path: Path) -> None:
    cold = add_flavor(
        tmp_path, "fake-model", "Cold", sampling=SamplingParams.from_json({"temperature": 0.0})
    )
    hot = add_flavor(
        tmp_path, "fake-model", "Hot", sampling=SamplingParams.from_json({"temperature": 1.2})
    )

    set_active_flavor(tmp_path, "fake-model", cold.id)
    assert resolve_flavor_sampling(tmp_path, _ENTRY).to_request_body() == {"temperature": 0.0}

    set_active_flavor(tmp_path, "fake-model", hot.id)
    assert resolve_flavor_sampling(tmp_path, _ENTRY).to_request_body() == {"temperature": 1.2}


def test_resolve_flavor_sampling_empty_for_builtin_default(tmp_path: Path) -> None:
    assert resolve_flavor_sampling(tmp_path, _ENTRY).is_empty
