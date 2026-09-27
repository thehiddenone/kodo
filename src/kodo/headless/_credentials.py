"""Cloud credentials for a headless run, read from the process environment.

There is no user to type a key, so ``api_key.request`` is answered from
environment variables. Each vendor accepts the name kodo itself would use
(``<VENDOR>_API_KEY``) plus the names other tools use for the same key —
Harbor's provider table among them — so an ``--agent-env`` value lands where
kodo looks without renaming.

Bedrock is the one structured credential: kodo carries AWS's access-key pair
as one JSON string (``kodo.llms.bedrock``'s ``parse_bedrock_credentials``).
``BEDROCK_API_KEY`` may hold that JSON directly; otherwise it is built from the
standard ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` pair. The region is
not a secret and travels in settings instead (:func:`bedrock_region`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

__all__ = [
    "bedrock_region",
    "resolve_vendor_api_key",
    "vendor_credential_env_names",
]

_ALIASES: dict[str, tuple[str, ...]] = {
    "google": ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "alibaba": ("DASHSCOPE_API_KEY",),
    "kimi": ("MOONSHOT_API_KEY",),
    "meta": ("LLAMA_API_KEY",),
}
_AWS_PAIR = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
_AWS_REGION = ("AWS_REGION", "AWS_DEFAULT_REGION")


def _key_names(vendor: str) -> tuple[str, ...]:
    own = f"{vendor.upper().replace('-', '_')}_API_KEY"
    return (own, *_ALIASES.get(vendor, ()))


def vendor_credential_env_names(vendor: str) -> tuple[str, ...]:
    """Every environment variable that can carry *vendor*'s credential.

    The first name is the one kodo prefers. ``kodo-harbor`` forwards exactly
    these into a task container, so nothing else from the host environment
    reaches it.

    Args:
        vendor (str): The cloud vendor key (``anthropic``, ``bedrock``, …).

    Returns:
        tuple[str, ...]: The variable names, preferred first.
    """
    names = _key_names(vendor)
    if vendor == "bedrock":
        names = (*names, *_AWS_PAIR, *_AWS_REGION)
    return names


def resolve_vendor_api_key(vendor: str, env: Mapping[str, str]) -> str | None:
    """The credential string kodo's ``api_key.request`` expects for *vendor*.

    Args:
        vendor (str): The cloud vendor key.
        env (Mapping[str, str]): The environment to read (``os.environ``).

    Returns:
        str | None: The key, or ``None`` when no variable carries one.
    """
    for name in _key_names(vendor):
        value = env.get(name, "").strip()
        if value:
            return value
    if vendor == "bedrock":
        access_key_id, secret_access_key = (env.get(name, "").strip() for name in _AWS_PAIR)
        if access_key_id and secret_access_key:
            return json.dumps(
                {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            )
    return None


def bedrock_region(env: Mapping[str, str]) -> str | None:
    """The AWS region named by the environment, if any.

    Args:
        env (Mapping[str, str]): The environment to read.

    Returns:
        str | None: ``AWS_REGION`` or ``AWS_DEFAULT_REGION``, else ``None``.
    """
    for name in _AWS_REGION:
        value = env.get(name, "").strip()
        if value:
            return value
    return None
