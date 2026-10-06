"""Behavior tests for the tools that delegate to the engine or the user, plus
the remaining file-I/O edge cases.

Covers ``ask_user`` (interactive and autonomous answering), ``rollback``,
``scaffold_new_project``, ``toolchain_deps``, ``toolchain_build``,
``create_directory``, and ``edit_file``. Engine collaborators are structural
fakes that record what they were asked to do, so each test asserts the
observable outcome — the tool result and what reached the engine — rather
than how the tool got there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kodo.project import ProjectLayoutError
from kodo.runtime import SessionState
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME, RootPath, ToolDispatcher
from kodo.toolspecs import requires_intent

_INTENT = "exercise this tool in a behavior test"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RootResolver:
    """Resolves paths under one root and refuses anything that escapes it."""

    __root: Path

    def __init__(self, root: Path) -> None:
        """Bind the resolver to *root*.

        Args:
            root (Path): The single allowed root.
        """
        self.__root = root.resolve()

    def resolve(self, path: str) -> Path:
        """Resolve *path* under the root, raising on an escape.

        Args:
            path (str): Absolute or root-relative path.

        Returns:
            Path: The resolved absolute path.
        """
        candidate = Path(path)
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (self.__root / path).resolve()
        )
        if resolved != self.__root and self.__root not in resolved.parents:
            raise PermissionError(f"Path {path!r} is outside the allowed roots.")
        return resolved

    @property
    def default_cwd(self) -> Path:
        return self.__root


class _Gate:
    """Structural ``GateLike`` that answers every question with fixed free text."""

    __calls: list[tuple[list[dict[str, object]], str]]

    def __init__(self) -> None:
        """Start with no recorded question batches."""
        self.__calls = []

    @property
    def calls(self) -> list[tuple[list[dict[str, object]], str]]:
        return list(self.__calls)

    async def fire_questions(
        self, questions: list[dict[str, object]], tool_call_id: str = ""
    ) -> list[dict[str, object]]:
        """Record the batch and answer each question as the "user".

        Args:
            questions (list[dict[str, object]]): The question batch.
            tool_call_id (str): The calling tool-use id.

        Returns:
            list[dict[str, object]]: One answer per question.
        """
        self.__calls.append((questions, tool_call_id))
        return [{"selected": [], "free_text": f"user answer {i}"} for i in range(len(questions))]


class _Services:
    """Structural ``EngineServices`` with configurable engine-side outcomes."""

    __root: Path
    __project_result: dict[str, object]
    __project_error: Exception | None
    __deps_result: dict[str, object]
    __rollback_error: Exception | None
    __deps_tasks: list[dict[str, object]]
    __rollbacks: list[tuple[str, str]]

    def __init__(
        self,
        root: Path,
        *,
        project_result: dict[str, object] | None = None,
        project_error: Exception | None = None,
        deps_result: dict[str, object] | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        """Configure the engine's canned answers.

        Args:
            root (Path): The bound project root.
            project_result (dict[str, object] | None): What ``init_project``
                returns.
            project_error (Exception | None): Raised by ``init_project``.
            deps_result (dict[str, object] | None): What the dependency manager
                sub-agent returns.
            rollback_error (Exception | None): Raised by ``rollback``.
        """
        self.__root = root
        self.__project_result = project_result or {}
        self.__project_error = project_error
        self.__deps_result = deps_result or {}
        self.__rollback_error = rollback_error
        self.__deps_tasks = []
        self.__rollbacks = []

    @property
    def deps_tasks(self) -> list[dict[str, object]]:
        return list(self.__deps_tasks)

    @property
    def rollbacks(self) -> list[tuple[str, str]]:
        return list(self.__rollbacks)

    def has_workspace(self) -> bool:
        """Report a bound workspace.

        Returns:
            bool: Always ``True``.
        """
        return True

    def root_paths(self) -> tuple[RootPath, ...]:
        """Return the one bound root.

        Returns:
            tuple[RootPath, ...]: The bound root.
        """
        return (RootPath(name="proj", path=str(self.__root)),)

    async def init_project(self, path: str) -> dict[str, object]:
        """Return (or raise) the configured scaffolding outcome.

        Args:
            path (str): The directory to scaffold.

        Returns:
            dict[str, object]: The configured result.
        """
        if self.__project_error is not None:
            raise self.__project_error
        return dict(self.__project_result)

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        """Record the delegated task and return the configured result.

        Args:
            task_input (dict[str, object]): The sub-agent's task.

        Returns:
            dict[str, object]: The configured result.
        """
        self.__deps_tasks.append(task_input)
        return dict(self.__deps_result)

    async def rollback(self, root: str, target_sha: str) -> None:
        """Record the rollback, or raise the configured error.

        Args:
            root (str): The resolved root to roll back.
            target_sha (str): The commit to roll back to.
        """
        if self.__rollback_error is not None:
            raise self.__rollback_error
        self.__rollbacks.append((root, target_sha))


def _make_dispatcher(
    root: Path,
    services: _Services | None = None,
    gate: _Gate | None = None,
    *,
    autonomous: bool = False,
) -> ToolDispatcher:
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous
    return ToolDispatcher(
        resolver=_RootResolver(root),
        gate=gate or _Gate(),
        session=session,
        services=services or _Services(root),  # type: ignore[arg-type]
        agent_name="test_agent",
        session_id="sess-test",
    )


async def _call(
    d: ToolDispatcher, name: str, payload: dict[str, object], tool_use_id: str = ""
) -> dict[str, object]:
    if requires_intent(DISPATCHABLE_TOOLS_BY_NAME[name]):
        payload = {"intent": _INTENT, **payload}
    result = json.loads(await d.dispatch(name, payload, tool_use_id))
    assert isinstance(result, dict)
    return result


_QUESTIONS: list[object] = [
    {"question": "Which database?", "kind": "single_choice", "options": ["sqlite", "postgres"]},
    {"question": "Which extras?", "kind": "multi_choice", "options": ["docs", "", "tests"]},
]


# ---------------------------------------------------------------------------
# ask_user
# ---------------------------------------------------------------------------


async def test_ask_user_interactive_returns_the_users_answers(tmp_path: Path) -> None:
    gate = _Gate()
    d = _make_dispatcher(tmp_path, gate=gate)
    result = await _call(d, "ask_user", {"questions": _QUESTIONS}, tool_use_id="tu-42")
    assert result == {
        "answers": [
            {"selected": [], "free_text": "user answer 0"},
            {"selected": [], "free_text": "user answer 1"},
        ]
    }
    # The client correlates the panel with the feed entry via the tool-use id,
    # and blank options never reach the user.
    [(asked, tool_use_id)] = gate.calls
    assert tool_use_id == "tu-42"
    assert asked[1]["options"] == ["docs", "tests"]


async def test_ask_user_autonomous_synthesizes_answers_without_the_gate(tmp_path: Path) -> None:
    gate = _Gate()
    d = _make_dispatcher(tmp_path, gate=gate, autonomous=True)
    result = await _call(d, "ask_user", {"questions": _QUESTIONS})
    answers = result["answers"]
    assert isinstance(answers, list)
    single, multi = answers
    assert single == {"selected": ["sqlite"], "free_text": None}
    assert multi["selected"] == []
    assert "User is away" in multi["free_text"]
    assert gate.calls == []


@pytest.mark.parametrize(
    ("question", "fragment"),
    [
        ("not an object", "questions[0] is not an object"),
        ({"question": "   ", "kind": "single_choice", "options": ["a"]}, "question is empty"),
        ({"question": "Q?", "kind": "free_text", "options": ["a"]}, "kind must be"),
        ({"question": "Q?", "kind": "single_choice", "options": "a"}, "options must list"),
    ],
)
async def test_ask_user_rejects_malformed_questions(
    tmp_path: Path, question: object, fragment: str
) -> None:
    gate = _Gate()
    d = _make_dispatcher(tmp_path, gate=gate)
    result = await _call(d, "ask_user", {"questions": [question]})
    assert fragment in str(result["error"])
    assert gate.calls == []


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


async def test_rollback_requires_target_sha(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "rollback", {"root": "."})
    assert result == {"error": "target_sha is required"}
    assert services.rollbacks == []


async def test_rollback_rejects_root_outside_allowed_roots(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    services = _Services(proj)
    d = _make_dispatcher(proj, services)
    result = await _call(d, "rollback", {"root": "../other", "target_sha": "abc123"})
    assert "outside the allowed roots" in str(result["error"])
    assert services.rollbacks == []


async def test_rollback_passes_resolved_root_to_engine(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "rollback", {"root": ".", "target_sha": " abc123 "})
    assert result == {"status": "completed"}
    assert services.rollbacks == [(str(tmp_path.resolve()), "abc123")]


async def test_rollback_engine_failure_is_reported(tmp_path: Path) -> None:
    services = _Services(tmp_path, rollback_error=RuntimeError("mirror is dirty"))
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "rollback", {"root": ".", "target_sha": "abc123"})
    assert result == {"error": "mirror is dirty"}


# ---------------------------------------------------------------------------
# scaffold_new_project (path-driven branch)
# ---------------------------------------------------------------------------


async def test_scaffold_new_project_layout_error_is_reported(tmp_path: Path) -> None:
    services = _Services(tmp_path, project_error=ProjectLayoutError("not a directory"))
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "scaffold_new_project", {"path": str(tmp_path)})
    assert result == {"error": "not a directory"}


async def test_scaffold_new_project_engine_error_result_is_forwarded(tmp_path: Path) -> None:
    services = _Services(tmp_path, project_result={"error": "user cancelled"})
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "scaffold_new_project", {"path": str(tmp_path)})
    assert result == {"error": "user cancelled"}


async def test_scaffold_new_project_reports_unconfirmed_attach_as_warning(
    tmp_path: Path,
) -> None:
    services = _Services(
        tmp_path,
        project_result={
            "path": str(tmp_path),
            "name": "proj",
            "workspace_attached": False,
            "warning": "VS Code did not confirm the folder in time.",
        },
    )
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "scaffold_new_project", {"path": str(tmp_path)})
    assert result == {
        "path": str(tmp_path),
        "name": "proj",
        "scaffolded": True,
        "already_scaffolded": False,
        "workspace_attached": False,
        "warning": "VS Code did not confirm the folder in time.",
    }


# ---------------------------------------------------------------------------
# toolchain_deps
# ---------------------------------------------------------------------------


async def test_toolchain_deps_requires_all_identifying_fields(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    d = _make_dispatcher(tmp_path, services)
    result = await _call(d, "toolchain_deps", {"project_root_path": str(tmp_path), "action": "add"})
    assert result["success"] is False and result["status"] == "failed"
    assert services.deps_tasks == []


async def test_toolchain_deps_forwards_version_and_extra_to_the_sub_agent(tmp_path: Path) -> None:
    services = _Services(tmp_path, deps_result={"status": "completed"})
    d = _make_dispatcher(tmp_path, services)
    result = await _call(
        d,
        "toolchain_deps",
        {
            "project_root_path": str(tmp_path),
            "action": "update",
            "name": "httpx",
            "version": "0.28",
            "kind": "dev",
            "extra": "http",
        },
    )
    assert result == {"success": True, "status": "completed", "message": "update httpx: done."}
    [task] = services.deps_tasks
    assert task["version"] == "0.28"
    assert task["extra"] == "http"
    assert task["kind"] == "dev"
    instructions = str(task["instructions"])
    assert instructions.startswith("Update the `dev` dependency `httpx`")
    assert "at version `0.28`" in instructions
    assert "optional extras group `http`" in instructions


async def test_toolchain_deps_defaults_kind_and_omits_unset_fields(tmp_path: Path) -> None:
    services = _Services(tmp_path, deps_result={"status": "completed", "summary": "ok"})
    d = _make_dispatcher(tmp_path, services)
    await _call(
        d,
        "toolchain_deps",
        {"project_root_path": str(tmp_path), "action": "add", "name": "rich"},
    )
    [task] = services.deps_tasks
    assert task["kind"] == "runtime"
    assert "version" not in task and "extra" not in task


async def test_toolchain_deps_failure_carries_partial_progress(tmp_path: Path) -> None:
    services = _Services(
        tmp_path,
        deps_result={
            "status": "failed",
            "summary": "resolver conflict",
            "commands_run": ["uv add foo"],
            "files_changed": ["pyproject.toml"],
        },
    )
    d = _make_dispatcher(tmp_path, services)
    result = await _call(
        d,
        "toolchain_deps",
        {"project_root_path": str(tmp_path), "action": "add", "name": "foo"},
    )
    assert result == {
        "success": False,
        "status": "failed",
        "message": "resolver conflict",
        "commands_run": ["uv add foo"],
        "files_changed": ["pyproject.toml"],
    }


async def test_toolchain_deps_unusable_sub_agent_result_is_a_failure(tmp_path: Path) -> None:
    services = _Services(tmp_path, deps_result={})
    d = _make_dispatcher(tmp_path, services)
    result = await _call(
        d,
        "toolchain_deps",
        {"project_root_path": str(tmp_path), "action": "remove", "name": "foo"},
    )
    assert result == {
        "success": False,
        "status": "failed",
        "message": "Dependency operation could not be completed.",
    }


# ---------------------------------------------------------------------------
# toolchain_build
# ---------------------------------------------------------------------------

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="build steps run as POSIX shell scripts"
)


def _script(root: Path, step: str, body: str, *, executable: bool = True) -> None:
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / f"{step}.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755 if executable else 0o644)


async def test_toolchain_build_requires_project_path(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "toolchain_build", {"project_path": "  "})
    assert "project_path is required" in str(result["error"])


async def test_toolchain_build_rejects_relative_path_outside_allowed_roots(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    d = _make_dispatcher(proj)
    result = await _call(d, "toolchain_build", {"project_path": "../elsewhere"})
    assert "outside the allowed roots" in str(result["error"])


@_posix_only
async def test_toolchain_build_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    _script(tmp_path, "build", "echo compiling; exit 1")
    _script(tmp_path, "static_analysis", "echo should not run")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "toolchain_build", {"project_path": "."})
    assert result == {
        "success": False,
        "steps": [{"step": "build", "success": False, "log": "compiling\n"}],
    }


@_posix_only
async def test_toolchain_build_unlaunchable_script_fails_the_step(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can execute a non-executable script")
    _script(tmp_path, "build", "echo hi", executable=False)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "toolchain_build", {"project_path": str(tmp_path)})
    assert result["success"] is False
    steps = result["steps"]
    assert isinstance(steps, list) and len(steps) == 1
    assert steps[0]["step"] == "build" and steps[0]["success"] is False
    assert steps[0]["log"]


@_posix_only
async def test_toolchain_build_passes_test_selector_only_to_the_test_step(tmp_path: Path) -> None:
    _script(tmp_path, "build", 'echo "build args: $#"')
    _script(tmp_path, "test", 'echo "test args: $1"')
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d,
        "toolchain_build",
        {
            "project_path": str(tmp_path),
            "static_analysis": False,
            "test_selector": "test_one",
        },
    )
    assert result == {
        "success": True,
        "steps": [
            {"step": "build", "success": True, "log": "build args: 0\n"},
            {"step": "test", "success": True, "log": "test args: test_one\n"},
        ],
    }


# ---------------------------------------------------------------------------
# create_directory / edit_file edge cases
# ---------------------------------------------------------------------------


async def test_create_directory_under_a_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_text("x", encoding="utf-8")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "create_directory", {"path": "plain.txt/sub"})
    assert "error" in result
    assert (tmp_path / "plain.txt").is_file()


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [("", "x", "must not be empty"), ("same", "same", "identical")],
)
async def test_edit_file_rejects_degenerate_replacements(
    tmp_path: Path, old: str, new: str, fragment: str
) -> None:
    (tmp_path / "f.txt").write_text("same content", encoding="utf-8")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "edit_file", {"path": "f.txt", "old_string": old, "new_string": new})
    assert fragment in str(result["error"])
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "same content"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits enforced for the current user",
)
async def test_edit_file_write_failure_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "locked.txt"
    target.write_text("old text", encoding="utf-8")
    target.chmod(0o444)
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d, "edit_file", {"path": "locked.txt", "old_string": "old", "new_string": "new"}
    )
    assert "error" in result
    assert target.read_text(encoding="utf-8") == "old text"
