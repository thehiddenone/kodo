"""Tests for ``kodo.llms.bedrock._credentials`` -- the JSON access-key blob.

AWS needs two secrets where every other vendor here needs one, and kodo-vsix
packs both into the single opaque string the ``api_key.request`` pull protocol
already moves (doc/LLM_REGISTRY.md §3b). These tests pin that contract from
the server side, including the failure a user hits by pasting a bare access
key id the way they would any other vendor's key.
"""

from __future__ import annotations

import json

import pytest

from kodo.llms.bedrock import InvalidCredentialsError, parse_bedrock_credentials


def test_parses_full_blob() -> None:
    creds = parse_bedrock_credentials(
        json.dumps({"access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cr3t"})
    )
    assert creds.access_key_id == "AKIAEXAMPLE"
    assert creds.secret_access_key == "s3cr3t"


def test_strips_surrounding_whitespace() -> None:
    creds = parse_bedrock_credentials(
        json.dumps({"access_key_id": "  AKIAEXAMPLE \n", "secret_access_key": " s3cr3t "})
    )
    assert creds.access_key_id == "AKIAEXAMPLE"
    assert creds.secret_access_key == "s3cr3t"


def test_ignores_unknown_fields() -> None:
    """A blob written by a newer client must not break an older server."""
    creds = parse_bedrock_credentials(
        json.dumps(
            {
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "s3cr3t",
                "session_token": "unused-for-now",
            }
        )
    )
    assert creds.access_key_id == "AKIAEXAMPLE"


def test_bare_key_string_is_rejected() -> None:
    """The mistake to expect: pasting an access key id like any other vendor's key."""
    with pytest.raises(InvalidCredentialsError):
        parse_bedrock_credentials("AKIAEXAMPLE")


def test_json_array_is_rejected() -> None:
    with pytest.raises(InvalidCredentialsError):
        parse_bedrock_credentials('["AKIAEXAMPLE", "s3cr3t"]')


def test_missing_secret_is_rejected() -> None:
    with pytest.raises(InvalidCredentialsError):
        parse_bedrock_credentials(json.dumps({"access_key_id": "AKIAEXAMPLE"}))


def test_empty_access_key_id_is_rejected() -> None:
    with pytest.raises(InvalidCredentialsError):
        parse_bedrock_credentials(
            json.dumps({"access_key_id": "   ", "secret_access_key": "s3cr3t"})
        )


def test_empty_string_is_rejected() -> None:
    with pytest.raises(InvalidCredentialsError):
        parse_bedrock_credentials("")
