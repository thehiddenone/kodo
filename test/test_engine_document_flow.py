"""Behavior tests for the engine's document accept/review flow.

Replaces the old artifact-promotion integration test. Exercises
``WorkflowEngine._finalize_document`` (the autonomous-auto-accept vs.
interactive-gate behavior that replaced ``__complete_artifact``) and
``WorkflowEngine._run_review_loop`` / ``._record_review_verdict`` (the
engine-driven author/critic loop and the verdict recording that replaced the
``document_feedback`` tool) directly, the same
``object.__new__(WorkflowEngine)`` + minimal-stub pattern already used by
``test_resume_ledger.py`` — these are private engine methods with no public
surface, so driving them directly is the only way to cover this logic without
standing up the full LLM/transport stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.guided_state import append_feedback, append_new_revision, read_history, read_status
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
    """Construct a WorkflowEngine with only the attributes these methods read.

    ``project_root`` is bound as the single root named ``"proj"`` — callers
    must folder-prefix every path they pass in
    (``"proj/specs/architecture.md"``), matching the logical-path convention
    ``_make_resolver``'s ``LogicalPathResolver`` uses in production.
    """
    from kodo.project import SessionWorkspace
    from kodo.runtime import WorkflowEngine
    from kodo.state import TransientStore

    engine = object.__new__(WorkflowEngine)
    session = SessionState()
    session.autonomous = autonomous
    session.effective_autonomous = autonomous
    ProjectLayout(project_root).init()
    engine._session = session
    engine._gate = gate
    engine._sink = _FakeSink()
    engine._orch_session_id = "sess-test"
    # Unattached — never touches disk; workspace_locked_paths defaults empty,
    # which makes _is_workspace_connected() vacuously True (see
    # kodo.state.workspace_shape_compatible), so _root_paths() just reads the
    # live folder map below.
    engine._transient = TransientStore(project_root / ".kodo-transient-unused")
    engine._session_workspace = SessionWorkspace(
        physical_root=project_root, folders={"proj": project_root}
    )
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

    await engine._finalize_document("proj/specs/architecture.md")

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

    await engine._finalize_document("proj/specs/architecture.md")

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

    await engine._finalize_document("proj/specs/architecture.md")

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
async def test_review_loop_uses_authors_reported_primary_path(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-3")

    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        calls.append((name, task_input))
        if name == "architect":
            return {
                "primary_path": "proj/specs/architecture.md",
                "paths": ["proj/specs/architecture.md"],
            }
        append_feedback(
            doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
        )
        return {"path": "proj/specs/architecture.md", "accept": True, "concerns": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["primary_path"] == "proj/specs/architecture.md"
    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 1
    assert calls[0][0] == "architect"
    assert calls[1][0] == "architect_critic"
    # The critic was told to review the author's reported path, not asked to
    # invent its own.
    assert calls[1][1]["input_paths"] == {"target": "proj/specs/architecture.md"}


@pytest.mark.asyncio
async def test_review_loop_folds_concerns_into_next_rounds_instructions(tmp_path: Path) -> None:
    """The caller writes its instructions once; the engine — not the caller —
    appends each round's outstanding concerns and points ``for_revision_path``
    at the file, which is the contract every author's input schema describes."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md")

    calls: list[tuple[str, dict[str, object]]] = []
    concern_counts = iter([3, 2, 0])

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        calls.append((name, task_input))
        if name == "architect":
            return {
                "primary_path": "proj/specs/architecture.md",
                "paths": ["proj/specs/architecture.md"],
            }
        n = next(concern_counts)
        concerns = [
            {"kind": "gap", "description": f"missing {i}", "first_line": i, "last_line": i}
            for i in range(n)
        ]
        append_feedback(
            doc,
            tmp_path,
            reviewer="architect_critic",
            accept=not concerns,
            concerns=concerns,
            summary="",
        )
        return {
            "path": "proj/specs/architecture.md",
            "accept": not concerns,
            "concerns": concerns,
        }

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 3

    author_rounds = [task for name, task in calls if name == "architect"]
    # Round 1 gets the caller's brief untouched, with no revision target.
    assert author_rounds[0]["instructions"] == "Produce the architecture."
    assert "for_revision_path" not in author_rounds[0]
    # Round 2 keeps the brief and adds the concerns plus the file to revise.
    assert author_rounds[1]["instructions"].startswith("Produce the architecture.")
    assert "## Concerns from review" in author_rounds[1]["instructions"]
    assert "missing 0" in author_rounds[1]["instructions"]
    assert author_rounds[1]["for_revision_path"] == "proj/specs/architecture.md"


