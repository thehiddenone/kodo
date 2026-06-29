"""Output-schema compliance tests for every dispatchable tool.

These tests are a guard rail: they exercise a range of realistic scenarios per
tool (success paths, error paths, and edge inputs) and assert that each tool's
raw output is *compliant* with its declared
:attr:`~kodo.toolspecs.ToolSpec.output_schema` — i.e. the engine's
:func:`~kodo.toolspecs.normalize_output` would not have to drop undeclared
fields or backfill missing required ones. A future change that breaks a tool's
output shape (or a schema that drifts from its handler) fails here instead of
silently surfacing ``schema_compliance: false`` to agents at runtime.

The ``{"error": ...}`` envelope is universal and intentionally always compliant,
so error-path scenarios assert the envelope shape rather than success keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.binutils import find_util
from kodo.project import kodo_user_dir
from kodo.runtime import ApprovalResponse, QuestionResponse, SessionState
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME, ProjectPathResolver, RootPath, ToolDispatcher
from kodo.toolspecs import (
    ALL_TOOLS,
    SCHEMA_COMPLIANCE_KEY,
    VISIBILITY_VALUES,
    SecurityImpact,
    normalize_output,
    tool_result_succeeded,
)
from kodo.workspace import ProjectIndex, Workspace

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGate:
    """Structural ``GateLike`` whose prompts resolve instantly."""

    def __init__(self, *, answer: str = "ok", choice: str = "yes", action: str = "agree") -> None:
        self._answer = answer
        self._choice = choice
        self._action = action

    async def fire_question(
        self, question: str, mode: str, choices: list[dict[str, str]] | None = None
    ) -> QuestionResponse:
        return QuestionResponse(answer_text=self._answer, choice_key=self._choice)

    async def fire_approval(
        self, gate_type: str, *, artifact_id: str | None = None, summary: str = ""
    ) -> ApprovalResponse:
        return ApprovalResponse(action=self._action, feedback="")


class _FakeServices:
    """Structural ``EngineServices`` returning canned values."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        return {"artifact_ids": ["sub-art-1"], "summary": "done"}

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        input_artifact_ids: list[str],
        for_revision_artifact_ids: list[str],
    ) -> dict[str, object]:
        return {"artifact_id": "ac-art-1", "verdict": "accepted", "concerns": []}

    async def rollback(self, target_sha: str) -> None:
        return None

    async def complete_artifact(self, artifact_id: str) -> None:
        await self._workspace.mark_completed(artifact_id)

    async def disable_autonomous_mode(self) -> None:
        return None

    async def create_project(self, name: str, path: str | None = None) -> dict[str, object]:
        return {"path": path or "/tmp/new-project", "name": name}


def _make_dispatcher(
    tmp_path: Path,
    *,
    agent_name: str = "test_agent",
    autonomous: bool = False,
    root_paths: tuple[RootPath, ...] = (),
    util_paths: dict[str, Path] | None = None,
    output_schema: dict[str, object] | None = None,
) -> ToolDispatcher:
    index = ProjectIndex()
    ws = Workspace(tmp_path, index)
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous
    return ToolDispatcher(
        workspace=ws,
        index=index,
        resolver=ProjectPathResolver(tmp_path),
        gate=_FakeGate(),
        session=session,
        services=_FakeServices(ws),
        agent_name=agent_name,
        session_id="sess-test",
        root_paths=root_paths,
        util_paths=util_paths,
        output_schema=output_schema,
    )


async def _dispatch(dispatcher: ToolDispatcher, name: str, payload: dict[str, object]) -> object:
    return json.loads(await dispatcher.dispatch(name, payload))


