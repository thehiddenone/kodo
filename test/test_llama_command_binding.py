"""A session binds to a llama-server command line (doc/SESSION_LLM_BINDING.md D2, D6).

``plan_llama_command`` builds the exact argv for an entry under a session's own
:class:`~kodo.llms.LaunchChoice`, and ``ensure_llama_running`` reuses the
running server only when its recorded argv is that same list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.llms import (
    LaunchChoice,
    LocalLLMEntry,
    add_local_entry,
    add_profile,
    get_launch_choice,
    get_local_registry,
)
from kodo.llms.llamacpp import ensure_llama_running, plan_llama_command

_MANAGER = "kodo.llms.llamacpp._manager"


class _FakeInstall:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.build = 99999


class _FakeServer:
    """Stands in for LlamaServer: no process, just the recorded argv."""

    active: _FakeServer | None = None
    started: list[_FakeServer] = []

    def __init__(self, config: object, llama_args: dict[str, str], *, profile_id: str = "") -> None:
        from kodo.llms.llamacpp import build_llama_command

        self.command = tuple(build_llama_command(config, llama_args))  # type: ignore[arg-type]
        self.is_running = False
        self.stops = 0
        self.model_name = getattr(config, "model_name", "")
        type(self).active = self

    @classmethod
    def get_active_llama_server(cls) -> _FakeServer | None:
        return cls.active

    async def start(self) -> None:
        self.is_running = True
        type(self).started.append(self)

    async def stop(self) -> None:
        self.stops += 1
        self.is_running = False


@pytest.fixture
def entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalLLMEntry:
    model = tmp_path / "model.gguf"
    model.write_text("gguf")
    monkeypatch.setattr(f"{_MANAGER}.find_installed", lambda _d: _FakeInstall(tmp_path / "srv"))
    _FakeServer.active = None
    _FakeServer.started = []
    monkeypatch.setattr(f"{_MANAGER}.LlamaServer", _FakeServer)
    add_local_entry(tmp_path, LocalLLMEntry(name="file-model", kind="custom_file", path=str(model)))
    return get_local_registry(tmp_path)["file-model"]


def test_a_sessions_profile_choice_changes_the_planned_command(
    tmp_path: Path, entry: LocalLLMEntry
) -> None:
    profile = add_profile(tmp_path, entry.name, "Tight", llama_args={"--ctx-size": "4096"})

    default = plan_llama_command(entry, tmp_path)
    tight = plan_llama_command(entry, tmp_path, LaunchChoice(profile_id=profile.id))

    assert default != tight
    assert tight[tight.index("--ctx-size") + 1] == "4096"
    assert str(entry.path) in tight


def test_a_removed_profile_falls_back_to_the_default_profile(
    tmp_path: Path, entry: LocalLLMEntry
) -> None:
    stale = plan_llama_command(entry, tmp_path, LaunchChoice(profile_id="gone"))

    assert stale == plan_llama_command(entry, tmp_path, LaunchChoice())


def test_no_choice_means_the_registrys_stored_choice(tmp_path: Path, entry: LocalLLMEntry) -> None:
    stored = get_launch_choice(tmp_path, entry)

    assert plan_llama_command(entry, tmp_path) == plan_llama_command(entry, tmp_path, stored)


async def test_a_server_already_running_the_planned_command_is_reused(
    tmp_path: Path, entry: LocalLLMEntry
) -> None:
    first = await ensure_llama_running(entry, tmp_path)

    again = await ensure_llama_running(entry, tmp_path, get_launch_choice(tmp_path, entry))

    assert again is first
    assert first.stops == 0
    assert len(_FakeServer.started) == 1


async def test_another_command_line_for_the_same_model_restarts_the_server(
    tmp_path: Path, entry: LocalLLMEntry
) -> None:
    profile = add_profile(tmp_path, entry.name, "Tight", llama_args={"--ctx-size": "4096"})
    first = await ensure_llama_running(entry, tmp_path)

    second = await ensure_llama_running(entry, tmp_path, LaunchChoice(profile_id=profile.id))

    assert second is not first
    assert first.stops == 1
    assert second.command == plan_llama_command(
        entry, tmp_path, LaunchChoice(profile_id=profile.id)
    )


async def test_a_server_with_no_recorded_command_line_is_restarted(
    tmp_path: Path, entry: LocalLLMEntry
) -> None:
    first = await ensure_llama_running(entry, tmp_path)
    first.command = ()  # adopted from a runtime file written before argv was recorded

    second = await ensure_llama_running(entry, tmp_path)

    assert second is not first
    assert first.stops == 1


def test_a_self_hosted_entry_has_no_command_line(tmp_path: Path) -> None:
    remote = LocalLLMEntry(name="remote", kind="custom_server_url", url="http://h:1")

    with pytest.raises(RuntimeError, match="custom_server_url"):
        plan_llama_command(remote, tmp_path)
