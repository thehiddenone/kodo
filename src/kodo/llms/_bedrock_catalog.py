"""Dynamic AWS Bedrock model catalog — fetched, not compiled in.

The Bedrock counterpart of :mod:`kodo.llms._openrouter_catalog`, and built to
the same decisions (doc/LLM_REGISTRY.md §3b): Bedrock is an aggregator with
110+ text models across ~18 providers whose availability differs per region
and changes on AWS's schedule, so a hardcoded ``CloudLLMEntry`` tuple in
:mod:`kodo.llms._cloud_registry` isn't a fit and Bedrock is deliberately
absent from it. The parsed catalog is cached to
``~/.kodo/etc/bedrock-models.json`` and served back synchronously for the
Cloud AI Settings webview's searchable model picker and for context-window
lookups elsewhere in this package.

Three things differ from OpenRouter's catalog, all forced by AWS:

* **The fetch needs credentials.** OpenRouter's ``/models`` endpoint is
  unauthenticated, so the server refreshes it on a background TTL loop from
  startup. ``ListFoundationModels`` is a signed control-plane call, and the
  server holds no credentials of its own — they live in VS Code SecretStorage
  and are pulled per request (doc/WS_PROTOCOL.md §6.3). So there is **no
  background loop here**: the refresh is client-driven
  (``bedrock.models.refresh``, §7.6i), which carries the credentials and the
  region with it. Kicking off a server-side loop would either fire a
  credential prompt at the user unprompted or silently do nothing.
* **The cache is region-scoped.** Bedrock is regional — which models exist,
  and which inference profiles can serve them, both depend on the region — so
  the cache records which region it was fetched for and
  :func:`get_bedrock_catalog` returns ``[]`` for any other one. That empty
  result is what makes a region change re-trigger the client's refresh, with
  no extra invalidation protocol.
* **Two calls, merged.** Many Bedrock models cannot be invoked on demand by
  their bare model id at all — they return a ``ValidationException`` telling
  the caller to use an inference profile instead — so
  ``ListInferenceProfiles`` is fetched alongside and its cross-region profile
  ids (``us.``/``eu.``/``apac.``/...) are offered as first-class catalog
  entries, flagged so the UI can prefer them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import boto3
from botocore.config import Config

from .bedrock import parse_bedrock_credentials

__all__ = [
    "BEDROCK_REGIONS",
    "DEFAULT_BEDROCK_REGION",
    "BedrockModelInfo",
    "get_bedrock_catalog",
    "get_bedrock_model",
    "refresh_bedrock_catalog",
]

_log = logging.getLogger(__name__)

_CONTROL_PLANE_SERVICE = "bedrock"
_CACHE_RELATIVE_PATH = ("etc", "bedrock-models.json")

#: Bedrock's default region when the user hasn't chosen one — the region with
#: the broadest model availability, and the one AWS's own examples use.
DEFAULT_BEDROCK_REGION = "us-east-1"

#: Regions offered in the Cloud AI Settings region picker. Not exhaustive and
#: not authoritative (AWS adds Bedrock regions on its own schedule); the list
#: exists so the common case is one click rather than a typed string, and the
#: settings key itself accepts any region string.
BEDROCK_REGIONS: tuple[str, ...] = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ca-central-1",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "sa-east-1",
)

# Bedrock exposes **no context-window field** on ListFoundationModels or
# GetFoundationModel -- unlike OpenRouter, whose catalog carries a real
# `context_length` per model. Leaving it unknown would silently hand every
# Bedrock model kodo's 262,144-token default (kodo.llms._context), which is
# *larger* than most of them and would let auto-compaction overfill a context
# until the provider hard-errors. This best-effort family table is the
# conservative direction instead: a documented published figure per family,
# and 0 ("unknown", falls back to the default) for anything unrecognised.
# Matched as a substring so a cross-region prefix and a version suffix around
# the family name still hit.
_FAMILY_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("anthropic.claude-3-haiku", 200_000),
    ("anthropic.claude", 200_000),
    ("amazon.nova-premier", 1_000_000),
    ("amazon.nova", 300_000),
    ("amazon.titan", 32_000),
    ("meta.llama", 128_000),
    ("mistral.pixtral", 128_000),
    ("mistral.mistral-large", 128_000),
    ("mistral.", 32_000),
    ("cohere.command-r", 128_000),
    ("cohere.", 4_000),
    ("ai21.jamba", 256_000),
    ("deepseek.", 128_000),
    ("qwen.", 128_000),
    ("openai.gpt-oss", 128_000),
    ("writer.palmyra", 128_000),
    ("moonshotai.", 256_000),
)


@dataclass(frozen=True)
class BedrockModelInfo:
    """One invocable Bedrock target — a foundation model or an inference profile.

    Attributes:
        id: What goes on the wire as Converse's ``modelId``: either a bare
            foundation-model id (``"anthropic.claude-opus-5"``) or a
            cross-region inference-profile id
            (``"us.anthropic.claude-opus-5"``).
        name: Human-readable display name.
        provider: Provider name from Bedrock (``"Anthropic"``, ``"Meta"``,
            ...), or ``""`` for a profile whose provider couldn't be resolved.
            Bedrock groups its catalog by provider and users search it that
            way, which is why this exists here and has no OpenRouter analogue.
        context_length: Best-effort maximum input context in tokens; ``0``
            when unknown — see :data:`_FAMILY_CONTEXT_WINDOWS` for why this
            can't come from the API.
        inference_profile: ``True`` when :attr:`id` is a cross-region
            inference profile. Many models are on-demand-invocable *only*
            this way, so the picker surfaces these first.
        supports_streaming: Whether Bedrock reports response streaming for
            this model. kodo always streams, so a ``False`` here is unusable
            and such entries are filtered out before they reach the cache.
    """

    id: str
    name: str
    provider: str
    context_length: int
    inference_profile: bool
    supports_streaming: bool


def _cache_file(kodo_dir: Path) -> Path:
    return kodo_dir.joinpath(*_CACHE_RELATIVE_PATH)


def _context_window_for(model_id: str) -> int:
    for marker, window in _FAMILY_CONTEXT_WINDOWS:
        if marker in model_id:
            return window
    return 0


def _model_to_cache_json(model: BedrockModelInfo) -> dict[str, object]:
    return {
        "id": model.id,
        "name": model.name,
        "provider": model.provider,
        "context_length": model.context_length,
        "inference_profile": model.inference_profile,
        "supports_streaming": model.supports_streaming,
    }


def _model_from_cache_json(raw: dict[str, object]) -> BedrockModelInfo | None:
    try:
        return BedrockModelInfo(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            provider=str(raw.get("provider", "")),
            context_length=int(cast(int, raw.get("context_length", 0)) or 0),
            inference_profile=bool(raw.get("inference_profile", False)),
            supports_streaming=bool(raw.get("supports_streaming", True)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _foundation_models(client: Any) -> list[BedrockModelInfo]:
    """Text-generation foundation models available for on-demand invocation."""
    response = client.list_foundation_models(
        byOutputModality="TEXT",
        byInferenceType="ON_DEMAND",
    )
    summaries = response.get("modelSummaries", [])
    models: list[BedrockModelInfo] = []
    for summary in summaries if isinstance(summaries, list) else []:
        if not isinstance(summary, dict):
            continue
        model_id = str(summary.get("modelId", "")).strip()
        if not model_id:
            continue
        # kodo only ever streams, and only ever sends text.
        if not summary.get("responseStreamingSupported", False):
            continue
        inputs = summary.get("inputModalities")
        if isinstance(inputs, list) and "TEXT" not in inputs:
            continue
        # Skip models AWS has already marked end-of-life; they still list but
        # reject invocation.
        lifecycle = summary.get("modelLifecycle")
        if isinstance(lifecycle, dict) and str(lifecycle.get("status", "ACTIVE")) != "ACTIVE":
            continue
        models.append(
            BedrockModelInfo(
                id=model_id,
                name=str(summary.get("modelName") or model_id),
                provider=str(summary.get("providerName", "")),
                context_length=_context_window_for(model_id),
                inference_profile=False,
                supports_streaming=True,
            )
        )
    return models


def _inference_profiles(client: Any, known: dict[str, BedrockModelInfo]) -> list[BedrockModelInfo]:
    """Amazon-defined cross-region profiles, paginated to the end.

    Each profile names the foundation models it routes to by ARN; the display
    name and provider are taken from the first of those that appears in
    *known* (the foundation-model pass), so a profile inherits the metadata of
    the model behind it rather than showing a bare id.
    """
    profiles: list[BedrockModelInfo] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, object] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_inference_profiles(**kwargs)
        summaries = response.get("inferenceProfileSummaries", [])
        for summary in summaries if isinstance(summaries, list) else []:
            if not isinstance(summary, dict):
                continue
            profile_id = str(summary.get("inferenceProfileId", "")).strip()
            if not profile_id or str(summary.get("status", "ACTIVE")) != "ACTIVE":
                continue
            backing = _backing_model(summary, known)
            profiles.append(
                BedrockModelInfo(
                    id=profile_id,
                    name=str(summary.get("inferenceProfileName") or profile_id),
                    provider=backing.provider if backing is not None else "",
                    context_length=_context_window_for(profile_id),
                    inference_profile=True,
                    supports_streaming=True,
                )
            )
        raw_token = response.get("nextToken")
        next_token = str(raw_token) if raw_token else None
        if not next_token:
            break
    return profiles


def _backing_model(
    summary: dict[str, object], known: dict[str, BedrockModelInfo]
) -> BedrockModelInfo | None:
    """The foundation model an inference profile routes to, if kodo listed it."""
    models = summary.get("models")
    for entry in models if isinstance(models, list) else []:
        if not isinstance(entry, dict):
            continue
        arn = str(entry.get("modelArn", ""))
        # "arn:aws:bedrock:<region>::foundation-model/<modelId>"
        model_id = arn.rsplit("/", 1)[-1]
        found = known.get(model_id)
        if found is not None:
            return found
    return None


def _fetch_catalog(
    access_key_id: str, secret_access_key: str, region: str
) -> list[BedrockModelInfo]:
    """Blocking control-plane fetch — run off the event loop by the caller."""
    client = boto3.client(
        _CONTROL_PLANE_SERVICE,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
        # Same no-SDK-retries rule as the runtime client (doc/LLM_GATEWAY.md);
        # a failed catalog fetch is non-fatal and simply keeps the old cache.
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )
    models = _foundation_models(client)
    known = {m.id: m for m in models}
    try:
        profiles = _inference_profiles(client, known)
    except Exception as exc:  # noqa: BLE001 — profiles are an enhancement, not a requirement
        # ListInferenceProfiles is a separate IAM action from
        # ListFoundationModels, so a narrowly scoped policy can grant one and
        # not the other. Losing profiles degrades the picker; losing the whole
        # catalog would break it.
        _log.warning("Could not list Bedrock inference profiles: %s", exc)
        profiles = []
    # Profiles first: for many models they are the only on-demand-invocable
    # form, so they should be what a search surfaces first.
    return profiles + models


async def refresh_bedrock_catalog(
    kodo_dir: Path, api_key: str, region: str
) -> list[BedrockModelInfo]:
    """Fetch Bedrock's model catalog for *region* and cache it to disk.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        api_key: The JSON IAM credential blob (see
            :func:`kodo.llms.bedrock.parse_bedrock_credentials`).
        region: AWS region to enumerate.

    Returns:
        list[BedrockModelInfo]: The freshly fetched catalog, or whatever was
        already cached for *region* if the fetch failed — a transient outage
        or a missing IAM grant never wipes out the last-known-good catalog the
        UI is showing, same tolerance as
        :func:`kodo.llms.refresh_openrouter_catalog`.
    """
    try:
        credentials = parse_bedrock_credentials(api_key)
        models = await asyncio.to_thread(
            _fetch_catalog,
            credentials.access_key_id,
            credentials.secret_access_key,
            region,
        )
    except Exception as exc:  # noqa: BLE001 — any credential/network/API failure is non-fatal
        _log.warning("Failed to fetch Bedrock model catalog for %s: %s", region, exc)
        return get_bedrock_catalog(kodo_dir, region)

    cache_path = _cache_file(kodo_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "region": region,
        "models": [_model_to_cache_json(m) for m in models],
    }
    try:
        cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("Failed to write Bedrock model catalog cache: %s", exc)

    _log.info("Fetched %d Bedrock models for %s", len(models), region)
    return models


def _read_cache(kodo_dir: Path) -> tuple[str, list[BedrockModelInfo]]:
    """The cache file's ``(region, models)``, or ``("", [])`` if unusable."""
    cache_path = _cache_file(kodo_dir)
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", []
    if not isinstance(raw, dict):
        return "", []
    raw_models = raw.get("models")
    if not isinstance(raw_models, list):
        return "", []
    return str(raw.get("region", "")), [
        model
        for entry in raw_models
        if isinstance(entry, dict)
        for model in [_model_from_cache_json(entry)]
        if model is not None
    ]


