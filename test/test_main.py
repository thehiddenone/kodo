"""Behavior tests for :mod:`kodo.__main__` — the ``python -m kodo`` CLI.

Nothing is stubbed out except ``kodo_user_dir``: the CLI is driven through its
real ``main(argv)`` against the **packaged** agent files and the **live** local
and cloud registries, so these tests fail if a shipped agent stops rendering, a
registry lookup regresses, or the OpenAI tools payload drifts from what
:class:`~kodo.llms.llamacpp.LlamaPlugin` actually sends. Redirecting
``kodo_user_dir`` at a ``tmp_path`` keeps the local-registry half hermetic —
the developer's own ``~/.kodo/etc/local-llm-registry.json`` must not decide
which ``LLM_ID``\\ s resolve here.

Model ids and agent names are read off the live registries rather than
hardcoded (see the repo's spec-driven-tests rule); the few structural
assumptions that cannot be derived are pinned by a loud ``assert`` in
:func:`_pin_assumptions`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import kodo.__main__ as main_mod
import kodo.subagents as subagents_pkg
from kodo.llms import LocalLLMEntry, add_local_entry, get_cloud_registry, get_local_registry
from kodo.llms.llamacpp import build_openai_tools
from kodo.runtime import agent_tool_specs
from kodo.subagents import AgentRegistry

_REAL_AGENTS_DIR = Path(subagents_pkg.__file__).parent

# The agent used wherever one concrete name is clearer than a parametrized sweep.
_PINNED_AGENT = "guide"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kodo_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's ``kodo_user_dir()`` at an empty ``tmp_path``."""
    home = tmp_path / "kodo-home"
    home.mkdir()
    monkeypatch.setattr(main_mod, "kodo_user_dir", lambda: home)
    return home


def _local_id(kodo_home: Path) -> str:
    """A real local-registry name, read off the live registry."""
    return sorted(get_local_registry(kodo_home))[0]


def _cloud_id() -> str:
    """A real cloud ``model_id``, read off the live registry."""
    return sorted(e.model_id for models in get_cloud_registry().values() for e in models)[0]


def _agent_names() -> list[str]:
    """Every packaged agent's frontmatter name, in sorted order."""
    return sorted(a.name for a in AgentRegistry(_REAL_AGENTS_DIR).all_agents())


def _rendered(agent_name: str) -> str:
    """The prompt the CLI must reproduce, straight from the real registry."""
    return AgentRegistry(_REAL_AGENTS_DIR).get(agent_name, autonomous=False).system_prompt


def _oai_tools(agent_name: str) -> list[dict[str, object]]:
    """The tools payload the CLI must reproduce, straight from production plumbing.

    Goes through ``agent_tool_specs`` — the same join the engine uses — so the
    per-agent expansions (one ``run_subagent_<name>`` per invocable sub-agent, a
    ``return_result`` bound to this agent's output schema) are covered too, not
    just the static catalog lookups.
    """
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    agent = registry.get(agent_name, autonomous=False)
    return build_openai_tools(agent_tool_specs(registry, agent))


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Invoke ``main(argv)`` and return ``(exit_code, stdout, stderr)``."""
    code = main_mod.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _pin_assumptions() -> None:
    """Fail loudly if this module's structural assumptions stop holding."""
    assert get_cloud_registry(), "cloud registry is empty — _cloud_id() has nothing to return"
    assert _PINNED_AGENT in _agent_names(), (
        f"agent {_PINNED_AGENT!r} no longer ships; pick another pinned agent"
    )


def test_module_targets_the_packaged_agents() -> None:
    """The CLI must read the installed agent files, not a copy or a fixture."""
    _pin_assumptions()
    assert main_mod._AGENTS_DIR == _REAL_AGENTS_DIR


# ---------------------------------------------------------------------------
# --system-prompt: the happy path
# ---------------------------------------------------------------------------


def test_prints_the_rendered_prompt_for_a_local_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", _local_id(kodo_home))
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


def test_prints_the_rendered_prompt_for_a_cloud_model_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "-p", _PINNED_AGENT, "-m", _cloud_id())
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


