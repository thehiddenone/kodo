"""Tests for ``kodo.llms._bedrock_catalog`` -- the fetched/cached Bedrock catalog.

Covers the two-call merge (ListFoundationModels + ListInferenceProfiles), the
filtering rules, the region-scoped on-disk cache, and the failure tolerances
that keep a bad fetch from wiping out a good catalog -- all against a faked
boto3 client, no AWS access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from kodo.llms import _bedrock_catalog as catalog_module
from kodo.llms._bedrock_catalog import (
    BedrockModelInfo,
    _context_window_for,
    get_bedrock_catalog,
    get_bedrock_model,
    refresh_bedrock_catalog,
)

_CREDENTIALS = json.dumps({"access_key_id": "AKIATEST", "secret_access_key": "s3cr3t"})


def _summary(model_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "modelId": model_id,
        "modelName": model_id.split(".")[-1],
        "providerName": model_id.split(".")[0].title(),
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
    }
    base.update(overrides)
    return base


class _FakeControlPlaneClient:
    def __init__(
        self,
        models: list[dict[str, Any]],
        profiles: list[dict[str, Any]] | None = None,
        profiles_error: Exception | None = None,
    ) -> None:
        self._models = models
        self._profile_pages = profiles if profiles is not None else []
        self._profiles_error = profiles_error
        self.profile_calls = 0

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        return {"modelSummaries": self._models}

    def list_inference_profiles(self, **kwargs: Any) -> dict[str, Any]:
        if self._profiles_error is not None:
            raise self._profiles_error
        page = self._profile_pages[self.profile_calls]
        self.profile_calls += 1
        return page


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeControlPlaneClient) -> None:
    monkeypatch.setattr(catalog_module.boto3, "client", lambda *a, **k: client)


# ---------------------------------------------------------------------------
# _context_window_for -- the best-effort family table
# ---------------------------------------------------------------------------


def test_context_window_for_known_family() -> None:
    assert _context_window_for("us.anthropic.claude-opus-5") == 200_000


def test_context_window_for_unknown_family_is_zero() -> None:
    """0 means "unknown", which falls back to kodo's own default downstream."""
    assert _context_window_for("some.brand-new-model-v1:0") == 0


def test_context_window_matches_most_specific_family_first() -> None:
    assert _context_window_for("amazon.nova-premier-v1:0") == 1_000_000
    assert _context_window_for("amazon.nova-pro-v1:0") == 300_000


# ---------------------------------------------------------------------------
# refresh_bedrock_catalog -- fetch, filter, merge, cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_lists_text_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _FakeControlPlaneClient([_summary("anthropic.claude-opus-5"), _summary("meta.llama4")]),
    )
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["anthropic.claude-opus-5", "meta.llama4"]
    assert models[0].provider == "Anthropic"
    assert models[0].context_length == 200_000


@pytest.mark.asyncio
async def test_refresh_skips_non_streaming_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kodo only ever streams, so a non-streaming model is unusable."""
    _install(
        monkeypatch,
        _FakeControlPlaneClient(
            [
                _summary("anthropic.claude-opus-5"),
                _summary("some.batch-only", responseStreamingSupported=False),
            ]
        ),
    )
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["anthropic.claude-opus-5"]


@pytest.mark.asyncio
async def test_refresh_skips_end_of_life_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        _FakeControlPlaneClient(
            [
                _summary("anthropic.claude-opus-5"),
                _summary("old.model", modelLifecycle={"status": "LEGACY"}),
            ]
        ),
    )
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["anthropic.claude-opus-5"]


@pytest.mark.asyncio
async def test_refresh_skips_non_text_input_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        _FakeControlPlaneClient([_summary("image.only", inputModalities=["IMAGE"])]),
    )
    assert await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1") == []


@pytest.mark.asyncio
async def test_inference_profiles_come_first_and_inherit_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many models are on-demand-invocable only through a profile."""
    _install(
        monkeypatch,
        _FakeControlPlaneClient(
            [_summary("anthropic.claude-opus-5")],
            profiles=[
                {
                    "inferenceProfileSummaries": [
                        {
                            "inferenceProfileId": "us.anthropic.claude-opus-5",
                            "inferenceProfileName": "US Claude Opus 5",
                            "status": "ACTIVE",
                            "type": "SYSTEM_DEFINED",
                            "models": [
                                {
                                    "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/"
                                    "anthropic.claude-opus-5"
                                }
                            ],
                        }
                    ]
                }
            ],
        ),
    )
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert models[0].id == "us.anthropic.claude-opus-5"
    assert models[0].inference_profile is True
    assert models[0].provider == "Anthropic"
    assert models[1].inference_profile is False


