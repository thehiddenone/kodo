"""Tests for ``kodo.llms._openrouter_catalog`` -- the fetched/cached
OpenRouter model catalog (doc/LLM_REGISTRY.md §3a).

Covers the pure parsing helpers, the on-disk cache round trip
(``get_openrouter_catalog``/``get_openrouter_model``), and
``refresh_openrouter_catalog``'s fetch-then-cache behavior against a faked
``aiohttp.ClientSession`` -- no real network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.llms import _openrouter_catalog as catalog_module
from kodo.llms._openrouter_catalog import (
    OpenRouterModelInfo,
    _model_from_api_json,
    get_openrouter_catalog,
    get_openrouter_model,
    refresh_openrouter_catalog,
)

# ---------------------------------------------------------------------------
# _model_from_api_json -- pure
# ---------------------------------------------------------------------------


def test_model_from_api_json_parses_full_entry() -> None:
    model = _model_from_api_json(
        {
            "id": "anthropic/claude-sonnet-4",
            "name": "Anthropic: Claude Sonnet 4",
            "context_length": 1000000,
            "pricing": {
                "prompt": "0.000003",
                "completion": "0.000015",
                "input_cache_read": "0.0000003",
                "input_cache_write": "0.00000375",
            },
            "supported_parameters": ["temperature", "reasoning", "tools"],
        }
    )
    assert model is not None
    assert model.id == "anthropic/claude-sonnet-4"
    assert model.name == "Anthropic: Claude Sonnet 4"
    assert model.context_length == 1000000
    assert model.price_prompt == pytest.approx(0.000003)
    assert model.price_completion == pytest.approx(0.000015)
    assert model.price_cache_read == pytest.approx(0.0000003)
    assert model.price_cache_write == pytest.approx(0.00000375)
    assert model.supports_reasoning is True


def test_model_from_api_json_missing_id_returns_none() -> None:
    assert _model_from_api_json({"name": "No id"}) is None


def test_model_from_api_json_missing_pricing_defaults_to_zero() -> None:
    model = _model_from_api_json({"id": "some/model", "name": "Some Model"})
    assert model is not None
    assert model.price_prompt == 0.0
    assert model.price_completion == 0.0


def test_model_from_api_json_negative_sentinel_pricing_preserved() -> None:
    """openrouter/auto's own entry reports -1 -- must round-trip as -1.0, not 0."""
    model = _model_from_api_json(
        {
            "id": "openrouter/auto",
            "name": "Auto Router",
            "pricing": {"prompt": "-1", "completion": "-1"},
        }
    )
    assert model is not None
    assert model.price_prompt == -1.0
    assert model.price_completion == -1.0


def test_model_from_api_json_no_reasoning_support() -> None:
    model = _model_from_api_json(
        {"id": "some/model", "name": "x", "supported_parameters": ["temperature"]}
    )
    assert model is not None
    assert model.supports_reasoning is False


def test_model_from_api_json_missing_name_falls_back_to_id() -> None:
    model = _model_from_api_json({"id": "some/model"})
    assert model is not None
    assert model.name == "some/model"


def test_model_from_api_json_non_numeric_pricing_string_defaults_to_zero() -> None:
    model = _model_from_api_json({"id": "x/y", "pricing": {"prompt": "not-a-number"}})
    assert model is not None
    assert model.price_prompt == 0.0


# ---------------------------------------------------------------------------
# get_openrouter_catalog / get_openrouter_model -- cache round trip
# ---------------------------------------------------------------------------


def test_get_openrouter_catalog_empty_when_never_fetched(tmp_path: Path) -> None:
    assert get_openrouter_catalog(tmp_path) == []


def _write_cache(kodo_dir: Path, models: list[dict[str, object]]) -> None:
    cache_path = kodo_dir / "etc" / "openrouter-models.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"fetched_at": 0.0, "models": models}), encoding="utf-8")


