"""Behaviour tests for kodo.state._transient.

Verifies that sessions are created under .kodo/sessions/, transient.json and
meta.json are written correctly, state updates are persisted in place, resumed
sessions load prior state, and message logs are appended properly.
"""

import json
from datetime import datetime
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


def test_new_session_last_modified_equals_created_at(store: TransientStore) -> None:
    data = json.loads((store.session_dir / "meta.json").read_text(encoding="utf-8"))
    assert data["last_modified"] == data["created_at"]
    assert store.last_modified == store.created_at


def test_append_message_bumps_last_modified(store: TransientStore) -> None:
    before = store.last_modified
    store.append_message("user", "hello")
    after = store.last_modified
    assert after > before
    data = json.loads((store.session_dir / "meta.json").read_text(encoding="utf-8"))
    assert data["last_modified"] == after
    assert data["created_at"] == store.created_at  # created_at untouched


def test_subsession_and_toolcall_writes_bump_last_modified(store: TransientStore) -> None:
    t1 = store.last_modified
    store.append_subsession_message("sub-1", "assistant", "work")
    t2 = store.last_modified
    assert t2 > t1
    store.write_tool_call("tool-1", "# doc")
    t3 = store.last_modified
    assert t3 > t2


# ---------------------------------------------------------------------------
# Prompt attachments
# ---------------------------------------------------------------------------


def test_store_attachment_writes_copy_and_returns_link(store: TransientStore) -> None:
    result = store.store_attachment("notes.py", "print('hi')")
    assert result is not None
    attachment_id, rel = result
    assert rel.startswith("attachments/")
    assert rel == f"attachments/{attachment_id}__notes.py"
    copy = store.session_dir / rel
    assert copy.read_text(encoding="utf-8") == "print('hi')"


def test_store_attachment_unique_names_for_same_basename(store: TransientStore) -> None:
    a = store.store_attachment("dup.txt", "first")
    b = store.store_attachment("dup.txt", "second")
    assert a is not None and b is not None
    assert a[0] != b[0]
    assert a[1] != b[1]
    assert (store.session_dir / a[1]).read_text(encoding="utf-8") == "first"
    assert (store.session_dir / b[1]).read_text(encoding="utf-8") == "second"


def test_store_attachment_bumps_last_modified(store: TransientStore) -> None:
    before = store.last_modified
    store.store_attachment("a.txt", "x")
    assert store.last_modified > before


def test_append_message_persists_attachment_links_not_content(store: TransientStore) -> None:
    result = store.store_attachment("f.py", "secret-content")
    assert result is not None
    attachment_id, rel = result
    store.append_message(
        "user",
        "clean prompt",
        entry_agent="guide",
        attachments=[{"id": attachment_id, "name": "f.py", "stored": rel}],
    )
    lines = store.read_session_lines()
    assert len(lines) == 1
    record = lines[0]
    assert record["content"] == "clean prompt"  # NOT the file content
    assert "secret-content" not in json.dumps(record)
    assert record["attachments"] == [{"id": attachment_id, "name": "f.py", "stored": rel}]


def test_append_message_without_attachments_omits_key(store: TransientStore) -> None:
    store.append_message("user", "hi")
    record = store.read_session_lines()[0]
    assert "attachments" not in record


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


def test_set_session_name_disambiguates_against_sibling_sessions(kodo_dir: Path) -> None:
    first = TransientStore(kodo_dir)
    first.attach_session("1748792600", resumed=False)
    first.set_session_name("Search Pagination Fix")

    # A second, different session titled from a similar/identical prompt
    # would otherwise collide with the first — the titler is a deterministic
    # function of the sanitized prompt text.
    second = TransientStore(kodo_dir)
    second.attach_session("1748792601", resumed=False)
    second.set_session_name("Search Pagination Fix")
    assert second.session_name == "Search Pagination Fix-1"

    third = TransientStore(kodo_dir)
    third.attach_session("1748792602", resumed=False)
    third.set_session_name("Search Pagination Fix")
    assert third.session_name == "Search Pagination Fix-2"

    # Renaming a session back to its OWN already-persisted name is not a
    # collision with itself.
    first.set_session_name("Search Pagination Fix")
    assert first.session_name == "Search Pagination Fix"