def _assert_compliant(name: str, parsed: object) -> dict[str, object]:
    """Assert *parsed* matches the tool's declared output schema.

    Returns the parsed dict for further assertions. Error envelopes are
    accepted as compliant by design. A top-level ``"diff"`` key is the one
    other sanctioned undeclared field (an engine-only side channel for the
    before/after diff link — see ``kodo.state.write_diff_files``); the engine
    pops it before normalizing, so this test mirrors that by stripping it too.
    """
    spec = DISPATCHABLE_TOOLS_BY_NAME[name]
    assert isinstance(parsed, dict), f"{name} returned a non-object: {parsed!r}"
    checked = {k: v for k, v in parsed.items() if k != "diff"}
    normalized, compliant = normalize_output(spec.output_schema, checked)
    if "error" in checked:
        assert compliant, f"{name} error envelope unexpectedly non-compliant"
        return parsed
    assert compliant, (
        f"{name} output {checked!r} is NOT compliant with its schema (normalized -> {normalized!r})"
    )
    # schema_compliance is engine-owned; tools must not emit it themselves.
    assert SCHEMA_COMPLIANCE_KEY not in checked, f"{name} must not declare schema_compliance itself"
    return parsed


async def _publish(dispatcher: ToolDispatcher, content: str = "body") -> str:
    parsed = await _dispatch(
        dispatcher,
        "publish_artifact",
        {
            "type": "narrative",
            "project_code": "TEST",
            "responsibility_code": "TEST",
            "content": content,
        },
    )
    assert isinstance(parsed, dict)
    return str(parsed["id"])


# ---------------------------------------------------------------------------
# Static spec invariants
# ---------------------------------------------------------------------------


def test_every_spec_has_well_formed_new_fields() -> None:
    for spec in ALL_TOOLS:
        assert isinstance(spec.security_impact, SecurityImpact), spec.name
        assert isinstance(spec.output_schema, dict) and spec.output_schema, spec.name
        # schema_compliance is engine-owned and must never be pre-declared.
        props = spec.output_schema.get("properties", {})
        assert SCHEMA_COMPLIANCE_KEY not in props, spec.name
        for vis_map in (spec.input_visibility, spec.output_visibility):
            for value in vis_map.values():
                assert value in VISIBILITY_VALUES, (spec.name, value)


def test_tool_result_succeeded_classification() -> None:
    """The success/✓ vs failure/✗ classifier driving the VSIX tool-name badge."""
    # Not-yet-known: no result has arrived.
    assert tool_result_succeeded(None) is None
    # Error envelope is always a failure, regardless of other keys.
    assert tool_result_succeeded({"error": "boom"}) is False
    assert tool_result_succeeded({"error": "boom", "exit_code": 0}) is False
    # run_command: only exit_code 0 succeeds; non-zero and null (timeout) fail.
    assert tool_result_succeeded({"exit_code": 0, "stdout": "", "stderr": ""}) is True
    assert tool_result_succeeded({"exit_code": 1, "stdout": "", "stderr": ""}) is False
    assert tool_result_succeeded({"exit_code": None, "stdout": "", "stderr": ""}) is False
    # Boolean success field (toolchain_*).
    assert tool_result_succeeded({"success": True, "log": ""}) is True
    assert tool_result_succeeded({"success": False, "log": ""}) is False
    # Plain compliant status envelope → success.
    assert tool_result_succeeded({"status": "created", "path": "a.txt"}) is True
    # schema_compliance is ignored: a repaired-but-successful result still passes.
    assert tool_result_succeeded({"status": "edited", SCHEMA_COMPLIANCE_KEY: False}) is True


def test_visibility_keys_reference_declared_properties() -> None:
    for spec in ALL_TOOLS:
        in_props = set(spec.input_schema.get("properties", {}))
        out_props = set(spec.output_schema.get("properties", {}))
        assert set(spec.input_visibility) <= in_props, (spec.name, spec.input_visibility)
        assert set(spec.output_visibility) <= out_props, (spec.name, spec.output_visibility)


# ---------------------------------------------------------------------------
# File I/O tools
# ---------------------------------------------------------------------------


async def _assert_fs(d: ToolDispatcher, payload: dict[str, object]) -> None:
    """Dispatch one ``filesystem`` operation and assert the result is compliant."""
    _assert_compliant("filesystem", await _dispatch(d, "filesystem", payload))


