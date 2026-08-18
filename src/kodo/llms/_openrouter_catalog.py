"""Dynamic OpenRouter model catalog — fetched, not compiled in.

Every other cloud vendor's models live in a hardcoded ``CloudLLMEntry`` tuple
in :mod:`kodo.llms._cloud_registry`. OpenRouter is an aggregator with 400+
models (as of 2026-08-17) across many upstream providers, with per-model
pricing/context-length/reasoning-support metadata that changes over time — a
hand-maintained tuple isn't a fit (see ``_cloud_registry``'s module
docstring on why OpenRouter is deliberately absent from it). This module owns
fetching OpenRouter's own public catalog (``GET
https://openrouter.ai/api/v1/models``, no API key required) and caching it to
``~/.kodo/etc/openrouter-models.json`` — the same directory
:mod:`kodo.llms.local_registry` uses for its own JSON-backed registry file —
serving it back synchronously for the Cloud AI Settings webview's searchable
model picker, and for context-window/reasoning-support lookups elsewhere in
this package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import aiohttp

__all__ = [
    "OpenRouterModelInfo",
    "get_openrouter_catalog",
    "get_openrouter_model",
    "refresh_openrouter_catalog",
    "run_openrouter_catalog_refresh_loop",
]

_log = logging.getLogger(__name__)

_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_RELATIVE_PATH = ("etc", "openrouter-models.json")

#: How long a cached catalog is trusted before the background refresh loop
#: re-fetches it — a kodo-server process can run for days, and OpenRouter
#: adds/retires models and updates pricing on its own schedule.
_CATALOG_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class OpenRouterModelInfo:
    """One model from OpenRouter's own catalog.

    Attributes:
        id: OpenRouter model id — also the value sent as the request
            ``model`` — e.g. ``"anthropic/claude-sonnet-4"``, or the special
            router pseudo-model ``"openrouter/auto"``.
        name: Human-readable display name.
        context_length: Maximum input context in tokens; ``0`` if unknown.
        price_prompt / price_completion / price_cache_read / price_cache_write:
            USD per token. ``openrouter/auto``'s own catalog entry reports
            ``-1`` for ``price_prompt``/``price_completion`` — its real
            per-token cost depends on whichever model it routes a given
            request to, so it must never be priced from this table (see
            ``Usage.provider_reported_cost``, which is why OpenRouter usage
            is priced from what the API actually reports per call instead).
        supports_reasoning: Whether this model's own ``supported_parameters``
            lists ``"reasoning"``.
    """

    id: str
    name: str
    context_length: int
    price_prompt: float
    price_completion: float
    price_cache_read: float
    price_cache_write: float
    supports_reasoning: bool


def _cache_file(kodo_dir: Path) -> Path:
    return kodo_dir.joinpath(*_CACHE_RELATIVE_PATH)


def _parse_price(pricing: dict[str, object], key: str) -> float:
    raw = pricing.get(key)
    if raw is None:
        return 0.0
    try:
        return float(str(raw))
    except ValueError:
        return 0.0


def _model_from_api_json(raw: dict[str, object]) -> OpenRouterModelInfo | None:
    """Parse one entry of OpenRouter's own ``/models`` response ``data`` array."""
    model_id = str(raw.get("id", "")).strip()
    if not model_id:
        return None
    pricing = raw.get("pricing")
    pricing_dict = pricing if isinstance(pricing, dict) else {}
    supported = raw.get("supported_parameters")
    supports_reasoning = isinstance(supported, list) and "reasoning" in supported
    context_length = raw.get("context_length")
    return OpenRouterModelInfo(
        id=model_id,
        name=str(raw.get("name") or model_id),
        context_length=int(context_length) if isinstance(context_length, int | float) else 0,
        price_prompt=_parse_price(pricing_dict, "prompt"),
        price_completion=_parse_price(pricing_dict, "completion"),
        price_cache_read=_parse_price(pricing_dict, "input_cache_read"),
        price_cache_write=_parse_price(pricing_dict, "input_cache_write"),
        supports_reasoning=supports_reasoning,
    )