def test_session_dir_layout_on_new_session(store: TransientStore) -> None:
    assert store.session_dir.is_dir()
    assert (store.session_dir / "subsessions").is_dir()
    assert (store.session_dir / "toolcalls").is_dir()


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


def test_workflow_mode_defaults_to_guided(store: TransientStore) -> None:
    assert store.workflow_mode == "guided"


def test_workflow_mode_update_is_persisted(store: TransientStore) -> None:
    store.update(workflow_mode="problem_solving")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["workflow_mode"] == "problem_solving"
    assert store.workflow_mode == "problem_solving"


def test_resumed_session_restores_workflow_mode(kodo_dir: Path) -> None:
    session_id = "1748792410"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.update(workflow_mode="problem_solving")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.workflow_mode == "problem_solving"


def test_edit_control_defaults_to_smart(store: TransientStore) -> None:
    assert store.edit_control == "smart"


def test_edit_control_update_is_persisted(store: TransientStore) -> None:
    store.update(edit_control="review_all")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["edit_control"] == "review_all"
    assert store.edit_control == "review_all"


def test_command_control_defaults_to_smart(store: TransientStore) -> None:
    assert store.command_control == "smart"


def test_command_control_update_is_persisted(store: TransientStore) -> None:
    store.update(command_control="permissive")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["command_control"] == "permissive"
    assert store.command_control == "permissive"


def test_edit_control_invalid_value_falls_back_to_smart(kodo_dir: Path) -> None:
    session_id = "1748792411"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    # Hand-corrupt the persisted value, then reload.
    path = first.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edit_control"] = "bogus"
    data["command_control"] = "bogus"
    path.write_text(json.dumps(data), encoding="utf-8")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.edit_control == "smart"
    assert second.command_control == "smart"


def test_resumed_session_restores_edit_and_command_control(kodo_dir: Path) -> None:
    session_id = "1748792412"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.update(edit_control="review_all", command_control="defensive")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.edit_control == "review_all"
    assert second.command_control == "defensive"


def test_security_rules_defaults_to_empty(store: TransientStore) -> None:
    assert store.security_rules == frozenset()


def test_add_security_rule_persists_and_accumulates(store: TransientStore) -> None:
    store.add_security_rule("git", "push")
    store.add_security_rule("npm", "publish")

    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert sorted(data["security_rules"]) == [["git", "push"], ["npm", "publish"]]
    assert store.security_rules == frozenset({("git", "push"), ("npm", "publish")})


def test_add_security_rule_is_idempotent(store: TransientStore) -> None:
    store.add_security_rule("git", "push")
    store.add_security_rule("git", "push")
    assert store.security_rules == frozenset({("git", "push")})


def test_resumed_session_restores_security_rules(kodo_dir: Path) -> None:
    session_id = "1748792413"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.add_security_rule("docker", "run")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.security_rules == frozenset({("docker", "run")})


def test_malformed_security_rules_falls_back_to_empty(kodo_dir: Path) -> None:
    session_id = "1748792414"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    path = first.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["security_rules"] = "not-a-list"
    path.write_text(json.dumps(data), encoding="utf-8")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.security_rules == frozenset()


def test_security_path_rules_defaults_to_empty(store: TransientStore) -> None:
    assert store.security_path_rules == frozenset()


def test_add_security_path_rule_persists_and_accumulates(store: TransientStore) -> None:
    store.add_security_path_rule("cat", "/etc/hosts")
    store.add_security_path_rule("cd", "/outside/path")

    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert sorted(data["security_path_rules"]) == [["cat", "/etc/hosts"], ["cd", "/outside/path"]]
    assert store.security_path_rules == frozenset({("cat", "/etc/hosts"), ("cd", "/outside/path")})


def test_add_security_path_rule_is_idempotent(store: TransientStore) -> None:
    store.add_security_path_rule("cat", "/etc/hosts")
    store.add_security_path_rule("cat", "/etc/hosts")
    assert store.security_path_rules == frozenset({("cat", "/etc/hosts")})


