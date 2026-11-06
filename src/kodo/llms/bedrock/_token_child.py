"""Child process that mints one Bedrock token — see :mod:`._token_mint`.

Run as ``python -m kodo.llms.bedrock._token_child``, with the choice of
credentials carried by the environment: ``AWS_PROFILE`` and ``AWS_REGION``,
resolved by botocore's default credential chain. Prints one JSON object on
stdout, ``{token, issued_at, expires_at}`` or ``{error}``, and nothing else.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta

from aws_bedrock_token_generator import provide_token
from botocore.session import Session

_TOKEN_LIFETIME = timedelta(hours=1)


class _LoadedCredentials:
    """Hands provide_token the credentials this process already resolved.

    Without it provide_token resolves its own copy, and the expiry read below
    would describe a different object than the one that signed the token.
    """

    __credentials: object

    def __init__(self, credentials: object) -> None:
        """Hold the resolved credentials.

        Args:
            credentials (object): botocore credentials.
        """
        self.__credentials = credentials

    def load(self) -> object:
        """Return the held credentials.

        Returns:
            object: The credentials given to the constructor.
        """
        return self.__credentials


def _expires_at(credentials: object, issued_at: float) -> float:
    """When the token stops working, in epoch seconds.

    A token lasts for the shorter of what was asked for and the session behind
    the credentials, so one minted from an SSO or assumed-role session close
    to its end has less than an hour. Refreshing at 75% of an assumed hour
    would then come too late. botocore has no public accessor for this:
    ``_expiry_time`` is read defensively, and credentials without one (a
    long-term key) leave the requested lifetime in place.
    """
    requested = issued_at + _TOKEN_LIFETIME.total_seconds()
    session_end = getattr(credentials, "_expiry_time", None)
    if isinstance(session_end, datetime):
        return min(requested, session_end.timestamp())
    return requested


def _mint(region: str) -> dict[str, object]:
    credentials = Session().get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials found for this profile.")
    issued_at = time.time()
    token = provide_token(
        region=region,
        aws_credentials_provider=_LoadedCredentials(credentials),
        expiry=_TOKEN_LIFETIME,
    )
    return {
        "token": token,
        "issued_at": issued_at,
        "expires_at": _expires_at(credentials, issued_at),
    }


def main() -> int:
    """Mint a token and print the result.

    Returns:
        int: The process exit code, ``0`` on success.
    """
    try:
        result = _mint(os.environ.get("AWS_REGION", ""))
    except Exception as exc:  # noqa: BLE001 — every failure is reported the same way
        result = {"error": str(exc) or type(exc).__name__}
    sys.stdout.write(json.dumps(result))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