@pytest.mark.asyncio
async def test_review_loop_stops_when_concerns_stop_decreasing(tmp_path: Path) -> None:
    """Rounds that stop reducing findings are the signal to stop spending
    budget, so the loop reports ``not_converging`` well short of ``max_rounds``
    rather than orbiting until the cap."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md")

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        if name == "architect":
            return {
                "primary_path": "proj/specs/architecture.md",
                "paths": ["proj/specs/architecture.md"],
            }
        concerns = [{"kind": "gap", "description": "same finding, every round"}]
        append_feedback(
            doc,
            tmp_path,
            reviewer="architect_critic",
            accept=False,
            concerns=concerns,
            summary="",
        )
        return {"path": "proj/specs/architecture.md", "accept": False, "concerns": concerns}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 5
    )

    assert result["review"]["outcome"] == "not_converging"
    assert result["review"]["rounds"] == 2  # stopped as soon as the count failed to drop
    assert result["review"]["status"] == "needs_revision"


@pytest.mark.asyncio
async def test_review_loop_reports_max_rounds_when_budget_runs_out(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    counts = iter([4, 3, 2, 1, 0])

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        if name == "architect":
            return {
                "primary_path": "proj/specs/architecture.md",
                "paths": ["proj/specs/architecture.md"],
            }
        concerns = [{"kind": "gap", "description": f"c{i}"} for i in range(next(counts))]
        append_feedback(
            doc,
            tmp_path,
            reviewer="architect_critic",
            accept=False,
            concerns=concerns,
            summary="",
        )
        return {"path": "proj/specs/architecture.md", "accept": False, "concerns": concerns}

    engine._spawn_subagent = _fake_spawn

    # Concerns shrink every round, so only the caller's budget stops the loop.
    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 2
    )

    assert result["review"]["outcome"] == "max_rounds"
    assert result["review"]["rounds"] == 2


@pytest.mark.asyncio
async def test_review_loop_reports_not_reviewed_when_author_names_no_file(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    spawned: list[str] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        spawned.append(name)
        return {"paths": [], "summary": "nothing written"}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, None
    )

    assert result["review"]["outcome"] == "not_reviewed"
    assert spawned == ["architect"]  # the critic is never spawned against nothing


@pytest.mark.asyncio
async def test_review_loop_stops_on_an_escalation_without_spawning_the_critic(
    tmp_path: Path,
) -> None:
    """An author that returns a non-empty ``reason`` is blocked on something no
    revision fixes, so the loop ends there: the critic is never spawned, no
    further round is spent, and the escalation rides back on the result."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    spawned: list[str] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        spawned.append(name)
        return {
            "summary": "The Narrative does not say which system owns settlement.",
            "reason": "insufficient_narrative_for_decomposition",
            "options": ["Fold it into LEDGER", "Give it its own codename"],
        }

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 5
    )

    assert result["review"]["outcome"] == "escalated"
    assert result["review"]["rounds"] == 1
    assert spawned == ["architect"]
    # The caller reads reason/summary/options straight off the result.
    assert result["reason"] == "insufficient_narrative_for_decomposition"
    assert result["options"] == ["Fold it into LEDGER", "Give it its own codename"]


@pytest.mark.asyncio
async def test_review_loop_treats_an_empty_reason_as_a_normal_result(tmp_path: Path) -> None:
    """``reason`` is optional, and ``normalize_output`` backfills a missing
    required field with ``""`` — so emptiness, not presence, is what marks a
    result as *not* an escalation."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    spawned: list[str] = []

    async def _fake_spawn(name: str, task_input: dict[str, object]) -> dict[str, object]:
        spawned.append(name)
        if name == "architect":
            return {
                "primary_path": "proj/specs/architecture.md",
                "paths": ["proj/specs/architecture.md"],
                "reason": "   ",
            }
        append_feedback(
            doc, tmp_path, reviewer="architect_critic", accept=True, concerns=[], summary="ok"
        )
        return {"path": "proj/specs/architecture.md", "accept": True, "concerns": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert spawned == ["architect", "architect_critic"]


# ---------------------------------------------------------------------------
# _record_review_verdict — the engine-side replacement for document_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_review_verdict_writes_feedback_entry(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_review_verdict(
        "architect_critic",
        {
            "path": "proj/specs/architecture.md",
            "accept": False,
            "concerns": [{"kind": "gap", "description": "missing section"}],
            "summary": "1 concern",
        },
    )

    status = read_status(tmp_path / "specs" / "architecture.md", tmp_path)
    assert status is not None
    assert status["status"] == "needs_revision"
    assert status["reviewer"] == "architect_critic"
    assert status["concerns"] == [{"kind": "gap", "description": "missing section"}]


@pytest.mark.asyncio
async def test_record_review_verdict_accept_drives_the_acceptance_flow(tmp_path: Path) -> None:
    """An accepting verdict must reach ``_finalize_document`` — the half of the
    retired ``document_feedback`` tool that ran in ``_finalize_tool_result``."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_review_verdict(
        "architect_critic",
        {"path": "proj/specs/architecture.md", "accept": True, "concerns": [], "summary": "ok"},
    )

    # Autonomous mode auto-accepts, so the log ends on the acceptance marker.
    history = read_history(tmp_path / "specs" / "architecture.md", tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "feedback", "accepted"]


@pytest.mark.asyncio
async def test_record_review_verdict_ignores_a_verdict_with_no_path(tmp_path: Path) -> None:
    """A malformed verdict is dropped, not raised: the loop reads the file's log
    for the real status, and an unrecorded verdict simply leaves it unsettled."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_review_verdict("architect_critic", {"accept": True, "concerns": []})

    history = read_history(tmp_path / "specs" / "architecture.md", tmp_path)
    assert [e["type"] for e in history] == ["new_revision"]


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
