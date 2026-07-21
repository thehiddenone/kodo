"""Tests for the create_file/edit_file review gate: the smart-mode heuristic
(``kodo.tools._edit_review.should_review_edit``), the shared diff-preview
helper (``kodo.tools._edit_file.compute_new_content``), and the dispatcher
integration (``ToolDispatcher.__edit_review_gate``, WS_PROTOCOL.md §6.5b).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.runtime._gates import EditReviewFeedbackEntry
from kodo.security import SecurityLayer
from kodo.tools import RootPath, compute_new_content, should_review_edit

# ---------------------------------------------------------------------------
# should_review_edit
# ---------------------------------------------------------------------------

_ROOTS = (RootPath(name="proj", path="/ws/proj"),)


def test_should_review_edit_true_for_src_segment() -> None:
    assert should_review_edit(Path("/ws/proj/src/foo.py"), _ROOTS) is True


def test_should_review_edit_false_outside_src() -> None:
    assert should_review_edit(Path("/ws/proj/README.md"), _ROOTS) is False


def test_should_review_edit_false_for_substring_not_segment() -> None:
    """`mysrc/foo.py` must NOT match — 'src' has to be a whole path segment."""
    assert should_review_edit(Path("/ws/proj/mysrc/foo.py"), _ROOTS) is False


def test_should_review_edit_nested_src_segment_matches() -> None:
    assert should_review_edit(Path("/ws/proj/backend/src/main.py"), _ROOTS) is True


def test_should_review_edit_matches_relative_to_root_not_full_path() -> None:
    """A checkout that happens to sit inside a directory coincidentally named
    'src' above the project root must not false-positive on every file."""
    roots = (RootPath(name="proj", path="/home/dev/src/proj"),)
    assert should_review_edit(Path("/home/dev/src/proj/README.md"), roots) is False


def test_should_review_edit_falls_back_to_absolute_path_when_no_root_matches() -> None:
    assert should_review_edit(Path("/elsewhere/src/foo.py"), ()) is True
    assert should_review_edit(Path("/elsewhere/lib/foo.py"), ()) is False


# ---------------------------------------------------------------------------
# compute_new_content
# ---------------------------------------------------------------------------


def test_compute_new_content_replaces_unique_match() -> None:
    assert compute_new_content("f.py", "a\nb\nc\n", "b\n", "B\n") == "a\nB\nc\n"


def test_compute_new_content_raises_on_zero_matches() -> None:
    with pytest.raises(ValueError, match="not found"):
        compute_new_content("f.py", "a\nb\nc\n", "zzz", "B")


def test_compute_new_content_raises_on_ambiguous_match() -> None:
    with pytest.raises(ValueError, match="not unique"):
        compute_new_content("f.py", "a\nb\na\n", "a\n", "A\n")


# ---------------------------------------------------------------------------
# Dispatcher integration: ToolDispatcher.__edit_review_gate
# ---------------------------------------------------------------------------


class _FakeEditReviewGate:
    """Satisfies GateLike; only fire_edit_review is expected to be called —
    command_control is "smart" in every _FakeSession here, which auto-allows
    both create_file (LOW) and edit_file (MODERATE), so fire_permission
    should never fire and asserts loudly if it does."""

    def __init__(self, action: str = "approve", feedback: tuple[object, ...] = ()) -> None:
        self.action = action
        self.feedback = feedback
        self.fired: list[dict[str, object]] = []

    async def fire_permission(self, **kwargs: object):  # noqa: ANN201
        raise AssertionError("fire_permission should not fire: command_control is smart")

    async def fire_questions(self, questions, tool_call_id=""):  # noqa: ANN001, ANN201
        raise AssertionError("not used")

    async def fire_approval(self, gate_type, **kwargs):  # noqa: ANN001, ANN201
        raise AssertionError("not used")

    async def fire_edit_review(self, **kwargs: object):  # noqa: ANN201
        self.fired.append(kwargs)
        action, feedback = self.action, self.feedback

        class _Resp:
            pass

        _Resp.action = action  # type: ignore[attr-defined]
        _Resp.feedback = feedback  # type: ignore[attr-defined]
        return _Resp()


class _FakeSession:
    def __init__(self, edit_control: str = "smart") -> None:
        self.phase = "running"
        self.effective_autonomous = False
        self.command_control = "smart"
        self.security_rules: frozenset[tuple[str, str]] = frozenset()
        self.security_path_rules: frozenset[tuple[str, str]] = frozenset()
        self.edit_control = edit_control


class _FakeWorkspaceServices:
    """Satisfies just enough of EngineServices for the has_workspace/root_paths
    gate — this test never triggers any other engine-side operation."""

    def __init__(self, *, has_workspace: bool, root_paths: tuple[RootPath, ...]) -> None:
        self._has_workspace = has_workspace
        self._root_paths = root_paths

    def has_workspace(self) -> bool:
        return self._has_workspace

    def root_paths(self) -> tuple[RootPath, ...]:
        return self._root_paths

    def project_root(self) -> Path | None:
        return None


def _make_dispatcher(gate: _FakeEditReviewGate, session: _FakeSession, tmp_path: Path):  # noqa: ANN201
    from kodo.tools import ProjectPathResolver, ToolDispatcher

    return ToolDispatcher(
        resolver=ProjectPathResolver(tmp_path),
        gate=gate,  # type: ignore[arg-type]
        security=SecurityLayer(),
        session=session,  # type: ignore[arg-type]
        services=_FakeWorkspaceServices(
            has_workspace=True, root_paths=(RootPath(name="proj", path=str(tmp_path)),)
        ),  # type: ignore[arg-type]
        agent_name="tester",
        session_id="s1",
    )


@pytest.mark.asyncio
async def test_allow_all_skips_gate_and_creates_file(tmp_path: Path) -> None:
    gate = _FakeEditReviewGate()
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="allow_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result["status"] == "created"
    assert (tmp_path / "foo.py").read_text() == "x = 1\n"
    assert gate.fired == []


@pytest.mark.asyncio
async def test_review_all_fires_for_create_file_approve(tmp_path: Path) -> None:
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result["status"] == "created"
    assert (tmp_path / "foo.py").read_text() == "x = 1\n"
    assert len(gate.fired) == 1
    assert gate.fired[0]["mode"] == "new_file"
    assert gate.fired[0]["old_content"] == ""
    assert gate.fired[0]["new_content"] == "x = 1\n"


@pytest.mark.asyncio
async def test_review_all_reject_blocks_write(tmp_path: Path) -> None:
    gate = _FakeEditReviewGate(action="reject")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result == {"status": "rejected", "path": "foo.py"}
    assert not (tmp_path / "foo.py").exists()


@pytest.mark.asyncio
async def test_review_all_reject_with_feedback(tmp_path: Path) -> None:
    feedback = (
        EditReviewFeedbackEntry(
            line_from=1, line_to=1, targeted_code="x = 1", feedback="rename x"
        ),
    )
    gate = _FakeEditReviewGate(action="reject", feedback=feedback)
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result["status"] == "rejected_with_feedback"
    assert result["feedback"] == [
        {
            "general_feedback": False,
            "line_from": 1,
            "line_to": 1,
            "targeted_code": "x = 1",
            "feedback": "rename x",
        }
    ]
    assert not (tmp_path / "foo.py").exists()


@pytest.mark.asyncio
async def test_review_all_reject_with_general_feedback(tmp_path: Path) -> None:
    """A note added with nothing selected (`general_feedback=True`) is
    rendered without any line_from/line_to/targeted_code keys at all — not
    as null/placeholder values."""
    feedback = (
        EditReviewFeedbackEntry(feedback="add a module docstring", general_feedback=True),
        EditReviewFeedbackEntry(
            line_from=1, line_to=1, targeted_code="x = 1", feedback="rename x"
        ),
    )
    gate = _FakeEditReviewGate(action="reject", feedback=feedback)
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result["status"] == "rejected_with_feedback"
    assert result["feedback"] == [
        {"general_feedback": True, "feedback": "add a module docstring"},
        {
            "general_feedback": False,
            "line_from": 1,
            "line_to": 1,
            "targeted_code": "x = 1",
            "feedback": "rename x",
        },
    ]
    assert not (tmp_path / "foo.py").exists()


@pytest.mark.asyncio
async def test_smart_mode_fires_for_src_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="smart"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "src/foo.py", "content": "x = 1\n"}, "tu_1"
        )
    )
    assert result["status"] == "created"
    assert len(gate.fired) == 1


@pytest.mark.asyncio
async def test_smart_mode_skips_outside_src(tmp_path: Path) -> None:
    gate = _FakeEditReviewGate(action="reject")  # would reject if it fired — must not fire
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="smart"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "README.md", "content": "hi\n"}, "tu_1"
        )
    )
    assert result["status"] == "created"
    assert gate.fired == []


@pytest.mark.asyncio
async def test_review_all_skips_gate_when_file_already_exists(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("old\n")
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file", {"intent": "add", "path": "foo.py", "content": "new\n"}, "tu_1"
        )
    )
    assert "error" in result
    assert "already exists" in result["error"]
    assert gate.fired == []
    assert (tmp_path / "foo.py").read_text() == "old\n"


@pytest.mark.asyncio
async def test_review_all_skips_gate_when_old_string_not_unique(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("a\na\n")
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"intent": "fix", "path": "foo.py", "old_string": "a\n", "new_string": "b\n"},
            "tu_1",
        )
    )
    assert "error" in result
    assert "not unique" in result["error"]
    assert gate.fired == []


@pytest.mark.asyncio
async def test_review_all_fires_for_edit_file_modification(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("a\nb\nc\n")
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "edit_file",
            {"intent": "fix", "path": "foo.py", "old_string": "b\n", "new_string": "B\n"},
            "tu_1",
        )
    )
    assert result["status"] == "edited"
    assert (tmp_path / "foo.py").read_text() == "a\nB\nc\n"
    assert len(gate.fired) == 1
    assert gate.fired[0]["mode"] == "modification"
    assert gate.fired[0]["old_content"] == "a\nb\nc\n"
    assert gate.fired[0]["new_content"] == "a\nB\nc\n"


@pytest.mark.asyncio
async def test_review_all_skips_gate_for_out_of_workspace_path(tmp_path: Path) -> None:
    """An out-of-workspace path hard-fails via PathResolver's PermissionError,
    exactly like today — no gate, no prompt, from either gate."""
    gate = _FakeEditReviewGate(action="approve")
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file",
            {"intent": "add", "path": "/definitely/outside/foo.py", "content": "x\n"},
            "tu_1",
        )
    )
    assert "error" in result
    assert "outside the project root" in result["error"]
    assert gate.fired == []


@pytest.mark.asyncio
async def test_temporary_true_skips_gate_even_in_review_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    gate = _FakeEditReviewGate(action="reject")  # would reject if it fired — must not fire
    dispatcher = _make_dispatcher(gate, _FakeSession(edit_control="review_all"), tmp_path)

    result = json.loads(
        await dispatcher.dispatch(
            "create_file",
            {"intent": "add", "path": "scratch.py", "content": "x\n", "temporary": True},
            "tu_1",
        )
    )
    assert result["status"] == "created"
    assert gate.fired == []
