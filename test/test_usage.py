"""Behavior tests for kodo.llms.anthropic._usage (cost computation)."""

from kodo.llms._interface import Usage


def test_zero_tokens_costs_nothing() -> None:
    usage = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    assert usage.usd_cost == 0.0


def test_one_million_input_tokens_costs_three_dollars() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    assert abs(usage.usd_cost - 3.0) < 1e-6


def test_one_million_output_tokens_costs_fifteen_dollars() -> None:
    usage = Usage(
        input_tokens=0,
        output_tokens=1_000_000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    assert abs(usage.usd_cost - 15.0) < 1e-6


def test_cache_read_is_cheaper_than_input() -> None:
    base = Usage(
        input_tokens=1000,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    cached = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=1000,
        model="claude-sonnet-4-6",
    )
    assert cached.usd_cost < base.usd_cost


def test_haiku_is_cheaper_than_sonnet() -> None:
    sonnet = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    haiku = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-haiku-4-5-20251001",
    )
    assert haiku.usd_cost < sonnet.usd_cost


def test_opus_is_more_expensive_than_sonnet() -> None:
    sonnet = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    opus = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-opus-4-7",
    )
    assert opus.usd_cost > sonnet.usd_cost


def test_unknown_model_falls_back_to_sonnet_pricing() -> None:
    known = Usage(
        input_tokens=500,
        output_tokens=500,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-sonnet-4-6",
    )
    unknown = Usage(
        input_tokens=500,
        output_tokens=500,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="claude-future-99-99",
    )
    assert abs(known.usd_cost - unknown.usd_cost) < 1e-9