@pytest.mark.parametrize("agent_name", _agent_names())
def test_every_packaged_agent_renders(
    agent_name: str, kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newly added agent whose prompt fails to render must fail here."""
    code, out, _ = _run(capsys, "--system-prompt", agent_name, "--model", _cloud_id())
    assert code == 0
    assert out == f"{_rendered(agent_name)}\n"


def test_local_and_cloud_ids_render_the_same_prompt(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Today's invariant: no plugin appends anything model-specific.

    ``--model`` is validated but does not change a byte of the output. Per-LLM
    prompt variation is planned (see the module docstring) — when it lands, this
    test is the one that must be *replaced* with per-model expectations, not
    quietly deleted.
    """
    local_id = _local_id(kodo_home)
    _, local_out, _ = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", local_id)
    _, cloud_out, _ = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", _cloud_id())
    assert local_out == cloud_out


def test_output_is_the_fully_rendered_prompt_not_a_raw_file(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preambles prepended, every placeholder substituted."""
    _, out, _ = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", _cloud_id())
    security = (_REAL_AGENTS_DIR / "preamble_security.md").read_text(encoding="utf-8")
    performance = (_REAL_AGENTS_DIR / "preamble_performance.md").read_text(encoding="utf-8")
    assert security in out
    assert performance in out
    assert "{PLACEHOLDER" not in out


@pytest.mark.parametrize("agent_name", _agent_names())
def test_prompt_never_describes_the_agents_tools(
    agent_name: str, kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tools are described only via the LLM ``tools`` argument, never the prompt.

    Guards the ``## Tools`` prompt section from being reintroduced — the CLI is
    the cheapest place to notice if it comes back (see doc/TOOLS.md §7).
    """
    _, out, _ = _run(capsys, "--system-prompt", agent_name, "--model", _cloud_id())
    assert "## Tools" not in out


def test_a_user_added_custom_entry_resolves(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--model`` goes through the live registry, not a hardcoded name list."""
    add_local_entry(
        kodo_home,
        LocalLLMEntry(
            name="my-own-box",
            kind="custom_server_url",
            description="An externally managed llama-server.",
            url="http://192.168.1.50:8042",
        ),
    )
    code, out, err = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", "my-own-box")
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


def test_a_user_added_custom_server_url_entry_is_the_default_model(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--model`` omitted falls back to the first installed local entry.

    A freshly created ``kodo_home`` has no hardcoded GGUF installed (nothing
    was ever downloaded into it), so a ``custom_server_url`` entry — always
    considered installed — is the only thing that can satisfy the default
    here without a real download.
    """
    add_local_entry(
        kodo_home,
        LocalLLMEntry(
            name="my-own-box",
            kind="custom_server_url",
            description="An externally managed llama-server.",
            url="http://192.168.1.50:8042",
        ),
    )
    code, out, err = _run(capsys, "--system-prompt", _PINNED_AGENT)
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


def test_no_model_given_and_none_installed_exits_2(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pristine local registry (nothing downloaded, no custom entries) has no default."""
    code, out, err = _run(capsys, "--system-prompt", _PINNED_AGENT)
    assert code == 2
    assert out == ""
    assert "--model" in err


# ---------------------------------------------------------------------------
# --system-prompt: resolution errors
# ---------------------------------------------------------------------------


def test_unknown_llm_id_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--system-prompt", _PINNED_AGENT, "--model", "no-such-model")
    assert code == 2
    assert out == ""
    assert "no-such-model" in err


def test_unknown_agent_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--system-prompt", "no-such-agent", "--model", _cloud_id())
    assert code == 2
    assert out == ""
    assert "no-such-agent" in err


def test_the_agent_is_resolved_before_the_llm_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both wrong reports the agent — the check order the CLI documents."""
    code, _, err = _run(capsys, "--system-prompt", "no-such-agent", "--model", "no-such-model")
    assert code == 2
    assert "no-such-agent" in err
    assert "no-such-model" not in err


def test_the_agent_is_resolved_before_the_missing_default_model(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown agent plus no ``--model`` (and nothing installed) still reports the agent."""
    code, _, err = _run(capsys, "--system-prompt", "no-such-agent")
    assert code == 2
    assert "no-such-agent" in err


# ---------------------------------------------------------------------------
# --tools: the happy path
# ---------------------------------------------------------------------------


def test_tools_prints_the_openai_payload_for_a_local_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--tools", _PINNED_AGENT, "--model", _local_id(kodo_home))
    assert code == 0
    assert err == ""
    assert json.loads(out) == _oai_tools(_PINNED_AGENT)


def test_tools_prints_the_openai_payload_for_a_cloud_model_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--tools", _PINNED_AGENT, "--model", _cloud_id())
    assert code == 0
    assert err == ""
    assert json.loads(out) == _oai_tools(_PINNED_AGENT)


@pytest.mark.parametrize("agent_name", _agent_names())
def test_tools_every_packaged_agent_renders(
    agent_name: str, kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newly added agent whose tools fail to resolve must fail here."""
    code, out, _ = _run(capsys, "--tools", agent_name, "--model", _cloud_id())
    assert code == 0
    assert json.loads(out) == _oai_tools(agent_name)


def test_tools_payload_uses_the_openai_function_wrapper() -> None:
    """Every entry must carry the wire shape the OpenAI client expects."""
    for entry in _oai_tools(_PINNED_AGENT):
        assert entry["type"] == "function"
        function = entry["function"]
        assert isinstance(function, dict)
        assert set(function) == {"name", "description", "parameters"}


def test_tools_local_and_cloud_ids_render_the_same_payload(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Today's invariant: the OpenAI tools shape doesn't vary by ``--model``.

    ``--model`` is validated but does not change a byte of the output — see
    the module docstring for why the argument is kept anyway.
    """
    _, local_out, _ = _run(capsys, "--tools", _PINNED_AGENT, "--model", _local_id(kodo_home))
    _, cloud_out, _ = _run(capsys, "--tools", _PINNED_AGENT, "--model", _cloud_id())
    assert local_out == cloud_out


# ---------------------------------------------------------------------------
# --tools: resolution errors
# ---------------------------------------------------------------------------


def test_tools_unknown_llm_id_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--tools", _PINNED_AGENT, "--model", "no-such-model")
    assert code == 2
    assert out == ""
    assert "no-such-model" in err


def test_tools_unknown_agent_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--tools", "no-such-agent", "--model", _cloud_id())
    assert code == 2
    assert out == ""
    assert "no-such-agent" in err


def test_tools_the_agent_is_resolved_before_the_llm_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both wrong reports the agent — the check order the CLI documents."""
    code, _, err = _run(capsys, "--tools", "no-such-agent", "--model", "no-such-model")
    assert code == 2
    assert "no-such-agent" in err
    assert "no-such-model" not in err


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-flag"),
        pytest.param(["--system-prompt"], id="no-value"),
        pytest.param(["-p"], id="no-value-short"),
        pytest.param(["--tools"], id="tools-no-value"),
        pytest.param(
            ["--system-prompt", "guide", "--tools", "guide"],
            id="both-flags",
        ),
        pytest.param(["--bogus", "a", "b"], id="unknown-flag"),
        pytest.param(["--model", "claude-opus-5"], id="model-without-a-command"),
        pytest.param(["--system-prompt", "guide", "--model"], id="model-no-value"),
    ],
)
def test_bad_invocation_exits_via_argparse(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(argv)
    assert excinfo.value.code == 2


def test_parse_args_captures_the_agent_and_model() -> None:
    parsed = main_mod._parse_args(["--system-prompt", "some-agent", "--model", "some-model"])
    assert parsed.agent == "some-agent"
    assert parsed.model == "some-model"


def test_parse_args_accepts_the_short_flags() -> None:
    parsed = main_mod._parse_args(["-p", "some-agent", "-m", "some-model"])
    assert parsed.agent == "some-agent"
    assert parsed.model == "some-model"


def test_parse_args_tools_captures_the_agent_and_model() -> None:
    parsed = main_mod._parse_args(["--tools", "some-agent", "--model", "some-model"])
    assert parsed.agent == "some-agent"
    assert parsed.model == "some-model"


def test_parse_args_model_defaults_to_none() -> None:
    parsed = main_mod._parse_args(["--system-prompt", "some-agent"])
    assert parsed.model is None


# ---------------------------------------------------------------------------
# Piping
# ---------------------------------------------------------------------------


def test_piping_into_head_does_not_print_a_traceback(tmp_path: Path) -> None:
    """A closed pipe is normal usage for a ~100 KB dump, not a crash.

    Run as a real subprocess: the failure this guards against (the interpreter's
    shutdown flush raising ``BrokenPipeError`` after ``main`` returns) only
    happens in a genuine process teardown, so in-process mocking cannot see it.
    """
    src_root = Path(subagents_pkg.__file__).parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(src_root),
        # Hermetic: the local registry must not read the developer's own ~/.kodo.
        "HOME": str(tmp_path),
    }
    head = subprocess.run(
        f"{shlex.quote(sys.executable)} -m kodo --system-prompt {shlex.quote(_PINNED_AGENT)} "
        f"--model {shlex.quote(_cloud_id())} | head -1",
        shell=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert "Traceback" not in head.stderr
    assert "BrokenPipeError" not in head.stderr
    assert head.stderr == ""
    assert head.stdout.strip() != ""


# ---------------------------------------------------------------------------
# _find_cloud_entry
# ---------------------------------------------------------------------------


def test_find_cloud_entry_searches_across_vendors() -> None:
    """There is no flat cloud key, so every vendor's tuple must be scanned."""
    for models in get_cloud_registry().values():
        for entry in models:
            assert main_mod._find_cloud_entry(entry.model_id) is entry


def test_find_cloud_entry_returns_none_for_an_unknown_model_id() -> None:
    assert main_mod._find_cloud_entry("definitely-not-a-model") is None


def test_find_cloud_entry_does_not_match_on_the_display_name() -> None:
    """Lookup is by ``model_id`` — a vendor key or display name must not match."""
    for vendor, models in get_cloud_registry().items():
        assert main_mod._find_cloud_entry(vendor) is None
        for entry in models:
            if entry.name != entry.model_id:
                assert main_mod._find_cloud_entry(entry.name) is None
