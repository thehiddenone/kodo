"""botocore clients that authenticate with one Bedrock bearer token.

The route AWS documents is the ``AWS_BEARER_TOKEN_BEDROCK`` environment
variable, but that is process-wide and kodo's server is one process shared by
every session, so the token is set per client instead: a client built here
reads nothing from the environment or from ``~/.aws`` to authenticate.
"""

from __future__ import annotations

from typing import Any

from botocore.config import Config
from botocore.session import Session
from botocore.tokens import FrozenAuthToken

__all__ = ["bearer_client"]

_BEARER = Config(signature_version="bearer")

# Bearer auth never signs with these, but create_client resolves credentials
# regardless. Left unset, that walks the whole default chain per client: a
# metadata-service probe on a machine that isn't EC2, and an SSO lookup.
_UNUSED_CREDENTIAL = "unused"

# The server inherits VS Code's environment, and a client needs no profile. A
# stale AWS_PROFILE there would otherwise fail create_client with
# ProfileNotFound, even with the profile picked in settings working fine.
_NO_PROFILE_VARIABLE: dict[str, tuple[None, None, None, None]] = {
    "profile": (None, None, None, None),
}


class _FixedToken:
    """A botocore token provider that always answers with one token."""

    __token: str

    def __init__(self, token: str) -> None:
        """Hold *token* for the client being built.

        Args:
            token (str): The bearer token.
        """
        self.__token = token

    def load_token(self, **_kwargs: Any) -> FrozenAuthToken:
        """Return the held token, whichever signing name asks.

        Returns:
            FrozenAuthToken: The token in botocore's own shape.
        """
        return FrozenAuthToken(self.__token)


def bearer_client(service: str, token: str, region: str, config: Config) -> Any:
    """Build a botocore client for *service* that sends ``Authorization: Bearer``.

    Args:
        service (str): ``"bedrock-runtime"`` or ``"bedrock"``.
        token (str): The bearer token.
        region (str): The AWS region to call. A short-term token only works in
            the region it was minted for.
        config (Config): Timeouts and retry settings; the signature version is
            set on top of it.

    Returns:
        Any: The client (botocore generates client classes at runtime).
    """
    session = Session(session_vars=_NO_PROFILE_VARIABLE)
    session.register_component("token_provider", _FixedToken(token))
    session.set_credentials(_UNUSED_CREDENTIAL, _UNUSED_CREDENTIAL)
    return session.create_client(service, region_name=region, config=config.merge(_BEARER))