@pytest.mark.asyncio
async def test_inference_profiles_are_paginated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeControlPlaneClient(
        [],
        profiles=[
            {
                "inferenceProfileSummaries": [
                    {"inferenceProfileId": "us.a", "status": "ACTIVE", "models": []}
                ],
                "nextToken": "page-2",
            },
            {
                "inferenceProfileSummaries": [
                    {"inferenceProfileId": "us.b", "status": "ACTIVE", "models": []}
                ]
            },
        ],
    )
    _install(monkeypatch, client)
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["us.a", "us.b"]
    assert client.profile_calls == 2


@pytest.mark.asyncio
async def test_profile_listing_failure_degrades_to_models_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ListInferenceProfiles is a separate IAM action; a narrow policy may omit it."""
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "ListInferenceProfiles"
    )
    _install(
        monkeypatch,
        _FakeControlPlaneClient([_summary("anthropic.claude-opus-5")], profiles_error=denied),
    )
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["anthropic.claude-opus-5"]


# ---------------------------------------------------------------------------
# Cache round trip and region scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_round_trips_through_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _FakeControlPlaneClient([_summary("anthropic.claude-opus-5")]))
    await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    cached = get_bedrock_catalog(tmp_path, "us-east-1")
    assert [m.id for m in cached] == ["anthropic.claude-opus-5"]
    assert isinstance(cached[0], BedrockModelInfo)


@pytest.mark.asyncio
async def test_cache_is_region_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A region change must read as "empty", which is what re-triggers a refresh."""
    _install(monkeypatch, _FakeControlPlaneClient([_summary("anthropic.claude-opus-5")]))
    await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert get_bedrock_catalog(tmp_path, "eu-central-1") == []


def test_missing_cache_reads_as_empty(tmp_path: Path) -> None:
    assert get_bedrock_catalog(tmp_path, "us-east-1") == []


def test_corrupt_cache_reads_as_empty(tmp_path: Path) -> None:
    cache = tmp_path / "etc" / "bedrock-models.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json", encoding="utf-8")
    assert get_bedrock_catalog(tmp_path, "us-east-1") == []


@pytest.mark.asyncio
async def test_get_bedrock_model_is_region_agnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_context_window has no region in scope -- see the function's docstring."""
    _install(monkeypatch, _FakeControlPlaneClient([_summary("anthropic.claude-opus-5")]))
    await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    found = get_bedrock_model(tmp_path, "anthropic.claude-opus-5")
    assert found is not None
    assert found.context_length == 200_000


def test_get_bedrock_model_unknown_id_is_none(tmp_path: Path) -> None:
    assert get_bedrock_model(tmp_path, "nope") is None


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_fetch_keeps_the_previous_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _FakeControlPlaneClient([_summary("anthropic.claude-opus-5")]))
    await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "ListFoundationModels",
        )

    monkeypatch.setattr(catalog_module.boto3, "client", _boom)
    models = await refresh_bedrock_catalog(tmp_path, _CREDENTIALS, "us-east-1")
    assert [m.id for m in models] == ["anthropic.claude-opus-5"]


@pytest.mark.asyncio
async def test_invalid_credentials_do_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing key is a normal state, not an error the client must handle."""
    assert await refresh_bedrock_catalog(tmp_path, "", "us-east-1") == []
