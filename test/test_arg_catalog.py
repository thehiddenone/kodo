"""Behavioral tests for the curated llama-server argument catalog's wire payload.

:func:`kodo.llms.llama_arg_catalog_to_json` is what kodo-vsix's profile editor
renders its "Add argument" picker from (doc/WS_PROTOCOL.md §5.12b).
"""

from __future__ import annotations

import json

from kodo.llms import LLAMA_ARG_CATALOG, RESERVED_LLAMA_ARGS, llama_arg_catalog_to_json

_WIRE_KEYS = frozenset(
    {
        "flag",
        "label",
        "kind",
        "category",
        "help",
        "advanced",
        "minimum",
        "maximum",
        "step",
        "choices",
        "placeholder",
        "default",
        "sensible_minimum",
        "sensible_maximum",
        "valid_values",
    }
)


def test_the_payload_lists_every_catalog_flag_in_order() -> None:
    payload = llama_arg_catalog_to_json()

    assert [item["flag"] for item in payload] == [spec.flag for spec in LLAMA_ARG_CATALOG]


def test_every_item_carries_the_full_wire_shape() -> None:
    for item in llama_arg_catalog_to_json():
        assert set(item) == _WIRE_KEYS, item["flag"]


def test_the_payload_is_plain_json() -> None:
    payload = llama_arg_catalog_to_json()

    assert json.loads(json.dumps(payload)) == payload


def test_items_mirror_their_specs() -> None:
    for spec, item in zip(LLAMA_ARG_CATALOG, llama_arg_catalog_to_json(), strict=True):
        assert item["label"] == spec.label
        assert item["kind"] == spec.kind
        assert item["category"] == spec.category
        assert item["choices"] == list(spec.choices)
        expected_values = None if spec.valid_values is None else list(spec.valid_values)
        assert item["valid_values"] == expected_values


def test_enum_flags_offer_choices_and_no_other_flag_does() -> None:
    payload = llama_arg_catalog_to_json()

    assert any(item["kind"] == "enum" for item in payload)
    for item in payload:
        assert bool(item["choices"]) == (item["kind"] == "enum"), item["flag"]


def test_no_reserved_flag_is_offered() -> None:
    offered = {item["flag"] for item in llama_arg_catalog_to_json()}

    assert offered.isdisjoint(RESERVED_LLAMA_ARGS)
