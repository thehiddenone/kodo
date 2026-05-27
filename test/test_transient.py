"""Behavior tests for kodo.state._transient.

Tests verify that records are written to disk, sessions are created with
unique IDs, and metadata is updated correctly.
"""

import json
from pathlib import Path

import pytest

from kodo.state._transient import TransientStore, find_unfinished_session, load_session_prompt


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TransientStore:
    """Return a TransientStore whose session dir lives in tmp_path."""
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    return TransientStore(tmp_path / "project")


@pytest.mark.asyncio
async def test_session_json_is_written_on_creation(store: TransientStore) -> None:
    session_json = store.session_dir / "session.json"
    assert session_json.exists(), "session.json must be written at init"
    data = json.loads(session_json.read_text(encoding="utf-8"))
    assert data["session_id"] == store.session_id
    assert data["last_stage"] == "IDLE"
    assert data["autonomous"] is False


@pytest.mark.asyncio
async def test_agent_jsonl_is_written(store: TransientStore) -> None:
    await store.write_agent_record("raw", {"prompt": "hello", "tokens": 10})
    jsonl = store.session_dir / "agents" / "raw.jsonl"
    assert jsonl.exists(), "agents/raw.jsonl must be created"
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["prompt"] == "hello"
    assert records[0]["tokens"] == 10


@pytest.mark.asyncio
async def test_multiple_records_append_to_same_file(store: TransientStore) -> None:
    await store.write_agent_record("raw", {"n": 1})
    await store.write_agent_record("raw", {"n": 2})
    await store.write_agent_record("raw", {"n": 3})
    jsonl = store.session_dir / "agents" / "raw.jsonl"
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert [r["n"] for r in records] == [1, 2, 3]


@pytest.mark.asyncio
async def test_different_agents_get_separate_files(store: TransientStore) -> None:
    await store.write_agent_record("narrative_author", {"x": 1})
    await store.write_agent_record("architect", {"x": 2})
    assert (store.session_dir / "agents" / "narrative_author.jsonl").exists()
    assert (store.session_dir / "agents" / "architect.jsonl").exists()


@pytest.mark.asyncio
async def test_mcp_records_are_written_separately(store: TransientStore) -> None:
    await store.write_mcp_record("tools/fileio", {"op": "read"})
    # Slashes and dots in tool names are sanitised to underscores
    mcp_file = store.session_dir / "mcp" / "tools_fileio.jsonl"
    assert mcp_file.exists()
    record = json.loads(mcp_file.read_text(encoding="utf-8").strip())
    assert record["op"] == "read"


def test_meta_stage_update_is_persisted(store: TransientStore) -> None:
    store.meta.update(stage="NARRATIVE")
    data = json.loads((store.session_dir / "session.json").read_text(encoding="utf-8"))
    assert data["last_stage"] == "NARRATIVE"


def test_meta_autonomous_update_is_persisted(store: TransientStore) -> None:
    store.meta.update(autonomous=True)
    data = json.loads((store.session_dir / "session.json").read_text(encoding="utf-8"))
    assert data["autonomous"] is True


def test_two_stores_for_same_project_get_different_session_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    project = tmp_path / "project"

    store1 = TransientStore(project)
    time.sleep(1.05)  # session IDs are second-resolution timestamps
    store2 = TransientStore(project)
    assert store1.session_id != store2.session_id


# ---------------------------------------------------------------------------
# SessionMeta property access
# ---------------------------------------------------------------------------


def test_meta_session_id_readable(store: TransientStore) -> None:
    """
    Given a TransientStore,
    when meta.session_id is read,
    then it equals the store's session_id.
    """
    assert store.meta.session_id == store.session_id


def test_meta_project_hash_is_twelve_chars(store: TransientStore) -> None:
    """
    Given a TransientStore,
    when meta.project_hash is read,
    then it is a 12-character hex string.
    """
    h = store.meta.project_hash
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)


def test_meta_last_stage_initial_value(store: TransientStore) -> None:
    """
    Given a new TransientStore,
    when meta.last_stage is read,
    then it equals 'IDLE'.
    """
    assert store.meta.last_stage == "IDLE"


