"""``kodo-headless`` with a cloud model (doc/HEADLESS.md §2): the ``--model``
spelling, the settings the isolated home selects, where credentials come
from, and the startup failures a cloud run reports before spawning anything.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kodo.headless import (
    EventSink,
    HeadlessOptions,
    HeadlessRun,
    ModelSpec,
    RunOutcome,
    bedrock_region,
    build_headless_home,
    resolve_vendor_api_key,
    vendor_credential_env_names,
)

# ---------------------------------------------------------------------------
# --model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "vendor", "name", "label"),
    [
        (
            "unsloth-qwen36-27b-q4-k-xl",
            None,
            "unsloth-qwen36-27b-q4-k-xl",
            "unsloth-qwen36-27b-q4-k-xl",
        ),
        ("local/odd/entry", None, "odd/entry", "odd/entry"),
        ("anthropic/claude-sonnet-5", "anthropic", "claude-sonnet-5", "anthropic/claude-sonnet-5"),
        (
            "OpenRouter/qwen/qwen3-coder",
            "openrouter",
            "qwen/qwen3-coder",
            "openrouter/qwen/qwen3-coder",
        ),
    ],
)
def test_model_spellings(text: str, vendor: str | None, name: str, label: str) -> None:
    spec = ModelSpec.parse(text)
    assert (spec.vendor, spec.name, spec.label, spec.is_cloud) == (
        vendor,
        name,
        label,
        vendor is not None,
    )


@pytest.mark.parametrize("text", ["", "  ", "anthropic/", "local/", "1bad/model"])
def test_malformed_model_spellings_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        ModelSpec.parse(text)


# ---------------------------------------------------------------------------
# The isolated home selects the cloud model
# ---------------------------------------------------------------------------


def _settings(kodo: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (kodo / "etc" / "settings.json").read_text(encoding="utf-8")
    )
    return loaded


def test_cloud_model_is_pinned_across_every_effort_tier(tmp_path: Path) -> None:
    template = tmp_path / "real" / ".kodo"
    (template / "etc").mkdir(parents=True)
    (template / "etc" / "settings.json").write_text(
        json.dumps(
            {
                "mode": "local",
                "models": {
                    "local": "m",
                    "cloud_uniform": {"openai": {"enabled": True, "model_id": "gpt-x"}},
                },
            }
        ),
        encoding="utf-8",
    )

    kodo = build_headless_home(
        tmp_path / "iso", model="anthropic/claude-sonnet-5", template_kodo_dir=template
    )

    settings = _settings(kodo)
    assert settings["mode"] == "cloud"
    assert settings["active_cloud_vendor"] == "anthropic"
    assert settings["models"] == {
        "local": "m",
        "cloud_uniform": {
            "openai": {"enabled": True, "model_id": "gpt-x"},
            "anthropic": {"enabled": True, "model_id": "claude-sonnet-5"},
        },
    }


def test_openrouter_turns_auto_mode_off_so_the_pin_holds(tmp_path: Path) -> None:
    kodo = build_headless_home(
        tmp_path / "iso", model="openrouter/qwen/qwen3-coder", template_kodo_dir=None
    )
    settings = _settings(kodo)
    assert settings["openrouter_auto_mode"] is False
    assert settings["models"] == {
        "cloud_uniform": {"openrouter": {"enabled": True, "model_id": "qwen/qwen3-coder"}}
    }


def test_bedrock_region_is_written_to_settings(tmp_path: Path) -> None:
    kodo = build_headless_home(
        tmp_path / "iso",
        model="bedrock/us.anthropic.claude-sonnet-4-6",
        template_kodo_dir=None,
        bedrock_region="eu-west-1",
    )
    assert _settings(kodo)["bedrock_region"] == "eu-west-1"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_vendor_key_is_read_from_its_own_name_first() -> None:
    env = {"GOOGLE_API_KEY": "own", "GEMINI_API_KEY": "alias"}
    assert resolve_vendor_api_key("google", env) == "own"
    assert resolve_vendor_api_key("google", {"GEMINI_API_KEY": "alias"}) == "alias"
    assert resolve_vendor_api_key("alibaba", {"DASHSCOPE_API_KEY": "d"}) == "d"
    assert resolve_vendor_api_key("anthropic", {"ANTHROPIC_API_KEY": "  "}) is None


def test_bedrock_key_is_built_from_the_standard_aws_pair() -> None:
    env = {
        "AWS_ACCESS_KEY_ID": "AKIA1",
        "AWS_SECRET_ACCESS_KEY": "s3cret",
        "AWS_REGION": "us-west-2",
    }
    key = resolve_vendor_api_key("bedrock", env)
    assert key is not None
    assert json.loads(key) == {"access_key_id": "AKIA1", "secret_access_key": "s3cret"}
    assert bedrock_region(env) == "us-west-2"
    assert resolve_vendor_api_key("bedrock", {"AWS_ACCESS_KEY_ID": "AKIA1"}) is None


def test_every_forwardable_name_resolves_the_key() -> None:
    for vendor in ("anthropic", "openai", "google", "alibaba", "kimi", "meta", "deepseek"):
        for name in vendor_credential_env_names(vendor):
            assert resolve_vendor_api_key(vendor, {name: "k"}) == "k", (vendor, name)


# ---------------------------------------------------------------------------
# The run refuses a cloud run it cannot start, before spawning anything
# ---------------------------------------------------------------------------


async def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, llama_url: str | None = None
) -> tuple[str, str, str]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    sandbox = tmp_path / "proj"
    sandbox.mkdir()
    buffer = io.StringIO()
    options = HeadlessOptions(prompt="do it", model=model, cwd=sandbox, llama_url=llama_url)
    result = await HeadlessRun(options, EventSink("jsonl", buffer)).run()
    kinds = " ".join(json.loads(line)["type"] for line in buffer.getvalue().splitlines()[:-1])
    return result.outcome, result.error or "", kinds


async def test_cloud_run_without_a_key_names_the_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in vendor_credential_env_names("anthropic"):
        monkeypatch.delenv(name, raising=False)

    outcome, error, kinds = await _run(tmp_path, monkeypatch, model="anthropic/claude-sonnet-5")

    assert outcome == RunOutcome.STARTUP_ERROR.value
    assert "ANTHROPIC_API_KEY" in error
    assert "llama.spawn" not in kinds


async def test_cloud_run_refuses_a_llama_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    outcome, error, _ = await _run(
        tmp_path, monkeypatch, model="anthropic/claude-sonnet-5", llama_url="http://h:1"
    )

    assert outcome == RunOutcome.STARTUP_ERROR.value
    assert "cloud model" in error
