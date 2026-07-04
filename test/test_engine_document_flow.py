"""Behavior tests for the engine's document accept/review flow.

Replaces the old artifact-promotion integration test. Exercises
``WorkflowEngine._finalize_document`` (the autonomous-auto-accept vs.
interactive-gate behavior that replaced ``__complete_artifact``) and
``WorkflowEngine._run_author_critic_iteration`` (retargeted to operate on a
real file path instead of an artifact ID) directly, the same
``object.__new__(WorkflowEngine)`` + minimal-stub pattern already used by
``test_resume_ledger.py`` — these are private engine methods with no public
surface, so driving them directly is the only way to cover this logic without
standing up the full LLM/transport stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.guided_state import append_new_revision, read_history, read_status
from kodo.project import ProjectLayout
from kodo.runtime import ApprovalResponse, SessionState
from kodo.runtime._checkpoints import RootMirrorManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeGate:
    def __init__(self, action: str = "agree", feedback: str = "") -> None:
        self.action = action
        self.feedback = feedback
        self.calls: list[tuple[str, str | None, str]] = []

    async def fire_approval(
        self, gate_type: str, *, artifact_id: str | None = None, summary: str = ""
    ) -> ApprovalResponse:
        self.calls.append((gate_type, artifact_id, summary))
        return ApprovalResponse(action=self.action, feedback=self.feedback)


class _FakeSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def send(self, envelope: object) -> None:
        self.events.append(envelope)


def _bare_engine(*, project_root: Path, autonomous: bool, gate: _FakeGate) -> object:
    """Construct a WorkflowEngine with only the attributes these methods read."""
    from kodo.runtime import WorkflowEngine

    engine = object.__new__(WorkflowEngine)
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous
    layout = ProjectLayout(project_root)
    layout.init()
    engine._layout = layout
    engine._session = session
    engine._gate = gate
    engine._sink = _FakeSink()
    return engine


def _seed_revision(project_root: Path, rel_path: str, *, sha: str = "deadbeef") -> Path:
    doc = project_root / rel_path
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("content", encoding="utf-8")
    append_new_revision(
        doc,
        project_root,
        commit_hash=sha,
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )
    return doc


# ---------------------------------------------------------------------------
# _finalize_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_document_autonomous_mode_auto_accepts(tmp_path: Path) -> None:
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-1")
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)

    await engine._finalize_document("specs/architecture.md")

    assert gate.calls == []  # never consulted in autonomous mode
    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "accepted"]
    assert history[-1]["commit_hash"] == "sha-1"


@pytest.mark.asyncio
async def test_finalize_document_interactive_agree_records_approval_then_accepted(
    tmp_path: Path,
) -> None:
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-2")
    gate = _FakeGate(action="agree")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)

    await engine._finalize_document("specs/architecture.md")

    assert len(gate.calls) == 1
    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "review_result", "accepted"]
    assert history[1]["decision"] == "approve"
    assert history[-1]["commit_hash"] == "sha-2"


@pytest.mark.asyncio
async def test_finalize_document_interactive_feedback_records_rejection_only(
    tmp_path: Path,
) -> None:
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    gate = _FakeGate(action="feedback", feedback="needs a North Star")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)

    await engine._finalize_document("specs/architecture.md")

    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "review_result"]
    assert history[-1]["decision"] == "reject"
    assert history[-1]["comment"] == "needs a North Star"
    status = read_status(doc, tmp_path)
    assert status is not None and status["status"] == "needs_revision"


# ---------------------------------------------------------------------------
# _run_author_critic_iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_author_critic_iteration_uses_authors_reported_primary_path(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    engine._assert_can_spawn = lambda *a, **k: None

    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-3")

    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        calls.append((name, task_input))
        if name == "architect":
            return {"primary_path": "specs/architecture.md", "paths": ["specs/architecture.md"]}
        # critic round: accept the document it was asked to review.
        from kodo.guided_state import append_feedback

        append_feedback(
            doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
        )
        return {"verdict": "accepted", "concerns": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_author_critic_iteration(
        "guide", "architect", "architect_critic", "", {}, "Produce the architecture.", False
    )

    assert result["path"] == "specs/architecture.md"
    assert result["status"] == "pending_acceptance"
    assert calls[0][0] == "architect"
    assert calls[1][0] == "architect_critic"
    # The critic was told to review the author's reported path, not asked to
    # invent its own.
    assert calls[1][1]["input_paths"] == {"target": "specs/architecture.md"}


@pytest.mark.asyncio
async def test_run_author_critic_iteration_revision_round_passes_for_revision_path(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    engine._assert_can_spawn = lambda *a, **k: None
    _seed_revision(tmp_path, "specs/architecture.md")

    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        calls.append((name, task_input))
        if name == "architect":
            return {"primary_path": "specs/architecture.md", "paths": ["specs/architecture.md"]}
        return {"verdict": "rejected", "concerns": [{"kind": "gap", "description": "x"}]}

    engine._spawn_subagent = _fake_spawn

    await engine._run_author_critic_iteration(
        "guide",
        "architect",
        "architect_critic",
        "specs/architecture.md",
        {"feedback": "specs/architecture.md"},
        "Revise per the critic's concerns.",
        True,
    )

    author_task = calls[0][1]
    assert author_task["for_revision_path"] == "specs/architecture.md"


# ---------------------------------------------------------------------------
# Checkpointing is no longer gated to Problem Solver
# ---------------------------------------------------------------------------


def test_checkpoint_enabled_regardless_of_workflow_mode() -> None:
    """Guided mode now drives the same shadow-git mirror Problem Solver does.

    There is no longer a separate Guided checkpoint system to collide with,
    so per-tool-call checkpointing must run unconditionally.
    """
    from kodo.runtime._engine._checkpointing import CheckpointCoordinator

    coordinator = object.__new__(CheckpointCoordinator)
    for mode in ("guided", "problem_solving"):
        session = SessionState()
        session.workflow_mode = mode
        session.effective_workflow_mode = mode
        assert coordinator._enabled() is True


# ---------------------------------------------------------------------------
# Mirror integration: a real Guided-mode commit also records a new_revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guided_filesystem_write_earns_both_a_commit_and_a_new_revision(
    tmp_path: Path,
) -> None:
    """The post-dispatch hook's two effects, composed at the primitive level.

    Mirrors what ``CheckpointCoordinator.record_guided_revision`` does after a
    real ``filesystem``/``edit_file`` call: commit the mirror, then append a
    ``new_revision`` entry carrying that exact commit's sha.
    """
    layout = ProjectLayout(tmp_path)
    layout.init()
    doc = tmp_path / "specs" / "architecture.md"
    doc.parent.mkdir(parents=True, exist_ok=True)

    mirrors = RootMirrorManager([tmp_path])
    await mirrors.prepare(doc)
    doc.write_text("# Architecture", encoding="utf-8")
    checkpoint = await mirrors.commit_for_path(doc, "filesystem create_file: specs/architecture.md")
    assert checkpoint is not None

    append_new_revision(
        doc,
        tmp_path,
        commit_hash=checkpoint.sha,
        author="architect",
        tool="filesystem",
        summary="create",
        workflow="guided",
    )

    history = read_history(doc, tmp_path)
    assert len(history) == 1
    assert history[0]["commit_hash"] == checkpoint.sha
    assert history[0]["workflow"] == "guided"

    # The jsonl evolution log itself lives under .kodo/, which the mirror
    # already excludes — it must never show up in the mirror's own commit.
    import subprocess

    tracked = subprocess.run(
        [
            "git",
            f"--git-dir={layout.checkpoints_dir / '.git'}",
            f"--work-tree={tmp_path}",
            "ls-files",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert not any(".kodo" in line for line in tracked)
    assert "specs/architecture.md" in tracked