def test_meta_last_prompt_initial_value(store: TransientStore) -> None:
    """
    Given a new TransientStore,
    when meta.last_prompt is read,
    then it is an empty string.
    """
    assert store.meta.last_prompt == ""


def test_meta_autonomous_initial_value(store: TransientStore) -> None:
    """
    Given a new TransientStore,
    when meta.autonomous is read,
    then it is False.
    """
    assert store.meta.autonomous is False


def test_meta_dev_proxy_rules_initial_value(store: TransientStore) -> None:
    """
    Given a new TransientStore,
    when meta.dev_proxy_rules is read,
    then it is an empty string.
    """
    assert store.meta.dev_proxy_rules == ""


def test_meta_prompt_update_is_persisted(store: TransientStore) -> None:
    """
    Given a TransientStore,
    when meta.update(prompt=...) is called,
    then the last_prompt property reflects the new value.
    """
    store.meta.update(prompt="Build a trading bot.")
    assert store.meta.last_prompt == "Build a trading bot."


def test_meta_dev_proxy_rules_update_is_persisted(store: TransientStore) -> None:
    """
    Given a TransientStore,
    when meta.update(dev_proxy_rules=...) is called,
    then the dev_proxy_rules property reflects the new value.
    """
    store.meta.update(dev_proxy_rules="allow: approve\ndeny: stop")
    assert store.meta.dev_proxy_rules == "allow: approve\ndeny: stop"


def test_meta_update_with_no_kwargs_does_not_raise(store: TransientStore) -> None:
    """
    Given a TransientStore,
    when meta.update() is called with no arguments,
    then no exception is raised and values remain unchanged.
    """
    store.meta.update()
    assert store.meta.last_stage == "IDLE"


# ---------------------------------------------------------------------------
# load_session_prompt
# ---------------------------------------------------------------------------


def test_load_session_prompt_returns_stored_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a session that stored a prompt,
    when load_session_prompt is called with the session_id,
    then the stored prompt is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    project = tmp_path / "project"

    store = TransientStore(project)
    store.meta.update(prompt="Build a price-alert bot.")

    result = load_session_prompt(project, store.session_id)
    assert result == "Build a price-alert bot."


def test_load_session_prompt_returns_empty_for_unknown_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given no session matching the requested session_id,
    when load_session_prompt is called,
    then an empty string is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    result = load_session_prompt(tmp_path / "project", "nonexistent-session")
    assert result == ""


def test_load_session_prompt_returns_empty_when_no_sessions_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a project with no transient directory,
    when load_session_prompt is called,
    then an empty string is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    result = load_session_prompt(tmp_path / "new_project", "any-session")
    assert result == ""


# ---------------------------------------------------------------------------
# find_unfinished_session
# ---------------------------------------------------------------------------


def test_find_unfinished_session_returns_none_when_no_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a project with no transient directory,
    when find_unfinished_session is called,
    then None is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    result = find_unfinished_session(tmp_path / "project")
    assert result is None


def test_find_unfinished_session_returns_session_id_for_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a session in a non-terminal stage with a prompt,
    when find_unfinished_session is called,
    then the session_id is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    project = tmp_path / "project"

    store = TransientStore(project)
    store.meta.update(stage="NARRATIVE", prompt="Build something.")

    result = find_unfinished_session(project)
    assert result == store.session_id


def test_find_unfinished_session_returns_none_for_completed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a session in a terminal stage ('DONE'),
    when find_unfinished_session is called,
    then None is returned.
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    project = tmp_path / "project"

    store = TransientStore(project)
    store.meta.update(stage="DONE", prompt="Build something.")

    result = find_unfinished_session(project)
    assert result is None


def test_find_unfinished_session_returns_none_when_no_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Given a session in a non-terminal stage but with no stored prompt,
    when find_unfinished_session is called,
    then None is returned (can't resume without knowing the original prompt).
    """
    import kodo.state._transient as mod

    monkeypatch.setattr(mod, "_TRANSIENT_BASE", tmp_path / "transient")
    project = tmp_path / "project"

    store = TransientStore(project)
    store.meta.update(stage="NARRATIVE")  # no prompt

    result = find_unfinished_session(project)
    assert result is None
