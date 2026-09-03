"""Behavior tests for the engine's document accept/review flow.

Replaces the old artifact-promotion integration test. Exercises
``WorkflowEngine._finalize_document`` (the autonomous-auto-accept vs.
interactive-gate behavior that replaced ``__complete_artifact``) and
``WorkflowEngine._run_review_loop`` / ``._record_findings`` (the engine-driven
author/critic loop and the findings recording behind it, doc/FINDINGS.md)
directly, the same
``object.__new__(WorkflowEngine)`` + minimal-stub pattern already used by
``test_resume_ledger.py`` — these are private engine methods with no public
surface, so driving them directly is the only way to cover this logic without
standing up the full LLM/transport stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.findings import read_findings
from kodo.guided_state import append_new_revision, read_history
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
    # Attached to a real (throwaway) session directory: the findings backlog is
    # session-scoped, so ``_findings_dir()`` needs a session to point at.
    engine._transient = TransientStore(project_root / ".kodo-transient")
    engine._transient.attach_session("sess-test", resumed=False)
    engine._session_workspace = SessionWorkspace(
        physical_root=project_root, folders={"proj": project_root}
    )
    return engine


def _findings_dir(project_root: Path) -> Path:
    """Where ``_bare_engine``'s attached session keeps its findings backlog."""
    return project_root / ".kodo-transient" / "sessions" / "sess-test" / "findings"


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
    # No review_result: that entry means "the user decided at the gate", and no
    # gate fired — writing one would fabricate a decision nobody made.
    assert [e["type"] for e in history] == ["new_revision", "accepted"]
    assert history[-1]["commit_hash"] == "sha-1"


@pytest.mark.asyncio
async def test_finalize_document_allow_all_edit_control_also_skips_the_gate(
    tmp_path: Path,
) -> None:
    """Edit Control *Allow All* already means "don't stop me for file changes";
    stopping for a document sign-off in that posture contradicted every other
    gate (doc/FINDINGS.md §5)."""
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-allow")
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)
    engine._session.edit_control = "allow_all"

    await engine._finalize_document("proj/specs/architecture.md")

    assert gate.calls == []
    assert [e["type"] for e in read_history(doc, tmp_path)] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_finalize_document_other_edit_control_settings_still_ask(tmp_path: Path) -> None:
    """Only *allow_all* shortcuts — ``smart`` and ``review_all`` still gate."""
    for setting in ("smart", "review_all"):
        root = tmp_path / setting
        root.mkdir()
        doc = _seed_revision(root, "specs/architecture.md")
        gate = _FakeGate(action="agree")
        engine = _bare_engine(project_root=root, autonomous=False, gate=gate)
        engine._session.edit_control = setting

        await engine._finalize_document("proj/specs/architecture.md")

        assert len(gate.calls) == 1, setting
        assert [e["type"] for e in read_history(doc, root)] == [
            "new_revision",
            "review_result",
            "accepted",
        ], setting


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
async def test_finalize_document_rejection_mints_the_users_feedback_as_a_finding(
    tmp_path: Path,
) -> None:
    """The user's objection reaches the author through the same backlog as every
    critic finding — one channel, one procedure (doc/FINDINGS.md §3)."""
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    gate = _FakeGate(action="feedback", feedback="needs a North Star")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)

    await engine._finalize_document("proj/specs/architecture.md")

    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "review_result"]
    assert history[-1]["decision"] == "reject"
    assert history[-1]["comment"] == "needs a North Star"

    findings = read_findings(_findings_dir(tmp_path), "proj/specs/architecture.md")
    assert len(findings) == 1
    assert findings[0]["state"] == "outstanding"
    assert findings[0]["reported_by"] == "user"
    assert findings[0]["description"] == "needs a North Star"
    # …and the document is back to needing revision because of it.
    assert await engine._document_status("proj/specs/architecture.md") == "needs_revision"