def get_bedrock_catalog(kodo_dir: Path, region: str) -> list[BedrockModelInfo]:
    """The cached catalog for *region*, ``[]`` if never fetched or unreadable.

    Synchronous — reads the on-disk cache :func:`refresh_bedrock_catalog`
    wrote, never makes a network call itself. Returns ``[]`` when the cache
    holds a *different* region's catalog, which is how a region change
    invalidates the picker without a separate protocol (see the module
    docstring).
    """
    cached_region, models = _read_cache(kodo_dir)
    return models if cached_region == region else []


def get_bedrock_model(kodo_dir: Path, model_id: str) -> BedrockModelInfo | None:
    """Look up one cached model by id, or ``None`` if unknown/not yet fetched.

    Region-agnostic on purpose, unlike :func:`get_bedrock_catalog`: the one
    caller is :func:`kodo.llms.get_context_window`, which is handed a
    ``model_id`` with no region in scope (and none in its signature — a
    context-window lookup happens deep inside the compaction path). The cache
    only ever holds the active region's catalog anyway, so matching on id
    alone is exact in practice; a stale hit after a region switch costs at
    worst a slightly wrong context estimate, where threading a region
    parameter through every caller would cost real coupling.
    """
    _, models = _read_cache(kodo_dir)
    for model in models:
        if model.id == model_id:
            return model
    return None
