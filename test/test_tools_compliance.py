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
from kodo.runtime import ApprovalResponse, SessionState
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME, ProjectPathResolver, RootPath, ToolDispatcher
from kodo.toolspecs import (
    ALL_TOOLS,
    SCHEMA_COMPLIANCE_KEY,
    VISIBILITY_VALUES,
    SecurityImpact,
    normalize_output,
    requires_intent,
    tool_result_succeeded,
)
from kodo.websearch import BrowserUnavailableError as WebsearchBrowserUnavailableError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGate:
    """Structural ``GateLike`` whose prompts resolve instantly."""

    def __init__(self, *, answer: str = "ok", choice: str = "yes", action: str = "agree") -> None:
        self._answer = answer
        self._choice = choice
        self._action = action

    async def fire_questions(
        self, questions: list[dict[str, object]], tool_call_id: str = ""
    ) -> list[dict[str, object]]:
        return [{"selected": [self._choice], "free_text": self._answer} for _ in questions]

    async def fire_approval(
        self, gate_type: str, *, artifact_id: str | None = None, summary: str = ""
    ) -> ApprovalResponse:
        return ApprovalResponse(action=self._action, feedback="")


class _FakeServices:
    """Structural ``EngineServices`` returning canned values."""

    async def run_subagent(
        self, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        return {"primary_path": "specs/sub.md", "paths": ["specs/sub.md"], "summary": "done"}

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        return {
            "status": "completed",
            "summary": "added foo",
            "commands_run": ["uv add foo"],
            "files_changed": ["pyproject.toml", "uv.lock"],
        }

    async def run_web_search_agent(
        self, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        return {"themes": [], "note": "stub"}

    async def run_author_critic_iteration(
        self,
        caller: str,
        author_name: str,
        critic_name: str,
        path: str,
        input_paths: dict[str, str],
        instructions: str,
        for_revision: bool,
    ) -> dict[str, object]:
        return {"path": path or "specs/ac.md", "status": "accepted", "concerns": []}

    async def rollback(self, target_sha: str) -> None:
        return None

    async def disable_autonomous_mode(self) -> None:
        return None

    async def create_project(
        self, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        return {"path": path or "/tmp/new-project", "name": name}

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        return None


def _make_dispatcher(
    tmp_path: Path,
    *,
    agent_name: str = "test_agent",
    autonomous: bool = False,
    mode: str = "guided",
    project_root: Path | None | object = ...,
    root_paths: tuple[RootPath, ...] = (),
    util_paths: dict[str, Path] | None = None,
    output_schema: dict[str, object] | None = None,
) -> ToolDispatcher:
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous
    resolved_project_root = tmp_path if project_root is ... else project_root
    return ToolDispatcher(
        resolver=ProjectPathResolver(tmp_path),
        gate=_FakeGate(),
        session=session,
        services=_FakeServices(),
        agent_name=agent_name,
        session_id="sess-test",
        mode=mode,
        project_root=resolved_project_root,
        root_paths=root_paths,
        util_paths=util_paths,
        output_schema=output_schema,
    )


async def _dispatch(dispatcher: ToolDispatcher, name: str, payload: dict[str, object]) -> object:
    # Mutating tools require a non-blank `intent`; inject a default so each
    # case stays focused on its own compliance behavior (enforcement itself is
    # covered in test_tools_leaf.py).
    spec = DISPATCHABLE_TOOLS_BY_NAME.get(name)
    if spec is not None and requires_intent(spec) and "intent" not in payload:
        payload = {"intent": "exercise this tool in a compliance test", **payload}
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


async def _write_file(dispatcher: ToolDispatcher, path: str, content: str = "body") -> None:
    parsed = await _dispatch(
        dispatcher, "filesystem", {"operation": "create_file", "path": path, "content": content}
    )
    assert isinstance(parsed, dict) and parsed.get("status") == "created", parsed


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


@pytest.mark.asyncio
async def test_read_file_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    await _write_file(d, "a.txt", "line1\nline2\nline3\n")
    _assert_compliant("read_file", await _dispatch(d, "read_file", {"path": "a.txt"}))
    _assert_compliant(
        "read_file",
        await _dispatch(
            d, "read_file", {"path": "a.txt", "ranges": [{"start_line": 1, "end_line": 2}]}
        ),
    )
    _assert_compliant("read_file", await _dispatch(d, "read_file", {"path": "missing.txt"}))
    _assert_compliant(
        "read_file",
        await _dispatch(
            d,
            "read_file",
            {"path": "a.txt", "ranges": [{"start_line": 1, "end_line": 1}], "pattern": "x"},
        ),
    )

    rg = find_util(kodo_user_dir(), "ripgrep")
    if rg is None:
        pytest.skip("ripgrep util not installed; pattern success path not exercised")
    d2 = _make_dispatcher(tmp_path, util_paths={"ripgrep": rg.path})
    await _write_file(d2, "b.txt", "needle here\nother\n")
    ok = _assert_compliant(
        "read_file", await _dispatch(d2, "read_file", {"path": "b.txt", "pattern": "needle"})
    )
    assert ok["matches"][0]["line"] == "needle here"


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
# Guided-dev tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guided_dev_status_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, mode="guided")
    _assert_compliant("guided_dev_status", await _dispatch(d, "guided_dev_status", {}))
    (tmp_path / "specs").mkdir()
    await _write_file(d, "specs/a.md", "x")
    _assert_compliant("guided_dev_status", await _dispatch(d, "guided_dev_status", {}))
    # Wrong mode → compliant error envelope, not an exception.
    ps = _make_dispatcher(tmp_path, mode="problem_solving")
    _assert_compliant("guided_dev_status", await _dispatch(ps, "guided_dev_status", {}))


@pytest.mark.asyncio
async def test_document_feedback_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="architect_critic")
    (tmp_path / "specs").mkdir()
    await _write_file(d, "specs/a.md", "x")
    _assert_compliant(
        "document_feedback",
        await _dispatch(d, "document_feedback", {"path": "specs/a.md", "accept": True}),
    )
    _assert_compliant(
        "document_feedback",
        await _dispatch(
            d,
            "document_feedback",
            {
                "path": "specs/a.md",
                "accept": False,
                "concerns": [{"kind": "gap", "description": "x"}],
            },
        ),
    )
    # Error: rejected with no concerns.
    _assert_compliant(
        "document_feedback",
        await _dispatch(d, "document_feedback", {"path": "specs/a.md", "accept": False}),
    )


@pytest.mark.asyncio
async def test_toolchain_build_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    steps = {"build": True, "static_analysis": False, "test": False}
    # project_path is mandatory → compliant error envelope without it.
    missing = _assert_compliant("toolchain_build", await _dispatch(d, "toolchain_build", steps))
    assert isinstance(missing, dict) and "project_path" in str(missing["error"])
    # No scripts yet → compliant failure envelope with a helpful step log.
    payload: dict[str, object] = {"project_path": str(tmp_path), **steps}
    no_scripts = _assert_compliant(
        "toolchain_build", await _dispatch(d, "toolchain_build", payload)
    )
    assert isinstance(no_scripts, dict) and no_scripts["success"] is False
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    build_sh = scripts / "build.sh"
    build_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    build_sh.chmod(0o755)
    # Absolute project_path runs the script; a relative one resolves through
    # the run's resolver ("." = the project root in Guided mode).
    built = _assert_compliant("toolchain_build", await _dispatch(d, "toolchain_build", payload))
    assert isinstance(built, dict) and built["success"] is True
    relative = _assert_compliant(
        "toolchain_build", await _dispatch(d, "toolchain_build", {"project_path": ".", **steps})
    )
    assert isinstance(relative, dict) and relative["success"] is True


@pytest.mark.asyncio
async def test_toolchain_deps_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    # project_root_path is mandatory → compliant error envelope without it.
    missing = _assert_compliant(
        "toolchain_deps", await _dispatch(d, "toolchain_deps", {"action": "add", "name": "foo"})
    )
    assert isinstance(missing, dict) and missing["success"] is False
    assert "project_root_path" in missing["message"]
    parsed = _assert_compliant(
        "toolchain_deps",
        await _dispatch(
            d,
            "toolchain_deps",
            {"project_root_path": str(tmp_path), "action": "add", "name": "foo"},
        ),
    )
    # The sub-agent's structured result is mapped onto the tool envelope.
    assert parsed["success"] is True
    assert parsed["status"] == "completed"
    assert parsed["commands_run"] == ["uv add foo"]


class _NoDepsMdServices(_FakeServices):
    """Dependency manager reporting no DEPENDENCIES.md, as on an unset-up project."""

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        return {"status": "dependencies_md_missing", "summary": "no DEPENDENCIES.md at root"}


@pytest.mark.asyncio
async def test_toolchain_deps_missing_dependencies_md_returns_remediation(tmp_path: Path) -> None:
    session = SessionState()
    d = ToolDispatcher(
        resolver=ProjectPathResolver(tmp_path),
        gate=_FakeGate(),
        session=session,
        services=_NoDepsMdServices(),
        agent_name="coder",
        session_id="sess-test",
        mode="guided",
        project_root=tmp_path,
    )
    parsed = _assert_compliant(
        "toolchain_deps",
        await _dispatch(
            d,
            "toolchain_deps",
            {"project_root_path": str(tmp_path), "action": "add", "name": "foo"},
        ),
    )
    assert parsed["success"] is False
    assert parsed["status"] == "dependencies_md_missing"
    # The caller gets an actionable sub-prompt naming the toolchain-setup route.
    assert "toolchain_python" in parsed["message"]
    assert "run_subagent" in parsed["message"]


# ---------------------------------------------------------------------------
# Control / escalation tools
# ---------------------------------------------------------------------------


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
    res = _assert_compliant(
        "ask_user",
        await _dispatch(
            d,
            "ask_user",
            {
                "questions": [
                    {"question": "Which DB?", "kind": "single_choice", "options": ["PostgreSQL"]},
                    {
                        "question": "Which features?",
                        "kind": "multi_choice",
                        "options": ["Auth", "Billing"],
                    },
                ]
            },
        ),
    )
    answers = res["answers"]
    assert isinstance(answers, list) and len(answers) == 2
    assert all("selected" in a and "free_text" in a for a in answers)


@pytest.mark.asyncio
async def test_ask_user_rejects_malformed_batches(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    # No questions at all.
    res = await _dispatch(d, "ask_user", {})
    assert "error" in res
    # A question without candidate options.
    res = await _dispatch(
        d,
        "ask_user",
        {"questions": [{"question": "q?", "kind": "single_choice", "options": []}]},
    )
    assert "error" in res
    # An invalid kind.
    res = await _dispatch(
        d,
        "ask_user",
        {"questions": [{"question": "q?", "kind": "free_text", "options": ["a"]}]},
    )
    assert "error" in res


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
        await _dispatch(
            d,
            "return_result",
            {"result": {"primary_path": "src/a.py", "paths": ["src/a.py"], "summary": "s"}},
        ),
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
            {"author_name": "a", "critic_name": "c", "instructions": "go"},
        ),
    )
    # for_revision without a path → compliant error envelope.
    _assert_compliant(
        "run_author_critic_iteration",
        await _dispatch(
            d,
            "run_author_critic_iteration",
            {"author_name": "a", "critic_name": "c", "instructions": "go", "for_revision": True},
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


class _WebSearchAgentFailsServices(_FakeServices):
    """``run_web_search_agent`` blows up, as if the agent turn itself failed."""

    async def run_web_search_agent(
        self, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        raise RuntimeError("agent turn exploded")


@pytest.mark.asyncio
async def test_web_search_compliance(tmp_path: Path) -> None:
    # WebSearchTool is a thin wrapper: it validates/clamps input and returns
    # whatever the web_search agent (via EngineServices.run_web_search_agent)
    # produced, verbatim.
    d = _make_dispatcher(tmp_path, agent_name="investigator")
    parsed = _assert_compliant(
        "web_search",
        await _dispatch(d, "web_search", {"query": "how to parse RFC 3339 in python"}),
    )
    assert parsed["themes"] == []
    assert parsed["note"] == "stub"
    # Missing query is rejected with the universal error envelope.
    _assert_compliant("web_search", await _dispatch(d, "web_search", {}))

    # A service-level failure (the agent turn itself blowing up) degrades to a
    # compliant themes:[]/note result rather than raising.
    session = SessionState()
    failing = ToolDispatcher(
        resolver=ProjectPathResolver(tmp_path),
        gate=_FakeGate(),
        session=session,
        services=_WebSearchAgentFailsServices(),
        agent_name="investigator",
        session_id="sess-test",
        mode="guided",
        project_root=tmp_path,
    )
    failed = _assert_compliant(
        "web_search", await _dispatch(failing, "web_search", {"query": "anything"})
    )
    assert failed["themes"] == []
    assert "failed" in failed["note"].lower()


@pytest.mark.asyncio
async def test_read_webpage_compliance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the pipeline off the real network: an unopenable browser makes the
    # handler degrade to the universal error envelope.
    class _NoBrowserSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> object:
            raise WebsearchBrowserUnavailableError("no browser in tests")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("kodo.tools._read_webpage.BrowserSession", _NoBrowserSession)
    d = _make_dispatcher(tmp_path, agent_name="investigator")
    _assert_compliant(
        "read_webpage",
        await _dispatch(d, "read_webpage", {"url": "https://example.com/docs"}),
    )
    # Missing url is rejected with the universal error envelope.
    _assert_compliant("read_webpage", await _dispatch(d, "read_webpage", {}))
    # A private-network URL is rejected by the SSRF guard, also as an error.
    _assert_compliant(
        "read_webpage",
        await _dispatch(d, "read_webpage", {"url": "http://127.0.0.1:8080/admin"}),
    )
    # An unsupported browser/content_filter value is also a universal error.
    _assert_compliant(
        "read_webpage",
        await _dispatch(d, "read_webpage", {"url": "https://example.com/docs", "browser": "nope"}),
    )


@pytest.mark.asyncio
async def test_query_search_engine_compliance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        @property
        def browser(self) -> object:
            return object()

    monkeypatch.setattr("kodo.tools._query_search_engine.BrowserSession", _FakeSession)

    async def _fake_hits(browser: object, engine: object, query: str) -> list[dict[str, str]]:
        return [{"url": "https://example.com/a", "title": "A", "snippet": "..."}]

    monkeypatch.setattr("kodo.tools._query_search_engine.query_via_browser", _fake_hits)
    d = _make_dispatcher(tmp_path, agent_name="web_search")
    parsed = _assert_compliant(
        "query_search_engine",
        await _dispatch(d, "query_search_engine", {"engine": "duckduckgo", "query": "asyncio"}),
    )
    assert parsed["hits"][0]["url"] == "https://example.com/a"

    # An engine wall (the query function returns None) is a compliant error,
    # distinct from a legitimate empty hits list.
    async def _fake_blocked(browser: object, engine: object, query: str) -> None:
        return None

    monkeypatch.setattr("kodo.tools._query_search_engine.query_via_browser", _fake_blocked)
    blocked = _assert_compliant(
        "query_search_engine",
        await _dispatch(d, "query_search_engine", {"engine": "google", "query": "x"}),
    )
    assert "error" in blocked

    # Unknown engine / query missing / bad browser -> universal error envelope.
    _assert_compliant(
        "query_search_engine",
        await _dispatch(d, "query_search_engine", {"engine": "altavista", "query": "x"}),
    )
    _assert_compliant(
        "query_search_engine", await _dispatch(d, "query_search_engine", {"engine": "google"})
    )


@pytest.mark.asyncio
async def test_get_and_update_web_search_state_compliance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Redirect the state file away from the real ~/.kodo home, same reasoning
    # as monkeypatching BrowserSession for the browser-backed tools above.
    monkeypatch.setattr("kodo.tools._get_web_search_state.kodo_user_dir", lambda: tmp_path)
    monkeypatch.setattr("kodo.tools._update_web_search_state.kodo_user_dir", lambda: tmp_path)
    d = _make_dispatcher(tmp_path, agent_name="web_search")

    empty = _assert_compliant(
        "get_web_search_state", await _dispatch(d, "get_web_search_state", {})
    )
    assert empty["state"] == {}

    _assert_compliant(
        "update_web_search_state",
        await _dispatch(d, "update_web_search_state", {"key": "google_status", "value": "blocked"}),
    )
    after_write = _assert_compliant(
        "get_web_search_state", await _dispatch(d, "get_web_search_state", {})
    )
    assert after_write["state"] == {"google_status": "blocked"}

    # Deleting via an empty-string value.
    _assert_compliant(
        "update_web_search_state",
        await _dispatch(d, "update_web_search_state", {"key": "google_status", "value": ""}),
    )
    after_delete = _assert_compliant(
        "get_web_search_state", await _dispatch(d, "get_web_search_state", {})
    )
    assert after_delete["state"] == {}

    # Missing key/value -> universal error envelope.
    _assert_compliant(
        "update_web_search_state", await _dispatch(d, "update_web_search_state", {"key": "x"})
    )


@pytest.mark.asyncio
async def test_wait_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="web_search")
    _assert_compliant("wait", await _dispatch(d, "wait", {"seconds": 0.01}))
    # Omitted seconds still compliant (falls back to the tool's own default);
    # not exercised at full length here to keep the test fast.


@pytest.mark.asyncio
async def test_remaining_time_compliance(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, agent_name="web_search")
    # No deadline set on this context -> fails closed at 0, still compliant.
    parsed = _assert_compliant("remaining_time", await _dispatch(d, "remaining_time", {}))
    assert parsed["remaining_seconds"] == 0.0


def test_all_dispatchable_tools_are_covered() -> None:
    """Fail if a new dispatchable tool is added without a compliance scenario."""
    covered = {
        "filesystem",
        "edit_file",
        "run_command",
        "read_file",
        "get_root_paths",
        "find_files",
        "find_text_in_files",
        "guided_dev_status",
        "document_feedback",
        "toolchain_build",
        "toolchain_deps",
        "escalate_blocker",
        "ask_user",
        "run_subagent",
        "run_author_critic_iteration",
        "return_result",
        "rollback",
        "finalize_project",
        "disable_autonomous_mode",
        "create_new_project",
        "web_search",
        "read_webpage",
        "query_search_engine",
        "get_web_search_state",
        "update_web_search_state",
        "wait",
        "remaining_time",
    }
    assert set(DISPATCHABLE_TOOLS_BY_NAME) == covered, (
        "Dispatchable tools changed; add a compliance scenario for: "
        f"{set(DISPATCHABLE_TOOLS_BY_NAME) ^ covered}"
    )