@pytest.mark.asyncio
async def test_finalize_document_rejection_with_no_comment_mints_nothing(tmp_path: Path) -> None:
    """An empty rejection has nothing actionable in it; minting a blank finding
    would give the author an item it cannot possibly close."""
    _seed_revision(tmp_path, "specs/architecture.md")
    engine = _bare_engine(
        project_root=tmp_path, autonomous=False, gate=_FakeGate(action="feedback", feedback="  ")
    )

    await engine._finalize_document("proj/specs/architecture.md")

    assert read_findings(_findings_dir(tmp_path), "proj/specs/architecture.md") == []


# ---------------------------------------------------------------------------
# _run_review_loop
# ---------------------------------------------------------------------------


def _author_result(path: str = "proj/specs/architecture.md") -> dict[str, object]:
    return {"primary_path": path, "paths": [path], "summary": "wrote it"}


@pytest.mark.asyncio
async def test_review_loop_uses_authors_reported_primary_path(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md", sha="sha-3")

    calls: list[tuple[str, dict[str, object], str]] = []

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        calls.append((name, task_input, findings_path))
        if name == "architect":
            return _author_result()
        await engine._record_findings(
            "architect_critic", {"path": "proj/specs/architecture.md", "findings": []}
        )
        return {"path": "proj/specs/architecture.md", "findings": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["primary_path"] == "proj/specs/architecture.md"
    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 1
    assert result["review"]["outstanding"] == 0
    assert calls[0][0] == "architect"
    assert calls[1][0] == "architect_critic"
    # The critic was told to review the author's reported path, not asked to
    # invent its own — and that same path scopes its get_findings.
    assert calls[1][1]["input_paths"] == {"target": "proj/specs/architecture.md"}
    assert calls[1][2] == "proj/specs/architecture.md"


@pytest.mark.asyncio
async def test_review_loop_resends_identical_instructions_every_round(tmp_path: Path) -> None:
    """Findings are no longer rendered into the author's task: every round sends
    the caller's brief unchanged, and the author reads the backlog through
    ``get_findings`` instead. That is what makes a first pass and a tenth
    identical for the agent (doc/FINDINGS.md §4)."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    findings_dir = _findings_dir(tmp_path)
    doc_path = "proj/specs/architecture.md"

    calls: list[tuple[str, dict[str, object], str]] = []
    rounds = iter(
        [
            # Round 1: three new findings.
            [{"kind": "gap", "description": f"missing {i}"} for i in range(3)],
            # Round 2: close two of them.
            [{"id": "F1", "state": "fixed"}, {"id": "F2", "state": "fixed"}],
            # Round 3: close the last.
            [{"id": "F3", "state": "fixed"}],
        ]
    )

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        calls.append((name, dict(task_input), findings_path))
        if name == "architect":
            return _author_result()
        updates = next(rounds)
        await engine._record_findings("architect_critic", {"path": doc_path, "findings": updates})
        return {"path": doc_path, "findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 3

    author_rounds = [task for name, task, _ in calls if name == "architect"]
    assert [t["instructions"] for t in author_rounds] == ["Produce the architecture."] * 3
    # Round 1 has no file yet, so no revision target and no findings scope.
    assert "for_revision_path" not in author_rounds[0]
    assert calls[0][2] == ""
    # From round 2 the author is pointed at the file, and its get_findings is
    # scoped to it — which is the only place the findings reach it.
    assert author_rounds[1]["for_revision_path"] == doc_path
    assert [scope for name, _, scope in calls if name == "architect"][1:] == [doc_path] * 2
    # Nothing about the findings themselves leaked into the task.
    assert all("missing" not in str(task) for task in author_rounds)

    # Every finding really did end up closed, by id.
    assert {f["id"]: f["state"] for f in read_findings(findings_dir, doc_path)} == {
        "F1": "fixed",
        "F2": "fixed",
        "F3": "fixed",
    }


@pytest.mark.asyncio
async def test_review_loop_stops_when_a_round_closes_and_opens_nothing(tmp_path: Path) -> None:
    """The stall detector: a round that neither closes nor opens anything is
    exact no-progress, so the loop reports ``not_converging`` well short of
    ``max_rounds`` rather than orbiting until the cap."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"
    first = True

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        nonlocal first
        if name == "architect":
            return _author_result()
        # Round 1 raises one finding; every later round says nothing at all —
        # the finding stays outstanding, and nothing moves.
        updates = [{"kind": "gap", "description": "same finding, every round"}] if first else []
        first = False
        await engine._record_findings("architect_critic", {"path": doc_path, "findings": updates})
        return {"path": doc_path, "findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 5
    )

    assert result["review"]["outcome"] == "not_converging"
    assert result["review"]["rounds"] == 2  # stopped as soon as a round did nothing
    assert result["review"]["status"] == "needs_revision"
    assert result["review"]["outstanding"] == 1


@pytest.mark.asyncio
async def test_review_loop_keeps_going_while_a_round_fixes_and_finds_in_equal_numbers(
    tmp_path: Path,
) -> None:
    """The case the old count heuristic got wrong: closing two and finding two
    is real progress, not a stall, and the loop must not stop on it."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"
    rounds = iter(
        [
            [{"kind": "gap", "description": "a"}, {"kind": "gap", "description": "b"}],
            [
                {"id": "F1", "state": "fixed"},
                {"id": "F2", "state": "fixed"},
                {"kind": "gap", "description": "c"},
                {"kind": "gap", "description": "d"},
            ],
            [{"id": "F3", "state": "fixed"}, {"id": "F4", "state": "fixed"}],
        ]
    )

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        updates = next(rounds)
        await engine._record_findings("architect_critic", {"path": doc_path, "findings": updates})
        return {"path": doc_path, "findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 5
    )

    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 3


@pytest.mark.asyncio
async def test_review_loop_reports_max_rounds_when_budget_runs_out(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"
    counter = iter(range(100))

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        # Every round finds something new, so progress never stalls and only
        # the caller's budget can stop the loop.
        updates = [{"kind": "gap", "description": f"c{next(counter)}"}]
        await engine._record_findings("architect_critic", {"path": doc_path, "findings": updates})
        return {"path": doc_path, "findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 2
    )

    assert result["review"]["outcome"] == "max_rounds"
    assert result["review"]["rounds"] == 2
    assert result["review"]["outstanding"] == 2


@pytest.mark.asyncio
async def test_review_loop_reports_not_reviewed_when_author_names_no_file(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    spawned: list[str] = []

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
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

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
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
    _seed_revision(tmp_path, "specs/architecture.md")
    spawned: list[str] = []

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        spawned.append(name)
        if name == "architect":
            return {**_author_result(), "reason": "   "}
        await engine._record_findings(
            "architect_critic", {"path": "proj/specs/architecture.md", "findings": []}
        )
        return {"path": "proj/specs/architecture.md", "findings": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert spawned == ["architect", "architect_critic"]


# ---------------------------------------------------------------------------
# _record_findings — the engine-side half of a critic round
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_findings_opens_new_findings_and_leaves_the_document_unsettled(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_findings(
        "architect_critic",
        {
            "path": "proj/specs/architecture.md",
            "findings": [
                {"kind": "gap", "description": "missing section", "first_line": 4, "last_line": 6}
            ],
            "summary": "1 finding",
        },
    )

    findings = read_findings(_findings_dir(tmp_path), "proj/specs/architecture.md")
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"
    assert findings[0]["kind"] == "gap"
    assert findings[0]["state"] == "outstanding"
    assert findings[0]["reported_by"] == "architect_critic"
    assert await engine._document_status("proj/specs/architecture.md") == "needs_revision"
    # Nothing outstanding was resolved, so acceptance was not driven.
    assert [e["type"] for e in read_history(tmp_path / "specs" / "architecture.md", tmp_path)] == [
        "new_revision"
    ]


@pytest.mark.asyncio
async def test_record_findings_empty_backlog_drives_the_acceptance_flow(tmp_path: Path) -> None:
    """The derived verdict: nothing outstanding *is* the acceptance signal —
    there is no ``accept`` field for a critic to disagree with."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_findings(
        "architect_critic",
        {"path": "proj/specs/architecture.md", "findings": [], "summary": "clean"},
    )

    # Autonomous mode auto-accepts, so the log ends on the acceptance marker.
    history = read_history(tmp_path / "specs" / "architecture.md", tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_record_findings_accepts_only_once_the_last_one_is_closed(tmp_path: Path) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"

    await engine._record_findings(
        "architect_critic",
        {"path": doc_path, "findings": [{"kind": "gap", "description": "a"}]},
    )
    assert [e["type"] for e in read_history(doc, tmp_path)] == ["new_revision"]

    await engine._record_findings(
        "architect_critic", {"path": doc_path, "findings": [{"id": "F1", "state": "fixed"}]}
    )
    assert [e["type"] for e in read_history(doc, tmp_path)] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_record_findings_leaves_unmentioned_findings_alone(tmp_path: Path) -> None:
    """Silence closes nothing: a finding the round does not mention keeps its
    state, so a critic that overlooks its own backlog cannot silently resolve
    it (doc/FINDINGS.md §3)."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"

    await engine._record_findings(
        "architect_critic",
        {
            "path": doc_path,
            "findings": [
                {"kind": "gap", "description": "a"},
                {"kind": "gap", "description": "b"},
            ],
        },
    )
    # A whole round that says nothing at all.
    await engine._record_findings("architect_critic", {"path": doc_path, "findings": []})

    states = {f["id"]: f["state"] for f in read_findings(_findings_dir(tmp_path), doc_path)}
    assert states == {"F1": "outstanding", "F2": "outstanding"}
    assert await engine._document_status(doc_path) == "needs_revision"


@pytest.mark.asyncio
async def test_record_findings_ignores_a_verdict_with_no_path(tmp_path: Path) -> None:
    """A malformed verdict is dropped, not raised: the loop reads the stores for
    the real status, and an unrecorded round simply leaves the file unsettled."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_findings("architect_critic", {"findings": []})

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


# ---------------------------------------------------------------------------
# The loop's return vs. the schema its caller is validated against
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_block_matches_the_generated_run_subagent_output_schema(
    tmp_path: Path,
) -> None:
    """The loop's ``review`` block and the ``run_subagent_<author>`` schema the
    caller's result is checked against must agree.

    They are written in two different files (``_run_review_loop`` here,
    ``_review_output_schema`` in ``subagents/_registry.py``) and nothing else
    connects them — a field renamed on one side alone would silently mark every
    reviewed spawn ``schema_compliance: false`` at runtime, which is the engine's
    "this sub-agent failed" signal. So the real loop output is run through the
    real ``normalize_output`` against the real generated schema here.
    """
    from kodo.subagents import AgentRegistry
    from kodo.toolspecs import normalize_output

    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = "proj/specs/architecture.md"

    async def _fake_spawn(
        name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        updates = [{"kind": "gap", "description": "still wrong"}]
        await engine._record_findings("architect_critic", {"path": doc_path, "findings": updates})
        return {"path": doc_path, "findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 1
    )

    registry = AgentRegistry(Path("src/kodo/subagents"))
    spec = next(
        s for s in registry.run_subagent_specs("guide") if s.name == "run_subagent_architect"
    )
    review_schema = spec.output_schema["properties"]["review"]  # type: ignore[index]

    normalized, compliant = normalize_output(review_schema, result["review"])  # type: ignore[arg-type]
    assert compliant, f"the loop's review block does not satisfy its own schema: {normalized}"
