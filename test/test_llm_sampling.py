"""Behavioral tests for request-level sampling parameters (doc/SAMPLING.md).

Covers the two things the feature rests on:

1. ``SamplingParams`` is **sparse** — an unset parameter is omitted from the
   request body rather than sent as a default, because llama-server treats an
   omitted field as "use whatever the CLI launched me with" (doc/SAMPLING.md
   §1). Everything else in this file follows from that. This is the shape a
   session's own per-quant overrides take (``SessionState.sampling``) — a
   flavor's sampling knobs are not a separate ``SamplingParams`` instance any
   more; they live directly in ``LlamaFlavor.llama_args`` (see
   ``test_llm_flavors.py`` for flavor persistence).
2. Untrusted JSON never crashes a session: unknown/reserved/wrong-typed values
   are dropped and out-of-range numbers clamped.

Spec-driven throughout (see the ``feedback_spec_driven_tests`` convention):
assertions about which parameters are curated/advanced/reserved read the live
``SAMPLING_PARAM_SPECS`` table rather than hardcoding a second copy of it.
"""

from __future__ import annotations

from kodo.llms._sampling import (
    RESERVED_SAMPLING_FIELDS,
    SAMPLER_NAMES,
    SAMPLING_PARAM_SPECS,
    SamplingParams,
    sampling_param_spec,
    sampling_specs_to_json,
)

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
