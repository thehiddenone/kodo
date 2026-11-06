"""Mint Bedrock bearer tokens and list the AWS profiles to mint them from.

The server holds no AWS credentials. kodo-vsix asks for a token over the
control connection and keeps it in VS Code SecretStorage; this module only
produces it (doc/LLM_REGISTRY.md §3b).

``aws-bedrock-token-generator`` takes its choice of credentials from the
environment, through botocore's default chain, and the server is one process
shared by every session. Changing ``AWS_PROFILE`` here would race other
sessions and every other AWS call in the process, so each token comes from a
short-lived child process (:mod:`._token_child`) given its own environment.
That also keeps a slow ``credential_process`` or SSO refresh away from the
event loop, and makes it killable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

import botocore.exceptions
import botocore.session

__all__ = ["BedrockToken", "BedrockTokenError", "list_bedrock_profiles", "mint_bedrock_token"]

_CHILD_MODULE = "kodo.llms.bedrock._token_child"

# Long enough for a credential_process or an SSO token refresh over a slow
# link, short enough that a hung helper doesn't look like a hung server.
_MINT_TIMEOUT_SECONDS = 60.0

# With a profile picked these all have to go, or it is silently not the
# profile that gets used. botocore's EnvProvider runs first in the chain and
# only an explicit session profile disables it, which AWS_PROFILE is not; and
# AWS_DEFAULT_PROFILE outranks AWS_PROFILE.
_OVERRIDES_A_PROFILE = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_PROFILE",
)


class BedrockTokenError(Exception):
    """A token could not be minted; the message is meant for the user."""


@dataclass(frozen=True)
class BedrockToken:
    """One minted bearer token.

    Attributes:
        token: The bearer token. Never logged, and never written to disk by
            the server (NFR-06).
        issued_at: When it was minted, in epoch seconds.
        expires_at: When it stops working, in epoch seconds. Up to an hour
            after :attr:`issued_at`, and sooner when the AWS session behind it
            ends first.
    """

    token: str
    issued_at: float
    expires_at: float


def _child_environment(
    profile: str, region: str, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["AWS_REGION"] = region
    if profile:
        env["AWS_PROFILE"] = profile
        for name in _OVERRIDES_A_PROFILE:
            env.pop(name, None)
    return env


def _parse_child_output(stdout: bytes) -> BedrockToken:
    try:
        raw = json.loads(stdout)
    except ValueError as exc:
        raise BedrockTokenError("The AWS token helper returned no usable result.") from exc
    if not isinstance(raw, dict):
        raise BedrockTokenError("The AWS token helper returned no usable result.")
    error = raw.get("error")
    if error:
        raise BedrockTokenError(str(error))
    try:
        return BedrockToken(
            token=str(raw["token"]),
            issued_at=float(raw["issued_at"]),
            expires_at=float(raw["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BedrockTokenError("The AWS token helper returned no usable result.") from exc


async def mint_bedrock_token(profile: str, region: str) -> BedrockToken:
    """Mint a bearer token for *region* from *profile*'s credentials.

    Args:
        profile (str): The AWS profile, set as ``AWS_PROFILE`` for the child.
            Empty leaves the inherited environment alone, so botocore resolves
            its default credential chain.
        region (str): The AWS region, set as ``AWS_REGION`` for the child. The
            token only works in this region.

    Returns:
        BedrockToken: The minted token.

    Raises:
        BedrockTokenError: The helper failed, timed out, or AWS rejected the
            credentials (an expired SSO login, an unknown profile).
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _CHILD_MODULE,
        env=_child_environment(profile, region),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), _MINT_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BedrockTokenError(
            f"Timed out after {_MINT_TIMEOUT_SECONDS:.0f} s waiting for AWS credentials."
        ) from exc
    return _parse_child_output(stdout)


def list_bedrock_profiles() -> list[str]:
    """The AWS profiles botocore can see on this machine.

    Returns:
        list[str]: The names from ``~/.aws/config`` and ``~/.aws/credentials``,
        sorted. Empty when there are none, or when those files can't be parsed
        (the user can still type a name in, or use the default chain).
    """
    try:
        return sorted(botocore.session.get_session().available_profiles)
    except (botocore.exceptions.BotoCoreError, OSError):
        return []
