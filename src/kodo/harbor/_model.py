"""The model a benchmark runs, checked on the host before any trial is queued.

``--model`` has ``kodo-headless``'s three spellings (:class:`kodo.headless.ModelSpec`).
Everything that can be known before Docker starts is checked here, so a typo
fails in a second instead of on trial 400:

- **cloud** (``VENDOR/MODEL_ID``): the vendor is one kodo supports; for a
  vendor with a fixed catalog the model id is in it (OpenRouter and Bedrock
  catalogs are fetched at runtime, so any id passes); a credential for the
  vendor is set in *this* environment. Only the variables that carry it are
  forwarded, as ``${NAME}`` templates Harbor resolves from the host
  environment at trial start — the key itself is never written to disk.
- **local** (``ENTRY``): the entry is in the host's local registry. The model
  runs on the host under ``kodo-llama-server`` (:class:`~._llama.HostLlama`).

The same object answers what the control arm needs: the LiteLLM model name
and credential variables Harbor's ``terminus-2`` uses for the same model.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from kodo.headless import ModelSpec, resolve_vendor_api_key, vendor_credential_env_names
from kodo.llms import get_cloud_entry, get_cloud_registry, get_local_registry

from ._errors import HarborRunError

__all__ = ["BenchModel"]

# Vendors whose catalog is fetched at runtime (doc/LLM_REGISTRY.md §3a/§3b):
# any model id may be valid, so none is rejected up front.
_RUNTIME_CATALOG_VENDORS = frozenset({"openrouter", "bedrock"})

# The API host each vendor plugin calls (the base URLs in kodo/llms/<vendor>/).
# A task whose network is an allowlist must allow it during the agent phase.
_VENDOR_HOSTS: dict[str, tuple[str, ...]] = {
    "anthropic": ("api.anthropic.com",),
    "openai": ("api.openai.com",),
    "google": ("generativelanguage.googleapis.com",),
    "deepseek": ("api.deepseek.com",),
    "kimi": ("api.moonshot.ai",),
    "alibaba": ("dashscope-intl.aliyuncs.com",),
    "meta": ("api.meta.ai",),
    "openrouter": ("openrouter.ai",),
}

# kodo vendor → (LiteLLM provider prefix, the variable LiteLLM reads the key from).
_LITELLM: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "google": ("gemini", "GEMINI_API_KEY"),
    "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "kimi": ("moonshot", "MOONSHOT_API_KEY"),
    "alibaba": ("dashscope", "DASHSCOPE_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
}


class BenchModel:
    """A validated ``--model``: its Harbor spelling, credentials and network needs."""

    __spec: ModelSpec
    __env: dict[str, str]

    def __init__(self, spec: ModelSpec, env: Mapping[str, str]) -> None:
        """Bind an already-validated model.

        Args:
            spec (ModelSpec): The model.
            env (Mapping[str, str]): The host environment it was checked against.
        """
        self.__spec = spec
        self.__env = dict(env)

    @classmethod
    def resolve(cls, text: str, env: Mapping[str, str], kodo_dir: Path) -> BenchModel:
        """Parse and check ``--model``.

        Args:
            text (str): ``ENTRY``, ``local/ENTRY`` or ``VENDOR/MODEL_ID``.
            env (Mapping[str, str]): The host environment (credentials).
            kodo_dir (Path): The host's ``~/.kodo`` (the local registry).

        Returns:
            BenchModel: The model.

        Raises:
            HarborRunError: The model is malformed, unknown, or has no key.
        """
        try:
            spec = ModelSpec.parse(text)
        except ValueError as exc:
            raise HarborRunError(f"--model {text!r}: {exc}") from exc
        vendor = spec.vendor
        if vendor is None:
            entries = get_local_registry(kodo_dir)
            if spec.name not in entries:
                close = sorted(n for n in entries if spec.name.split("-")[0] in n)[:8]
                hint = f"; similar: {', '.join(close)}" if close else ""
                raise HarborRunError(f"No local model {spec.name!r} in the registry{hint}")
            return cls(spec, env)
        catalog = get_cloud_registry()
        known = sorted({*catalog, *_RUNTIME_CATALOG_VENDORS})
        if vendor not in known:
            raise HarborRunError(
                f"Unknown cloud vendor {vendor!r}; kodo supports: {', '.join(known)}"
            )
        if vendor not in _RUNTIME_CATALOG_VENDORS and get_cloud_entry(vendor, spec.name) is None:
            ids = ", ".join(entry.model_id for entry in catalog[vendor])
            raise HarborRunError(f"{vendor} has no model {spec.name!r}; known: {ids}")
        if resolve_vendor_api_key(vendor, env) is None:
            names = ", ".join(vendor_credential_env_names(vendor))
            raise HarborRunError(f"No {vendor} credential in this environment; set one of {names}")
        return cls(spec, env)

    @property
    def spec(self) -> ModelSpec:
        """The parsed model."""
        return self.__spec

    @property
    def is_local(self) -> bool:
        """Whether the model runs on a host llama-server."""
        return not self.__spec.is_cloud

    @property
    def harbor_model_name(self) -> str:
        """The trial's ``model_name``: ``VENDOR/MODEL_ID`` or ``local/ENTRY``.

        Harbor splits it on the first ``/`` into the provider and model it
        reports in every trial result.
        """
        if self.__spec.is_cloud:
            return self.__spec.label
        return f"local/{self.__spec.name}"

    def kodo_env(self) -> dict[str, str]:
        """The credential variables forwarded to Kodo's trials, as templates.

        Returns:
            dict[str, str]: ``{NAME: "${NAME}"}`` for each variable that is set.
        """
        vendor = self.__spec.vendor
        if vendor is None:
            return {}
        return {
            name: f"${{{name}}}"
            for name in vendor_credential_env_names(vendor)
            if self.__env.get(name, "").strip()
        }

    def allowed_hosts(self) -> list[str]:
        """Hosts Kodo's trials must reach during the agent phase.

        Returns:
            list[str]: The vendor API host(s); empty for a local model.
        """
        vendor = self.__spec.vendor
        if vendor is None:
            return []
        if vendor == "bedrock":
            region = self.__env.get("AWS_REGION") or self.__env.get("AWS_DEFAULT_REGION")
            return [f"bedrock-runtime.{region or 'us-east-1'}.amazonaws.com"]
        return list(_VENDOR_HOSTS.get(vendor, ()))

    def litellm_model(self) -> tuple[str, dict[str, str]]:
        """The same cloud model as LiteLLM names it, for a ``terminus-2`` control arm.

        Returns:
            tuple[str, dict[str, str]]: ``PROVIDER/MODEL_ID`` and the agent env
            (LiteLLM's key variable → a ``${NAME}`` template of the host's).

        Raises:
            HarborRunError: A local model, or a vendor LiteLLM has no provider for.
        """
        vendor = self.__spec.vendor
        if vendor is None or vendor not in _LITELLM:
            raise HarborRunError(f"No LiteLLM mapping for {self.__spec.label!r}")
        prefix, key_name = _LITELLM[vendor]
        source = next(
            name for name in vendor_credential_env_names(vendor) if self.__env.get(name, "").strip()
        )
        return f"{prefix}/{self.__spec.name}", {key_name: f"${{{source}}}"}
