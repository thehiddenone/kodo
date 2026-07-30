"""Behavior tests for :mod:`kodo.llms.__main__` — the ``python -m kodo.llms`` CLI.

Nothing is stubbed out except ``kodo_user_dir``: the CLI is driven through its
real ``main(argv)`` against the **packaged** agent files and the **live** local
and cloud registries, so these tests fail if a shipped agent stops rendering or
a registry lookup regresses. Redirecting ``kodo_user_dir`` at a ``tmp_path``
keeps the local-registry half hermetic — the developer's own
``~/.kodo/etc/local-llm-registry.json`` must not decide which ``LLM_ID``\\ s
resolve here.

Model ids and agent names are read off the live registries rather than
hardcoded (see the repo's spec-driven-tests rule); the few structural
assumptions that cannot be derived are pinned by a loud ``assert`` in
:func:`_pin_assumptions`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import kodo.llms.__main__ as llms_main
import kodo.subagents as subagents_pkg
from kodo.llms import LocalLLMEntry, add_local_entry, get_cloud_registry, get_local_registry
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
    monkeypatch.setattr(llms_main, "kodo_user_dir", lambda: home)
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


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """Invoke ``main(argv)`` and return ``(exit_code, stdout, stderr)``."""
    code = llms_main.main(list(argv))
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
    assert llms_main._AGENTS_DIR == _REAL_AGENTS_DIR


# ---------------------------------------------------------------------------
# --system-prompt: the happy path
# ---------------------------------------------------------------------------


def test_prints_the_rendered_prompt_for_a_local_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--system-prompt", _local_id(kodo_home), _PINNED_AGENT)
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


def test_prints_the_rendered_prompt_for_a_cloud_model_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(capsys, "--system-prompt", _cloud_id(), _PINNED_AGENT)
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


@pytest.mark.parametrize("agent_name", _agent_names())
def test_every_packaged_agent_renders(
    agent_name: str, kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newly added agent whose prompt fails to render must fail here."""
    code, out, _ = _run(capsys, "--system-prompt", _cloud_id(), agent_name)
    assert code == 0
    assert out == f"{_rendered(agent_name)}\n"


def test_local_and_cloud_ids_render_the_same_prompt(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Today's invariant: no plugin appends anything model-specific.

    ``LLM_ID`` is validated but does not change a byte of the output. Per-LLM
    prompt variation is planned (see the module docstring) — when it lands, this
    test is the one that must be *replaced* with per-model expectations, not
    quietly deleted.
    """
    _, local_out, _ = _run(capsys, "--system-prompt", _local_id(kodo_home), _PINNED_AGENT)
    _, cloud_out, _ = _run(capsys, "--system-prompt", _cloud_id(), _PINNED_AGENT)
    assert local_out == cloud_out


def test_output_is_the_fully_rendered_prompt_not_a_raw_file(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preambles prepended, every placeholder substituted."""
    _, out, _ = _run(capsys, "--system-prompt", _cloud_id(), _PINNED_AGENT)
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
    _, out, _ = _run(capsys, "--system-prompt", _cloud_id(), agent_name)
    assert "## Tools" not in out


def test_a_user_added_custom_entry_resolves(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``LLM_ID`` goes through the live registry, not a hardcoded name list."""
    add_local_entry(
        kodo_home,
        LocalLLMEntry(
            name="my-own-box",
            kind="custom_server_url",
            description="An externally managed llama-server.",
            url="http://192.168.1.50:8042",
        ),
    )
    code, out, err = _run(capsys, "--system-prompt", "my-own-box", _PINNED_AGENT)
    assert code == 0
    assert err == ""
    assert out == f"{_rendered(_PINNED_AGENT)}\n"


# ---------------------------------------------------------------------------
# --system-prompt: resolution errors
# ---------------------------------------------------------------------------


def test_unknown_llm_id_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--system-prompt", "no-such-model", _PINNED_AGENT)
    assert code == 2
    assert out == ""
    assert "no-such-model" in err


def test_unknown_agent_exits_2(kodo_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "--system-prompt", _cloud_id(), "no-such-agent")
    assert code == 2
    assert out == ""
    assert "no-such-agent" in err


def test_the_agent_is_resolved_before_the_llm_id(
    kodo_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both wrong reports the agent — the check order the CLI documents."""
    code, _, err = _run(capsys, "--system-prompt", "no-such-model", "no-such-agent")
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
        pytest.param(["--system-prompt"], id="no-values"),
        pytest.param(["--system-prompt", "claude-opus-5"], id="one-value"),
        pytest.param(["--bogus", "a", "b"], id="unknown-flag"),
    ],
)
def test_bad_invocation_exits_via_argparse(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        llms_main.main(argv)
    assert excinfo.value.code == 2


def test_parse_args_splits_the_pair_into_llm_id_and_agent() -> None:
    parsed = llms_main._parse_args(["--system-prompt", "some-model", "some-agent"])
    assert parsed.llm_id == "some-model"
    assert parsed.agent == "some-agent"


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
        f"{shlex.quote(sys.executable)} -m kodo.llms "
        f"--system-prompt {shlex.quote(_cloud_id())} {_PINNED_AGENT} | head -1",
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
            assert llms_main._find_cloud_entry(entry.model_id) is entry


def test_find_cloud_entry_returns_none_for_an_unknown_model_id() -> None:
    assert llms_main._find_cloud_entry("definitely-not-a-model") is None


def test_find_cloud_entry_does_not_match_on_the_display_name() -> None:
    """Lookup is by ``model_id`` — a vendor key or display name must not match."""
    for vendor, models in get_cloud_registry().items():
        assert llms_main._find_cloud_entry(vendor) is None
        for entry in models:
            if entry.name != entry.model_id:
                assert llms_main._find_cloud_entry(entry.name) is None
