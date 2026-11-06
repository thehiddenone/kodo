"""The Bedrock bearer token, as it travels kodo's one-secret-per-vendor channel.

Bedrock authenticates with a short-term bearer token
(``bedrock-api-key-...``), minted from the user's own AWS credentials
(doc/LLM_REGISTRY.md §3b). It is one opaque string, which is exactly what the
``api_key.request`` pull protocol (doc/WS_PROTOCOL.md §6.3) moves, so nothing
about that protocol or the key broker is specific to Bedrock.

The check here exists for one failure: a credential saved by an older kodo (an
IAM access-key pair packed into a JSON object) reaching the plugin, for
instance through a ``BEDROCK_API_KEY`` left in a headless run's environment.
That has to name what is wrong rather than surface as an opaque 403 from AWS.
"""

from __future__ import annotations

__all__ = ["InvalidCredentialsError", "require_bedrock_token"]


class InvalidCredentialsError(ValueError):
    """The Bedrock "api key" is not a usable bearer token.

    Raised eagerly at plugin construction rather than at request time so the
    failure surfaces as a plain configuration error.
    """


def require_bedrock_token(api_key: str) -> str:
    """Validate the string a client returned for vendor ``"bedrock"``.

    Args:
        api_key (str): The string from an ``api_key.request`` response.

    Returns:
        str: The bearer token, stripped of surrounding whitespace.

    Raises:
        InvalidCredentialsError: The string is empty, or it is the JSON
            access-key pair kodo no longer accepts.
    """
    token = api_key.strip()
    if not token:
        raise InvalidCredentialsError("AWS Bedrock needs a bearer token; none was provided.")
    if token.startswith("{"):
        raise InvalidCredentialsError(
            "AWS Bedrock no longer accepts an IAM access-key pair. Kōdo generates a short-term "
            "bearer token from your AWS profile (Kōdo Settings → AWS Bedrock); a headless run "
            "takes one from AWS_BEARER_TOKEN_BEDROCK."
        )
    return token