def test_resumed_session_restores_security_path_rules(kodo_dir: Path) -> None:
    session_id = "1748792415"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.add_security_path_rule("cat", "/etc/hosts")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.security_path_rules == frozenset({("cat", "/etc/hosts")})


def test_malformed_security_path_rules_falls_back_to_empty(kodo_dir: Path) -> None:
    session_id = "1748792416"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    path = first.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["security_path_rules"] = "not-a-list"
    path.write_text(json.dumps(data), encoding="utf-8")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.security_path_rules == frozenset()


def test_pending_security_alert_defaults_to_none(store: TransientStore) -> None:
    assert store.pending_security_alert is None


def test_pending_security_alert_update_is_persisted(store: TransientStore) -> None:
    store.update(pending_security_alert="tu_1")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["pending_security_alert"] == "tu_1"
    assert store.pending_security_alert == "tu_1"


def test_pending_security_alert_cleared_by_explicit_none(store: TransientStore) -> None:
    store.update(pending_security_alert="tu_1")
    store.update(pending_security_alert=None)
    assert store.pending_security_alert is None
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["pending_security_alert"] is None


def test_pending_security_alert_left_unchanged_when_omitted(store: TransientStore) -> None:
    store.update(pending_security_alert="tu_1")
    store.update(stage="RUNNING")
    assert store.pending_security_alert == "tu_1"


def test_resumed_session_restores_pending_security_alert(kodo_dir: Path) -> None:
    session_id = "1748792413"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.update(pending_security_alert="tu_9")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.pending_security_alert == "tu_9"


def test_malformed_pending_security_alert_falls_back_to_none(kodo_dir: Path) -> None:
    session_id = "1748792414"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    path = first.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pending_security_alert"] = 42
    path.write_text(json.dumps(data), encoding="utf-8")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.pending_security_alert is None


def test_thinking_level_defaults_to_empty_string(store: TransientStore) -> None:
    assert store.thinking_level == ""


def test_thinking_level_update_is_persisted(store: TransientStore) -> None:
    store.update(thinking_level="unlimited")
    data = json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))
    assert data["thinking_level"] == "unlimited"
    assert store.thinking_level == "unlimited"


def test_thinking_level_update_to_empty_string_is_distinguished_from_unset(
    store: TransientStore,
) -> None:
    # thinking_level="" is a legitimate value (no thinking family) — update()
    # must not treat it as "leave unchanged" the way it would for a bare None.
    store.update(thinking_level="medium")
    store.update(thinking_level="")
    assert store.thinking_level == ""


def test_resumed_session_restores_thinking_level(kodo_dir: Path) -> None:
    session_id = "1748792413"
    first = TransientStore(kodo_dir)
    first.attach_session(session_id, resumed=False)
    first.update(thinking_level="high")

    second = TransientStore(kodo_dir)
    second.attach_session(session_id, resumed=True)
    assert second.thinking_level == "high"


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
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello there."
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Hi! How can I help?"


def test_appended_entries_are_stamped_with_id_and_ts(store: TransientStore) -> None:
    """Every persisted entry gets a unique ``id`` and an ISO-8601 ``ts`` —
    centralized in ``__append_line`` so every caller (message or marker, main
    log or subsession) gets both for free, no matter which append method it
    used."""
    store.append_message("user", "Hello there.")
    store.append_marker({"type": "error", "message": "boom", "recoverable": True})
    lines = store.read_session_lines()
    assert len(lines) == 2
    ids = {line["id"] for line in lines}
    assert len(ids) == 2  # unique per entry
    for line in lines:
        assert isinstance(line["id"], str) and line["id"]
        datetime.fromisoformat(str(line["ts"]))  # parses without raising


def test_read_messages_returns_empty_when_no_log(store: TransientStore) -> None:
    assert store.read_messages() == []


def test_append_message_with_content_blocks(store: TransientStore) -> None:
    blocks = [{"type": "text", "text": "result"}, {"type": "tool_use", "id": "t1"}]
    store.append_message("assistant", blocks)
    messages = store.read_messages()
    assert messages[0]["content"] == blocks


def test_session_log_path_is_inside_session_dir(store: TransientStore) -> None:
    assert store.session_log_path == store.session_dir / "session.jsonl"
