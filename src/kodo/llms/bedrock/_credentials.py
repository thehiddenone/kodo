"""AWS long-term IAM user access keys, carried over kodo's one-secret-per-vendor channel.

Every other cloud vendor authenticates with a single opaque string, which is
exactly what the ``api_key.request`` pull protocol (doc/WS_PROTOCOL.md §6.3)
was built to move: the extension holds the secret in VS Code SecretStorage and
hands the server one string on demand. AWS needs **two** values instead — an
access key id and a secret access key (doc/LLM_REGISTRY.md §3b) — so kodo-vsix
packs both into one JSON object and sends that as the "api key":

.. code-block:: json

    {"access_key_id": "AKIA...", "secret_access_key": "..."}

Nothing about the protocol, the key broker, or the named-multi-key management
in kodo-vsix's ``cloud-credentials.ts`` changes; only this module knows the
string is structured. The **region is deliberately not in here** — it is not a
secret, it belongs to the account's Bedrock setup rather than to the
credential, and settings already carry non-secret per-vendor knobs
(``bedrock_region``, doc/SETTINGS.md §2.2c), which the plugin factory reads
straight off the settings dict the same way Meta's contributor tier is read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["BedrockCredentials", "InvalidCredentialsError", "parse_bedrock_credentials"]


class InvalidCredentialsError(ValueError):
    """The stored Bedrock "api key" is not a usable access-key pair.

    Raised eagerly at plugin construction rather than at request time so the
    failure surfaces as a plain configuration error instead of an opaque
    botocore ``UnrecognizedClientException`` three layers down.
    """


@dataclass(frozen=True)
class BedrockCredentials:
    """One long-term IAM user access key.

    Attributes:
        access_key_id: The ``AKIA...`` access key id.
        secret_access_key: Its matching secret. Never logged, never written to
            disk by the server (NFR-06) — it lives only in VS Code
            SecretStorage on the client side.
    """

    access_key_id: str
    secret_access_key: str


def parse_bedrock_credentials(api_key: str) -> BedrockCredentials:
    """Unpack the JSON credential blob kodo-vsix sends for the ``bedrock`` vendor.

    Args:
        api_key (str): The string the client returned for an
            ``api_key.request`` naming vendor ``"bedrock"``.

    Returns:
        BedrockCredentials: The parsed access-key pair.

    Raises:
        InvalidCredentialsError: The string isn't a JSON object, or either
            field is missing/empty. A bare access key id pasted on its own
            (the shape every *other* vendor's key has) lands here too, with a
            message naming what's actually expected.
    """
    try:
        raw = json.loads(api_key)
    except json.JSONDecodeError as exc:
        raise InvalidCredentialsError(
            "AWS Bedrock credentials must be an access key id and secret access key pair; "
            "re-add the key in Kōdo Settings → AWS Bedrock."
        ) from exc

    if not isinstance(raw, dict):
        raise InvalidCredentialsError("AWS Bedrock credentials must be a JSON object.")

    access_key_id = str(raw.get("access_key_id", "")).strip()
    secret_access_key = str(raw.get("secret_access_key", "")).strip()
    if not access_key_id or not secret_access_key:
        raise InvalidCredentialsError(
            "AWS Bedrock credentials are missing an access key id or secret access key."
        )
    return BedrockCredentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
