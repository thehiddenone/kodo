"""A running llama-server the headless kodo server attaches to by URL.

The headless server (``kodo-server --headless-sandbox … --llama-url URL``,
doc/HEADLESS.md) never launches llama-server itself: the model runs elsewhere
— typically on the host, under ``kodo-llama-server``, while kodo runs in a
container. Unlike a ``custom_server_url`` registry entry, the model keeps its
full registry identity (thinking family, context window, sampling defaults),
because only *where* inference happens changes, not *which* model kodo
believes it is talking to.

That belief is checked once per ``(url, model)`` before first use: the
endpoint must serve the expected model (its ``--alias`` or its GGUF file
name), and its per-slot context must be at least what the compactor will
budget for — a registry/profile skew between the two sides would otherwise
surface much later as a context-overflow error mid-run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import aiohttp

from kodo.llms import LocalLLMEntry, resolve_context_window, resolve_effective_llama_config

__all__ = ["RemoteLlamaEndpoint"]

_log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 10.0
_PARALLEL_FLAGS = ("--parallel", "-np")


class RemoteLlamaEndpoint:
    """Process-wide llama-server URL that replaces kodo's managed server."""

    __url: str | None = None
    __verified: set[tuple[str, str]] = set()

    @classmethod
    def configure(cls, url: str | None) -> None:
        """Set (or clear, with ``None``) the endpoint for this process.

        Args:
            url (str | None): Base URL (``http://host:port``), no ``/v1``.
        """
        cls.__url = url.rstrip("/") if url else None
        cls.__verified = set()

    @classmethod
    def url(cls) -> str | None:
        """The configured base URL, or ``None`` when kodo manages llama-server."""
        return cls.__url

    @classmethod
    async def verify(cls, entry: LocalLLMEntry, kodo_dir: Path) -> None:
        """Check the endpoint serves *entry* with a large enough context.

        Runs the probes once per ``(url, entry.name)``; later calls return
        immediately.

        Args:
            entry (LocalLLMEntry): The registry entry kodo is about to use.
            kodo_dir (Path): User-level ``~/.kodo`` (for the active profile).

        Raises:
            RuntimeError: No endpoint configured, the endpoint is unreachable,
                it serves a different model, or its context is too small.
        """
        url = cls.__url
        if url is None:
            raise RuntimeError("No remote llama-server endpoint is configured")
        if (url, entry.name) in cls.__verified:
            return
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_SECONDS)
            ) as http:
                models = await cls.__get_json(http, f"{url}/v1/models")
                props = await cls.__get_json(http, f"{url}/props")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Cannot reach llama-server at {url}: {exc}") from exc

        served_ids = cls.__model_ids(models)
        model_path = str(props.get("model_path", "") or "")
        # A plain (unaliased) llama-server reports its GGUF *path* as the model
        # id, and a split GGUF's registry filename carries a subdirectory
        # (`Model-bf16/Model-bf16-00001-of-00002.gguf`), so a file is matched
        # on base names on both sides.
        served_files = {Path(value).name for value in served_ids | {model_path} if value}
        expected = entry.path if entry.kind == "custom_file" else entry.filename
        expected_file = Path(expected).name if expected else ""
        if entry.name not in served_ids and expected_file not in served_files:
            raise RuntimeError(
                f"llama-server at {url} serves {sorted(served_ids) or [model_path]!r}, "
                f"not {entry.name!r}. Start it with `kodo-llama-server start --model "
                f"{entry.name}` or pass the matching --model."
            )

        llama_args, _ = resolve_effective_llama_config(kodo_dir, entry)
        expected_ctx = resolve_context_window(entry, llama_args) // cls.__parallel(llama_args)
        served_ctx = cls.__served_ctx(props)
        if served_ctx and expected_ctx and served_ctx < expected_ctx:
            raise RuntimeError(
                f"llama-server at {url} runs {entry.name!r} with a {served_ctx}-token context "
                f"per slot, but this kodo's registry expects {expected_ctx}. The two sides' "
                "local-llm-registry.json (active profile) or py-kodo versions differ."
            )
        cls.__verified.add((url, entry.name))
        _log.info("Attached to llama-server at %s serving %s", url, entry.name)

    @staticmethod
    async def __get_json(http: aiohttp.ClientSession, url: str) -> dict[str, object]:
        async with http.get(url) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
        return cast(dict[str, object], data) if isinstance(data, dict) else {}

    @staticmethod
    def __model_ids(models: dict[str, object]) -> set[str]:
        ids: set[str] = set()
        for key in ("data", "models"):
            listed = models.get(key)
            if not isinstance(listed, list):
                continue
            for item in listed:
                if isinstance(item, dict):
                    for field in ("id", "name", "model"):
                        value = item.get(field)
                        if isinstance(value, str) and value:
                            ids.add(value)
        return ids

    @staticmethod
    def __served_ctx(props: dict[str, object]) -> int:
        settings = props.get("default_generation_settings")
        for source in (settings if isinstance(settings, dict) else {}, props):
            value = source.get("n_ctx")
            if isinstance(value, int) and value > 0:
                return value
        return 0

    @staticmethod
    def __parallel(llama_args: dict[str, str]) -> int:
        for flag in _PARALLEL_FLAGS:
            raw = llama_args.get(flag, "")
            if raw.isdigit() and int(raw) > 0:
                return int(raw)
        return 1