def _model_to_cache_json(model: OpenRouterModelInfo) -> dict[str, object]:
    return {
        "id": model.id,
        "name": model.name,
        "context_length": model.context_length,
        "price_prompt": model.price_prompt,
        "price_completion": model.price_completion,
        "price_cache_read": model.price_cache_read,
        "price_cache_write": model.price_cache_write,
        "supports_reasoning": model.supports_reasoning,
    }


def _model_from_cache_json(raw: dict[str, object]) -> OpenRouterModelInfo | None:
    try:
        return OpenRouterModelInfo(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            context_length=int(cast(int, raw.get("context_length", 0)) or 0),
            price_prompt=float(cast(float, raw.get("price_prompt", 0.0)) or 0.0),
            price_completion=float(cast(float, raw.get("price_completion", 0.0)) or 0.0),
            price_cache_read=float(cast(float, raw.get("price_cache_read", 0.0)) or 0.0),
            price_cache_write=float(cast(float, raw.get("price_cache_write", 0.0)) or 0.0),
            supports_reasoning=bool(raw.get("supports_reasoning", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def refresh_openrouter_catalog(kodo_dir: Path) -> list[OpenRouterModelInfo]:
    """Fetch OpenRouter's full model catalog and cache it to disk.

    One unauthenticated ``GET`` (OpenRouter's model list needs no API key).
    A network or parse failure logs a warning and leaves any existing cache
    file untouched, so a transient outage never wipes out the last-known-good
    catalog the UI is showing — same tolerance as the llama.cpp
    update-checker.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        list[OpenRouterModelInfo]: The freshly fetched catalog, or whatever
        was already cached on disk if the fetch failed.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(_MODELS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp,
        ):
            resp.raise_for_status()
            body = await resp.json()
    except Exception as exc:  # noqa: BLE001 — any network/parse failure is non-fatal here
        _log.warning("Failed to fetch OpenRouter model catalog: %s", exc)
        return get_openrouter_catalog(kodo_dir)

    raw_models = body.get("data") if isinstance(body, dict) else None
    models = (
        [m for r in raw_models if isinstance(r, dict) for m in [_model_from_api_json(r)] if m]
        if isinstance(raw_models, list)
        else []
    )

    cache_path = _cache_file(kodo_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "models": [_model_to_cache_json(m) for m in models],
    }
    try:
        cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("Failed to write OpenRouter model catalog cache: %s", exc)

    _log.info("Fetched %d OpenRouter models", len(models))
    return models


def get_openrouter_catalog(kodo_dir: Path) -> list[OpenRouterModelInfo]:
    """The cached OpenRouter catalog, ``[]`` if never fetched or unreadable.

    Synchronous — reads the on-disk cache :func:`refresh_openrouter_catalog`
    wrote, never makes a network call itself.
    """
    cache_path = _cache_file(kodo_dir)
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(raw_models, list):
        return []
    return [
        model
        for entry in raw_models
        if isinstance(entry, dict)
        for model in [_model_from_cache_json(entry)]
        if model is not None
    ]


def get_openrouter_model(kodo_dir: Path, model_id: str) -> OpenRouterModelInfo | None:
    """Look up one cached model by id, or ``None`` if unknown/not yet fetched."""
    for model in get_openrouter_catalog(kodo_dir):
        if model.id == model_id:
            return model
    return None


async def run_openrouter_catalog_refresh_loop(kodo_dir: Path) -> None:
    """Fetch immediately, then keep the catalog fresh on a 12-hour TTL.

    Meant to run forever as a background task started once at server startup
    (see ``kodo.server._app._start_background``). A kodo-server process can
    stay up for days with clients reconnecting to an already-open control
    channel rather than sending a fresh ``hello``, so refreshing only in
    response to incoming connections would let the catalog go stale
    indefinitely.
    """
    while True:
        await refresh_openrouter_catalog(kodo_dir)
        await asyncio.sleep(_CATALOG_TTL_SECONDS)