@pytest.mark.asyncio
async def test_filesystem_file_ops_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    # create_file: success + already-exists error.
    await _assert_fs(d, {"operation": "create_file", "path": "a.txt", "content": "x"})
    err = await _dispatch(
        d, "filesystem", {"operation": "create_file", "path": "a.txt", "content": "y"}
    )
    assert isinstance(err, dict) and "error" in err
    _assert_compliant("filesystem", err)
    # copy_file / move_file: success + missing-source error.
    await _assert_fs(d, {"operation": "copy_file", "source": "a.txt", "destination": "b.txt"})
    await _assert_fs(d, {"operation": "copy_file", "source": "no.txt", "destination": "c.txt"})
    await _assert_fs(d, {"operation": "move_file", "source": "b.txt", "destination": "d.txt"})
    await _assert_fs(d, {"operation": "move_file", "source": "no.txt", "destination": "e.txt"})
    # delete_file: success + already-gone error.
    await _assert_fs(d, {"operation": "delete_file", "path": "a.txt"})
    await _assert_fs(d, {"operation": "delete_file", "path": "a.txt"})
    # Unknown operation → error envelope.
    await _assert_fs(d, {"operation": "nope"})


@pytest.mark.asyncio
async def test_filesystem_dir_ops_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    await _assert_fs(d, {"operation": "create_dir", "path": "src"})
    await _assert_fs(d, {"operation": "copy_dir", "source": "src", "destination": "dst"})
    # copy_dir onto an existing destination → error.
    await _assert_fs(d, {"operation": "copy_dir", "source": "src", "destination": "dst"})
    await _assert_fs(d, {"operation": "move_dir", "source": "dst", "destination": "moved"})
    await _assert_fs(d, {"operation": "delete_dir", "path": "src"})
    await _assert_fs(d, {"operation": "delete_dir", "path": "src"})


@pytest.mark.asyncio
async def test_edit_file_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    await _dispatch(d, "filesystem", {"operation": "create_file", "path": "a.txt", "content": "x"})
    _assert_compliant(
        "edit_file",
        await _dispatch(d, "edit_file", {"path": "a.txt", "old_string": "x", "new_string": "z"}),
    )
    _assert_compliant(
        "edit_file",
        await _dispatch(
            d, "edit_file", {"path": "missing.txt", "old_string": "x", "new_string": "z"}
        ),
    )


@pytest.mark.asyncio
async def test_run_command_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    ok = _assert_compliant(
        "run_command", await _dispatch(d, "run_command", {"command": "echo hi", "timeout": 10})
    )
    assert ok["exit_code"] == 0
    fail = _assert_compliant(
        "run_command", await _dispatch(d, "run_command", {"command": "exit 3", "timeout": 10})
    )
    assert fail["exit_code"] == 3


# ---------------------------------------------------------------------------
# Workspace search tools (get_root_paths / find_files / find_text_in_files)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_root_paths_compliance(tmp_path: Path) -> None:
    roots = (RootPath(name="proj", path=str(tmp_path)),)
    d = _make_dispatcher(tmp_path, root_paths=roots)
    parsed = _assert_compliant("get_root_paths", await _dispatch(d, "get_root_paths", {}))
    assert parsed["roots"] == [{"name": "proj", "path": str(tmp_path)}]
    # Degenerate (no roots synced) is still a compliant, empty result.
    empty = _make_dispatcher(tmp_path)
    _assert_compliant("get_root_paths", await _dispatch(empty, "get_root_paths", {}))


@pytest.mark.asyncio
async def test_find_files_compliance(tmp_path: Path) -> None:
    # Util-missing and bad-input paths return compliant error envelopes.
    d = _make_dispatcher(tmp_path)
    _assert_compliant("find_files", await _dispatch(d, "find_files", {}))
    _assert_compliant("find_files", await _dispatch(d, "find_files", {"root": str(tmp_path)}))

    fd = find_util(kodo_user_dir(), "fd")
    if fd is None:
        pytest.skip("fd util not installed; success path not exercised")
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("y", encoding="utf-8")
    d = _make_dispatcher(tmp_path, util_paths={"fd": fd.path})
    ok = _assert_compliant(
        "find_files",
        await _dispatch(d, "find_files", {"root": str(tmp_path), "extension": "py"}),
    )
    assert ok["files"] == ["alpha.py"]
    assert ok["count"] == 1 and ok["truncated"] is False


