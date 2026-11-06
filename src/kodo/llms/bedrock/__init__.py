"""AWS Bedrock LLM plugin — Bedrock's whole regional model catalog via the Converse API."""

from ._bedrock import BedrockPlugin
from ._client import bearer_client
from ._credentials import InvalidCredentialsError, require_bedrock_token
from ._retry import RetryExhaustedError, UnrecoverableError
from ._token_mint import (
    BedrockToken,
    BedrockTokenError,
    list_bedrock_profiles,
    mint_bedrock_token,
)

__all__ = [
    "BedrockPlugin",
    "BedrockToken",
    "BedrockTokenError",
    "InvalidCredentialsError",
    "RetryExhaustedError",
    "UnrecoverableError",
    "bearer_client",
    "list_bedrock_profiles",
    "mint_bedrock_token",
    "require_bedrock_token",
]
