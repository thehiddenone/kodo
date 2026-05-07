"""Behavior tests for kodo.state._transient.

Tests verify that records are written to disk, sessions are created with
unique IDs, and metadata is updated correctly.
"""

import json
from pathlib import Path

import pytest

from kodo.state._transient import TransientStore


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
