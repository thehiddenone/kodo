"""Behaviour tests for kodo.state._transient.

Verifies that sessions are created under .kodo/sessions/, transient.json and
meta.json are written correctly, state updates are persisted in place, resumed
sessions load prior state, and agent/message logs are appended properly.
"""

import json
from pathlib import Path

import pytest

from kodo.state import TransientStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def kodo_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".kodo"
    d.mkdir()
    return d


@pytest.fixture()
def store(kodo_dir: Path) -> TransientStore:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    return s


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


def test_transient_json_written_on_new_session(store: TransientStore) -> None:
    transient_file = store.session_dir / "transient.json"
    assert transient_file.exists()
    data = json.loads(transient_file.read_text(encoding="utf-8"))
    assert data["stage"] == "IDLE"
    assert data["autonomous"] is False
    assert data["last_prompt"] == ""


def test_meta_json_written_on_new_session(store: TransientStore) -> None:
    meta_file = store.session_dir / "meta.json"
    assert meta_file.exists()
    data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert data["session_name"] == "Unnamed Session"
    assert "created_at" in data


def test_new_session_is_unnamed(store: TransientStore) -> None:
    assert store.session_name == "Unnamed Session"
    assert store.is_session_named is False


def test_set_session_name_persists_to_meta(store: TransientStore) -> None:
    store.set_session_name("Library Inventory API")
    assert store.session_name == "Library Inventory API"
    assert store.is_session_named is True
    data = json.loads((store.session_dir / "meta.json").read_text(encoding="utf-8"))
    assert data["session_name"] == "Library Inventory API"
    assert "created_at" in data  # created_at preserved across the rewrite


def test_resumed_session_loads_existing_name(kodo_dir: Path) -> None:
    session_id = "1748792500"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.set_session_name("Search Pagination Fix")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.session_name == "Search Pagination Fix"
    assert second.is_session_named is True


def test_session_dir_layout_on_new_session(store: TransientStore) -> None:
    assert store.session_dir.is_dir()
    assert (store.session_dir / "agents").is_dir()


def test_session_id_matches_attach_argument(store: TransientStore) -> None:
    assert store.session_id == "1748792400"


def test_session_dir_is_under_kodo_sessions(store: TransientStore, kodo_dir: Path) -> None:
    assert store.session_dir == kodo_dir / "sessions" / "1748792400"


# ---------------------------------------------------------------------------
# State properties and update
# ---------------------------------------------------------------------------


def test_initial_stage_is_idle(store: TransientStore) -> None:
    assert store.stage == "IDLE"


def test_initial_last_prompt_is_empty(store: TransientStore) -> None:
    assert store.last_prompt == ""


def test_initial_autonomous_is_false(store: TransientStore) -> None:
    assert store.autonomous is False


def test_stage_update_is_persisted(store: TransientStore) -> None:
    store.update(stage="NARRATIVE")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["stage"] == "NARRATIVE"


def test_prompt_update_is_persisted(store: TransientStore) -> None:
    store.update(prompt="Build a trading bot.")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["last_prompt"] == "Build a trading bot."


def test_autonomous_update_is_persisted(store: TransientStore) -> None:
    store.update(autonomous=True)
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["autonomous"] is True


def test_update_with_no_kwargs_does_not_raise(store: TransientStore) -> None:
    store.update()
    assert store.stage == "IDLE"


def test_stage_property_reflects_update(store: TransientStore) -> None:
    store.update(stage="RUNNING")
    assert store.stage == "RUNNING"


def test_prompt_property_reflects_update(store: TransientStore) -> None:
    store.update(prompt="hello")
    assert store.last_prompt == "hello"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resumed_session_loads_transient_state(kodo_dir: Path) -> None:
    session_id = "1748792401"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.update(stage="NARRATIVE", prompt="Build me something.", autonomous=True)

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)

    assert second.stage == "NARRATIVE"
    assert second.last_prompt == "Build me something."
    assert second.autonomous is True


def test_resumed_session_does_not_overwrite_meta(kodo_dir: Path) -> None:
    session_id = "1748792402"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)

    meta = json.loads((second.session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_name"] == "Unnamed Session"


def test_resumed_missing_transient_json_uses_defaults(kodo_dir: Path) -> None:
    session_id = "1748792403"
    session_dir = kodo_dir / "sessions" / session_id
    session_dir.mkdir(parents=True)

    store = TransientStore(kodo_dir)
    store.attach_session(session_id, resumed=True)

    assert store.stage == "IDLE"
    assert store.last_prompt == ""
    assert store.autonomous is False


# ---------------------------------------------------------------------------
# Message log (session.jsonl)
# ---------------------------------------------------------------------------


def test_append_and_read_messages(store: TransientStore) -> None:
    store.append_message("user", "Hello there.")
    store.append_message("assistant", "Hi! How can I help?")
    messages = store.read_messages()
    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "Hello there."}
    assert messages[1] == {"role": "assistant", "content": "Hi! How can I help?"}


def test_read_messages_returns_empty_when_no_log(store: TransientStore) -> None:
    assert store.read_messages() == []


def test_append_message_with_content_blocks(store: TransientStore) -> None:
    blocks = [{"type": "text", "text": "result"}, {"type": "tool_use", "id": "t1"}]
    store.append_message("assistant", blocks)
    messages = store.read_messages()
    assert messages[0]["content"] == blocks


def test_session_log_path_is_inside_session_dir(store: TransientStore) -> None:
    assert store.session_log_path == store.session_dir / "session.jsonl"


# ---------------------------------------------------------------------------
# Agent JSONL logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_jsonl_is_written(store: TransientStore) -> None:
    await store.write_agent_record("orchestrator", {"prompt": "hello", "tokens": 10})
    jsonl = store.session_dir / "agents" / "orchestrator.jsonl"
    assert jsonl.exists()
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["prompt"] == "hello"


@pytest.mark.asyncio
async def test_multiple_records_append_to_same_agent_file(store: TransientStore) -> None:
    for i in range(1, 4):
        await store.write_agent_record("orchestrator", {"n": i})
    jsonl = store.session_dir / "agents" / "orchestrator.jsonl"
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert [r["n"] for r in records] == [1, 2, 3]


@pytest.mark.asyncio
async def test_different_agents_get_separate_files(store: TransientStore) -> None:
    await store.write_agent_record("narrative_author", {"x": 1})
    await store.write_agent_record("architect", {"x": 2})
    assert (store.session_dir / "agents" / "narrative_author.jsonl").exists()
    assert (store.session_dir / "agents" / "architect.jsonl").exists()
