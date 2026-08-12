"""Behavior tests for Usage.usd_cost -- kodo.llms._pricing's vendor dispatch

plus each vendor's own pricing table (kodo.llms.anthropic._usage,
kodo.llms.openai._usage).
"""

from kodo.llms import Usage


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


# ---------------------------------------------------------------------------
# OpenAI vendor dispatch (kodo.llms._pricing routes by model-id prefix)
# ---------------------------------------------------------------------------


def test_openai_zero_tokens_costs_nothing() -> None:
    usage = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-terra",
    )
    assert usage.usd_cost == 0.0


def test_openai_sol_more_expensive_than_terra() -> None:
    terra = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-terra",
    )
    sol = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-sol",
    )
    assert sol.usd_cost > terra.usd_cost


def test_openai_terra_more_expensive_than_luna() -> None:
    luna = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-luna",
    )
    terra = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-terra",
    )
    assert terra.usd_cost > luna.usd_cost


def test_openai_cache_read_is_cheaper_than_input() -> None:
    base = Usage(
        input_tokens=1000,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="gpt-5.6-sol",
    )
    cached = Usage(
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=1000,
        model="gpt-5.6-sol",
    )
    assert 0.0 < cached.usd_cost < base.usd_cost


def test_local_model_costs_nothing() -> None:
    """A model id matching no cloud vendor's prefix (e.g. a local llama.cpp name) costs $0."""
    usage = Usage(
        input_tokens=1000,
        output_tokens=1000,
        cache_write_tokens=0,
        cache_read_tokens=0,
        model="llamacpp-qwen36-27b-q4-k-xl",
    )
    assert usage.usd_cost == 0.0