@pytest.mark.asyncio
async def test_find_text_in_files_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    _assert_compliant("find_text_in_files", await _dispatch(d, "find_text_in_files", {}))
    _assert_compliant(
        "find_text_in_files",
        await _dispatch(d, "find_text_in_files", {"query": "x", "root": str(tmp_path)}),
    )

    rg = find_util(kodo_user_dir(), "ripgrep")
    if rg is None:
        pytest.skip("ripgrep util not installed; success path not exercised")
    (tmp_path / "a.py").write_text("needle here\nother\n", encoding="utf-8")
    d = _make_dispatcher(tmp_path, util_paths={"ripgrep": rg.path})
    ok = _assert_compliant(
        "find_text_in_files",
        await _dispatch(d, "find_text_in_files", {"query": "needle", "root": str(tmp_path)}),
    )
    assert ok["matches"] == [{"path": "a.py", "line": 1, "text": "needle here"}]
    assert ok["count"] == 1 and ok["truncated"] is False


# ---------------------------------------------------------------------------
# Workspace / report tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_artifact_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="narrative_author")
    _assert_compliant(
        "publish_artifact",
        await _dispatch(
            d,
            "publish_artifact",
            {
                "type": "narrative",
                "project_code": "TEST",
                "responsibility_code": "TEST",
                "content": "c",
            },
        ),
    )
    # Error: invalid type.
    _assert_compliant("publish_artifact", await _dispatch(d, "publish_artifact", {"type": "bogus"}))
    # Error: missing required fields.
    _assert_compliant(
        "publish_artifact", await _dispatch(d, "publish_artifact", {"type": "narrative"})
    )


@pytest.mark.asyncio
async def test_read_artifact_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="narrative_author")
    art_id = await _publish(d)
    _assert_compliant("read_artifact", await _dispatch(d, "read_artifact", {"artifact_id": art_id}))
    _assert_compliant("read_artifact", await _dispatch(d, "read_artifact", {"artifact_id": "nope"}))


@pytest.mark.asyncio
async def test_list_artifacts_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="narrative_author")
    await _publish(d)
    _assert_compliant("list_artifacts", await _dispatch(d, "list_artifacts", {"type": "narrative"}))
    # Error: no filter supplied.
    _assert_compliant("list_artifacts", await _dispatch(d, "list_artifacts", {}))


@pytest.mark.asyncio
async def test_query_frontier_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    _assert_compliant("query_frontier", await _dispatch(d, "query_frontier", {}))


@pytest.mark.asyncio
async def test_report_artifact_completed_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="narrative_author")
    art_id = await _publish(d)
    _assert_compliant(
        "report_artifact_completed",
        await _dispatch(d, "report_artifact_completed", {"artifact_id": art_id}),
    )


@pytest.mark.asyncio
async def test_request_user_review_artifact_compliance(tmp_path: Path) -> None:
    art_in_auto = _make_dispatcher(tmp_path, autonomous=True)
    _assert_compliant(
        "request_user_review_artifact",
        await _dispatch(art_in_auto, "request_user_review_artifact", {"artifact_id": "x"}),
    )
    d = _make_dispatcher(tmp_path, agent_name="narrative_author")
    art_id = await _publish(d)
    _assert_compliant(
        "request_user_review_artifact",
        await _dispatch(d, "request_user_review_artifact", {"artifact_id": art_id}),
    )


@pytest.mark.asyncio
async def test_escalate_blocker_compliance(tmp_path: Path) -> None:
    auto = _make_dispatcher(tmp_path, autonomous=True)
    _assert_compliant(
        "escalate_blocker",
        await _dispatch(auto, "escalate_blocker", {"reason": "cap", "summary": "stuck"}),
    )
    inter = _make_dispatcher(tmp_path, autonomous=False)
    res = _assert_compliant(
        "escalate_blocker",
        await _dispatch(inter, "escalate_blocker", {"reason": "cap", "summary": "stuck"}),
    )
    assert "user_response" in res