def test_get_openrouter_catalog_reads_cached_models(tmp_path: Path) -> None:
    _write_cache(
        tmp_path,
        [
            {
                "id": "openrouter/auto",
                "name": "Auto Router",
                "context_length": 0,
                "price_prompt": -1.0,
                "price_completion": -1.0,
                "price_cache_read": 0.0,
                "price_cache_write": 0.0,
                "supports_reasoning": False,
            }
        ],
    )
    models = get_openrouter_catalog(tmp_path)
    assert len(models) == 1
    assert models[0] == OpenRouterModelInfo(
        id="openrouter/auto",
        name="Auto Router",
        context_length=0,
        price_prompt=-1.0,
        price_completion=-1.0,
        price_cache_read=0.0,
        price_cache_write=0.0,
        supports_reasoning=False,
    )


def test_get_openrouter_catalog_corrupt_file_returns_empty(tmp_path: Path) -> None:
    cache_path = tmp_path / "etc" / "openrouter-models.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("not valid json {", encoding="utf-8")
    assert get_openrouter_catalog(tmp_path) == []


def test_get_openrouter_catalog_skips_malformed_entries(tmp_path: Path) -> None:
    _write_cache(tmp_path, [{"name": "no id field"}, {"id": "ok/model"}])
    models = get_openrouter_catalog(tmp_path)
    assert [m.id for m in models] == ["ok/model"]


def test_get_openrouter_model_found(tmp_path: Path) -> None:
    _write_cache(tmp_path, [{"id": "a/b"}, {"id": "c/d"}])
    model = get_openrouter_model(tmp_path, "c/d")
    assert model is not None
    assert model.id == "c/d"


def test_get_openrouter_model_not_found(tmp_path: Path) -> None:
    _write_cache(tmp_path, [{"id": "a/b"}])
    assert get_openrouter_model(tmp_path, "nonexistent/model") is None


# ---------------------------------------------------------------------------
# refresh_openrouter_catalog -- fetch + cache, against a faked aiohttp session
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self) -> object:
        return self._payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FailingResponse:
    async def __aenter__(self) -> _FailingResponse:
        raise ConnectionError("network unreachable")

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: object) -> None:
        self._response = response

    def get(self, url: str, timeout: object = None) -> object:
        return self._response

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_session(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    monkeypatch.setattr(catalog_module.aiohttp, "ClientSession", lambda: _FakeSession(response))


@pytest.mark.asyncio
async def test_refresh_fetches_and_caches_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "data": [
            {
                "id": "openrouter/auto",
                "name": "Auto Router",
                "pricing": {"prompt": "-1", "completion": "-1"},
            },
            {
                "id": "anthropic/claude-sonnet-4",
                "name": "Claude Sonnet 4",
                "context_length": 1000000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "supported_parameters": ["reasoning"],
            },
        ]
    }
    _patch_session(monkeypatch, _FakeResponse(payload))

    models = await refresh_openrouter_catalog(tmp_path)

    assert [m.id for m in models] == ["openrouter/auto", "anthropic/claude-sonnet-4"]
    # Cache written to disk, readable back synchronously.
    assert [m.id for m in get_openrouter_catalog(tmp_path)] == [
        "openrouter/auto",
        "anthropic/claude-sonnet-4",
    ]


@pytest.mark.asyncio
async def test_refresh_skips_entries_without_an_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, _FakeResponse({"data": [{"name": "no id"}, {"id": "ok/model"}]}))

    models = await refresh_openrouter_catalog(tmp_path)

    assert [m.id for m in models] == ["ok/model"]


@pytest.mark.asyncio
async def test_refresh_network_failure_falls_back_to_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cache(tmp_path, [{"id": "already/cached"}])
    _patch_session(monkeypatch, _FailingResponse())

    models = await refresh_openrouter_catalog(tmp_path)

    assert [m.id for m in models] == ["already/cached"]
    # The existing cache file is untouched, not overwritten with an empty list.
    assert [m.id for m in get_openrouter_catalog(tmp_path)] == ["already/cached"]


@pytest.mark.asyncio
async def test_refresh_network_failure_with_no_prior_cache_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, _FailingResponse())

    models = await refresh_openrouter_catalog(tmp_path)

    assert models == []


@pytest.mark.asyncio
async def test_refresh_http_error_status_falls_back_to_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cache(tmp_path, [{"id": "already/cached"}])
    _patch_session(monkeypatch, _FakeResponse({"data": []}, status=500))

    models = await refresh_openrouter_catalog(tmp_path)

    assert [m.id for m in models] == ["already/cached"]
