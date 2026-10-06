"""Behavioral tests for :func:`kodo.llms.local_registry.make_yarn_context_knob`.

The per-family "Context window" knob offers the model's native context (no
args) plus YaRN-extended sizes, each owning the rope-scaling flag set.
"""

from __future__ import annotations

import pytest

from kodo.llms.local_registry import KnobKind, make_yarn_context_knob, validate_knobs


def test_the_knob_offers_native_first_then_each_extended_size() -> None:
    knob = make_yarn_context_knob(
        knob_id="context-test",
        arch_key="testarch",
        native_context=262_144,
        sizes=(524_288, 1_048_576),
    )

    assert knob.id == "context-test"
    assert knob.kind == KnobKind.DROPDOWN
    assert knob.default_option == "native"
    assert [option.id for option in knob.options] == ["native", "512k", "1m"]
    assert knob.options[0].name == "Native (256K)"
    assert knob.options[0].llama_args == {}
    assert knob.options[2].name == "1M (YaRN-extended)"
    validate_knobs((knob,), context="test")


def test_an_extended_option_sets_the_full_yarn_flag_set() -> None:
    knob = make_yarn_context_knob(
        knob_id="context-test", arch_key="testarch", native_context=32_768, sizes=(131_072,)
    )

    option = knob.option("128k")

    assert option is not None
    assert option.llama_args == {
        "--ctx-size": "131072",
        "--rope-scaling": "yarn",
        "--rope-scale": "4.0",
        "--yarn-orig-ctx": "32768",
        "--override-kv": "testarch.context_length=int:131072",
    }


def test_sizes_that_are_not_whole_k_or_m_are_spelled_verbatim() -> None:
    knob = make_yarn_context_knob(
        knob_id="context-test", arch_key="testarch", native_context=1000, sizes=(1500,)
    )

    assert [option.id for option in knob.options] == ["native", "1500"]
    assert knob.options[0].name == "Native (1000)"
    option = knob.option("1500")
    assert option is not None
    assert option.llama_args["--rope-scale"] == "1.5"


@pytest.mark.parametrize(
    ("native_context", "sizes", "message"),
    [
        (0, (1024,), "native_context must be positive"),
        (-1, (1024,), "native_context must be positive"),
        (1024, (), "at least one extended size"),
        (2048, (2048,), "not larger than the native"),
        (2048, (4096, 1024), "not larger than the native"),
    ],
)
def test_a_malformed_declaration_is_rejected(
    native_context: int, sizes: tuple[int, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message) as excinfo:
        make_yarn_context_knob(
            knob_id="context-bad", arch_key="testarch", native_context=native_context, sizes=sizes
        )
    assert "context-bad" in str(excinfo.value)