@pytest.mark.asyncio
async def test_ask_user_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    _assert_compliant(
        "ask_user", await _dispatch(d, "ask_user", {"question": "q?", "mode": "free_text"})
    )
    _assert_compliant(
        "ask_user",
        await _dispatch(
            d,
            "ask_user",
            {"question": "q?", "mode": "choice", "choices": [{"key": "yes", "label": "Yes"}]},
        ),
    )


# ---------------------------------------------------------------------------
# Guide tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_subagent_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant(
        "run_subagent",
        await _dispatch(
            d,
            "run_subagent",
            {"name": "narrative_author", "task_input": {"instructions": "go"}},
        ),
    )


@pytest.mark.asyncio
async def test_return_result_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="coder")
    _assert_compliant(
        "return_result",
        await _dispatch(d, "return_result", {"result": {"artifact_ids": ["a"], "summary": "s"}}),
    )


@pytest.mark.asyncio
async def test_return_result_captures_normalized_output_and_stops(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}, "concerns": {"type": "array"}},
        "required": ["verdict", "concerns"],
    }
    d = _make_dispatcher(tmp_path, agent_name="architect_critic", output_schema=schema)
    await _dispatch(
        d,
        "return_result",
        {"result": {"verdict": "rejected", "concerns": [{"kind": "gap"}], "stray": 1}},
    )
    # The run ends after return_result, and the engine reads the normalized result.
    assert d.stop_requested
    out = d.returned_output
    assert out is not None
    assert out["verdict"] == "rejected"
    assert "stray" not in out  # undeclared field dropped by normalize_output
    assert out["schema_compliance"] is False  # because a field was dropped


@pytest.mark.asyncio
async def test_run_author_critic_iteration_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant(
        "run_author_critic_iteration",
        await _dispatch(
            d,
            "run_author_critic_iteration",
            {"author_name": "a", "critic_name": "c", "input_artifact_ids": []},
        ),
    )


@pytest.mark.asyncio
async def test_rollback_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant("rollback", await _dispatch(d, "rollback", {"target_sha": "abc123"}))
    _assert_compliant("rollback", await _dispatch(d, "rollback", {"target_sha": ""}))


@pytest.mark.asyncio
async def test_finalize_project_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant("finalize_project", await _dispatch(d, "finalize_project", {}))


@pytest.mark.asyncio
async def test_disable_autonomous_mode_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant(
        "disable_autonomous_mode",
        await _dispatch(d, "disable_autonomous_mode", {"reason": "loop"}),
    )


@pytest.mark.asyncio
async def test_create_new_project_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="guide")
    _assert_compliant(
        "create_new_project",
        await _dispatch(d, "create_new_project", {"name": "My Todo App"}),
    )
    # Empty name is rejected with the universal error envelope.
    _assert_compliant(
        "create_new_project",
        await _dispatch(d, "create_new_project", {"name": "   "}),
    )


def test_all_dispatchable_tools_are_covered() -> None:
    """Fail if a new dispatchable tool is added without a compliance scenario."""
    covered = {
        "filesystem",
        "edit_file",
        "run_command",
        "get_root_paths",
        "find_files",
        "find_text_in_files",
        "publish_artifact",
        "read_artifact",
        "list_artifacts",
        "query_frontier",
        "report_artifact_completed",
        "request_user_review_artifact",
        "escalate_blocker",
        "ask_user",
        "run_subagent",
        "run_author_critic_iteration",
        "return_result",
        "rollback",
        "finalize_project",
        "disable_autonomous_mode",
        "create_new_project",
    }
    assert set(DISPATCHABLE_TOOLS_BY_NAME) == covered, (
        "Dispatchable tools changed; add a compliance scenario for: "
        f"{set(DISPATCHABLE_TOOLS_BY_NAME) ^ covered}"
    )
