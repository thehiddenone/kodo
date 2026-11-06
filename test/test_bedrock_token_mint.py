"""Tests for ``kodo.llms.bedrock.mint_bedrock_token`` and the profile listing.

These run the real child process against throwaway AWS config files, so they
exercise the real generator and botocore's real credential chain. The tokens
are never used: they are signed locally from fake keys, and no AWS call is made.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

from kodo.llms.bedrock import BedrockTokenError, list_bedrock_profiles, mint_bedrock_token

_AWS_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@pytest.fixture
def aws_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in _AWS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config"
    config.write_text("[profile from-config]\nregion = us-east-1\n", encoding="utf-8")
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = AKIADEFAULT\naws_secret_access_key = fake\n"
        "[work]\naws_access_key_id = AKIAWORK\naws_secret_access_key = fake\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return tmp_path


def _signed_url(token: str) -> str:
    prefix = "bedrock-api-key-"
    assert token.startswith(prefix)
    return base64.b64decode(token[len(prefix) :]).decode()


@pytest.mark.usefixtures("aws_files")
def test_profiles_come_from_both_aws_files() -> None:
    assert list_bedrock_profiles() == ["default", "from-config", "work"]


@pytest.mark.usefixtures("aws_files")
async def test_a_token_uses_the_chosen_profile_and_region() -> None:
    before = time.time()
    token = await mint_bedrock_token("work", "eu-west-1")
    url = _signed_url(token.token)
    assert "AKIAWORK" in url
    assert "eu-west-1" in url
    assert "X-Amz-Expires=3600" in url
    assert before <= token.issued_at <= time.time()
    assert token.expires_at == pytest.approx(token.issued_at + 3600)


@pytest.mark.usefixtures("aws_files")
async def test_no_profile_uses_the_default_credential_chain() -> None:
    token = await mint_bedrock_token("", "us-east-1")
    assert "AKIADEFAULT" in _signed_url(token.token)


@pytest.mark.usefixtures("aws_files")
async def test_a_chosen_profile_wins_over_keys_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFROMENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fake")
    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "default")
    token = await mint_bedrock_token("work", "us-east-1")
    assert "AKIAWORK" in _signed_url(token.token)


@pytest.mark.usefixtures("aws_files")
async def test_no_profile_leaves_environment_credentials_in_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFROMENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake")
    token = await mint_bedrock_token("", "us-east-1")
    assert "AKIAFROMENV" in _signed_url(token.token)


async def test_a_session_ending_within_the_hour_shortens_the_expiry(
    aws_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry follows the session: 20 minutes of credentials is a 20-minute token."""
    ends = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1200))
    helper = aws_files / "credential_process.py"
    helper.write_text(
        "import json\n"
        "print(json.dumps({'Version': 1, 'AccessKeyId': 'AKIASHORT', 'SecretAccessKey': 'fake',\n"
        f"                  'SessionToken': 'fake', 'Expiration': {ends!r}}}))\n",
        encoding="utf-8",
    )
    (aws_files / "config").write_text(
        f'[profile short]\ncredential_process = "{sys.executable}" "{helper}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE")
    token = await mint_bedrock_token("short", "us-east-1")
    assert token.expires_at - token.issued_at == pytest.approx(1200, abs=30)


@pytest.mark.usefixtures("aws_files")
async def test_an_unknown_profile_is_a_user_facing_error() -> None:
    with pytest.raises(BedrockTokenError, match="no-such-profile"):
        await mint_bedrock_token("no-such-profile", "us-east-1")


async def test_no_credentials_at_all_is_a_user_facing_error(
    aws_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (aws_files / "credentials").write_text("", encoding="utf-8")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with pytest.raises(BedrockTokenError, match="No AWS credentials"):
        await mint_bedrock_token("", "us-east-1")


@pytest.mark.usefixtures("aws_files")
async def test_a_helper_that_never_answers_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodo.llms.bedrock._token_mint._MINT_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(BedrockTokenError, match="Timed out"):
        await mint_bedrock_token("work", "us-east-1")
