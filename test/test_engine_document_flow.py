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
from kodo.workproducts import (
    WorkProduct,
    read_components,
    read_work_product,
    record_membership,
    work_product_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeGate:
    def __init__(self, action: str = "agree", feedback: str = "", artifact_path: str = "") -> None:
        self.action = action
        self.feedback = feedback
        self.artifact_path = artifact_path
        self.calls: list[tuple[str, str | None, str]] = []
        self.paths: list[list[str]] = []

    async def fire_approval(
        self,
        gate_type: str,
        *,
        artifact_id: str | None = None,
        summary: str = "",
        paths: list[str] | None = None,
    ) -> ApprovalResponse:
        self.calls.append((gate_type, artifact_id, summary))
        self.paths.append(list(paths or []))
        return ApprovalResponse(
            action=self.action, feedback=self.feedback, artifact_path=self.artifact_path
        )


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
    # Input resolution and the role map both read the callee's SubAgentSpec.
    engine._registry = _FakeAgentRegistry()
    # The user-only findings table is pushed through the emitters; capturing it
    # here is what lets a test read what the *user* was shown, separately from
    # what any agent was told.
    engine._emitters = _FakeEmitters()
    return engine


class _FakeEmitters:
    """Captures the findings tables the engine pushes to the client."""

    def __init__(self) -> None:
        self.review_findings: list[dict[str, object]] = []

    async def emit_review_findings(self, **payload: object) -> None:
        self.review_findings.append(payload)


class _FakeAgentRegistry:
    """Minimal AgentRegistry double — only ``spec_for`` is read here.

    Defaults to the **real** specs, so these tests exercise the artifact roles
    the shipped agents actually declare rather than a parallel fiction; a test
    that needs a bespoke contract passes its own.
    """

    def __init__(
        self,
        *,
        specs: dict[str, object] | None = None,
        user_review: bool = True,
        critics: dict[str, str] | None = None,
    ) -> None:
        self._specs = specs
        self._user_review = user_review
        self._critics = critics or {}

    def get(self, name: str, autonomous: bool = False, phase: str = "initial"):
        from types import SimpleNamespace

        # `user_review` gates the approval prompt and `critic` decides whether
        # approval also closes the backlog; both are read off the agent, so the
        # double has to carry them. Default True keeps these tests aimed at the
        # gate's own behaviour rather than at whether it fires.
        return SimpleNamespace(
            name=name,
            critic=self._critics.get(name, ""),
            user_review=self._user_review,
        )

    def spec_for(self, name: str):
        from kodo.subagents._registry import SUBAGENT_SPECS_BY_NAME

        if self._specs is not None:
            return self._specs.get(name)
        return SUBAGENT_SPECS_BY_NAME.get(name)


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


# The architect's work product: one review loop, one reviewable set. Every test
# below drives the real store, so the id is derived exactly as the engine
# derives it (project folder + authoring agent).
_ARCH_DOC = "proj/specs/architecture.md"
_ARCH_WP = "proj/architect"


def _wp(paths: list[str], *, agent: str = "architect", responsibility: str = "") -> WorkProduct:
    return WorkProduct(
        id=work_product_id("proj", agent, responsibility),
        project="proj",
        agent=agent,
        responsibility_code=responsibility,
        paths=tuple(paths),
    )


def _author_result(*paths: str) -> dict[str, object]:
    """An author's return: the whole set it wrote, never a single primary path."""
    return {"paths": list(paths or (_ARCH_DOC,)), "summary": "wrote it"}


async def _seed_architect_inputs(engine) -> None:
    """Record what `architect` consumes, so it is not refused for lacking it.

    Every review-loop test below drives `architect`, whose contract requires the
    Narrative and Tech Stack. Since 2026-09-05 an agent whose required roles are
    unfilled is refused rather than spawned under-supplied — correct behaviour,
    and something these tests have to satisfy exactly as a real pipeline does.
    """
    await engine._record_work_product(
        "narrative_author",
        "",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )


def _ids(findings_dir: Path, key: str = _ARCH_WP) -> list[str]:
    """Finding ids read back from the store — never hardcoded, since they are
    minted from each finding's own first location."""
    return [f["id"] for f in read_findings(findings_dir, key)]


# ---------------------------------------------------------------------------
# _finalize_work_product
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_work_product_autonomous_mode_auto_accepts(tmp_path: Path) -> None:
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-1")
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)

    await engine._finalize_work_product(_wp([_ARCH_DOC]))

    assert gate.calls == []  # never consulted in autonomous mode
    history = read_history(doc, tmp_path)
    # No review_result: that entry means "the user decided at the gate", and no
    # gate fired — writing one would fabricate a decision nobody made.
    assert [e["type"] for e in history] == ["new_revision", "accepted"]
    assert history[-1]["commit_hash"] == "sha-1"


@pytest.mark.asyncio
async def test_finalize_work_product_allow_all_edit_control_also_skips_the_gate(
    tmp_path: Path,
) -> None:
    """Edit Control *Allow All* already means "don't stop me for file changes";
    stopping for a document sign-off in that posture contradicted every other
    gate (doc/FINDINGS.md §5)."""
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-allow")
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)
    engine._session.edit_control = "allow_all"

    await engine._finalize_work_product(_wp([_ARCH_DOC]))

    assert gate.calls == []
    assert [e["type"] for e in read_history(doc, tmp_path)] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_finalize_work_product_other_edit_control_settings_still_ask(tmp_path: Path) -> None:
    """Only *allow_all* shortcuts — ``smart`` and ``review_all`` still gate."""
    for setting in ("smart", "review_all"):
        root = tmp_path / setting
        root.mkdir()
        doc = _seed_revision(root, "specs/architecture.md")
        gate = _FakeGate(action="agree")
        engine = _bare_engine(project_root=root, autonomous=False, gate=gate)
        engine._session.edit_control = setting

        await engine._finalize_work_product(_wp([_ARCH_DOC]))

        assert len(gate.calls) == 1, setting
        assert [e["type"] for e in read_history(doc, root)] == [
            "new_revision",
            "review_result",
            "accepted",
        ], setting


@pytest.mark.asyncio
async def test_finalize_work_product_interactive_agree_records_approval_then_accepted(
    tmp_path: Path,
) -> None:
    doc = _seed_revision(tmp_path, "specs/architecture.md", sha="sha-2")
    gate = _FakeGate(action="agree")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)

    await engine._finalize_work_product(_wp([_ARCH_DOC]))

    assert len(gate.calls) == 1
    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "review_result", "accepted"]
    assert history[1]["decision"] == "approve"
    assert history[-1]["commit_hash"] == "sha-2"


@pytest.mark.asyncio
async def test_finalize_work_product_rejection_mints_the_users_feedback_as_a_finding(
    tmp_path: Path,
) -> None:
    """The user's objection reaches the author through the same backlog as every
    critic finding — one channel, one procedure (doc/FINDINGS.md §3)."""
    doc = _seed_revision(tmp_path, "specs/architecture.md")
    gate = _FakeGate(action="feedback", feedback="needs a North Star")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)

    await engine._finalize_work_product(_wp([_ARCH_DOC]))

    history = read_history(doc, tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "review_result"]
    assert history[-1]["decision"] == "reject"
    assert history[-1]["comment"] == "needs a North Star"

    findings = read_findings(_findings_dir(tmp_path), _ARCH_WP)
    assert len(findings) == 1
    assert findings[0]["state"] == "outstanding"
    assert findings[0]["reported_by"] == "user"
    assert findings[0]["description"] == "needs a North Star"
    # …and the document is back to needing revision because of it.
    assert await engine._work_product_status(_wp([_ARCH_DOC])) == "needs_revision"


@pytest.mark.asyncio
async def test_finalize_work_product_rejection_with_no_comment_mints_nothing(
    tmp_path: Path,
) -> None:
    """An empty rejection has nothing actionable in it; minting a blank finding
    would give the author an item it cannot possibly close."""
    _seed_revision(tmp_path, "specs/architecture.md")
    engine = _bare_engine(
        project_root=tmp_path, autonomous=False, gate=_FakeGate(action="feedback", feedback="  ")
    )

    await engine._finalize_work_product(_wp([_ARCH_DOC]))

    assert read_findings(_findings_dir(tmp_path), _ARCH_WP) == []


# ---------------------------------------------------------------------------
# _run_review_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_loop_reviews_the_authors_whole_reported_set(tmp_path: Path) -> None:
    """The author reports `paths`; every one of them is recorded as the work
    product and handed to the critic. Reviewing one file out of several cannot
    see the coherence between them, which is what is most likely to be wrong."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md", sha="sha-3")
    _seed_revision(tmp_path, "specs/design/AUTH.md", sha="sha-4")
    second = "proj/specs/design/AUTH.md"

    calls: list[tuple[str, dict[str, object], str]] = []

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        calls.append((name, task_input, findings_key))
        if name == "architect":
            return _author_result(_ARCH_DOC, second)
        await engine._record_findings("architect_critic", {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["paths"] == [_ARCH_DOC, second]
    assert "primary_path" not in result
    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 1
    assert result["review"]["outstanding"] == 0
    assert calls[0][0] == "architect"
    assert calls[1][0] == "architect_critic"
    # Both members reach the critic — under the ROLE they fill, which is what
    # architect_critic's own contract calls them — and the work product, not a
    # file path, scopes its get_findings.
    assert calls[1][1]["input_paths"] == {
        "architecture_architecture.md": _ARCH_DOC,
        "architecture_AUTH.md": second,
    }
    assert calls[1][2] == _ARCH_WP
    # The task names every file and says they are one change.
    assert _ARCH_DOC in str(calls[1][1]["instructions"])
    assert second in str(calls[1][1]["instructions"])


@pytest.mark.asyncio
async def test_review_loop_auto_closes_findings_for_a_file_that_leaves_the_set(
    tmp_path: Path,
) -> None:
    """A file that leaves the work product takes its findings with it: nothing
    will re-read it, so an outstanding finding against it could never be
    verified fixed and would block the loop forever."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    _seed_revision(tmp_path, "specs/design/AUTH.md")
    findings_dir = _findings_dir(tmp_path)
    doomed = "proj/specs/design/AUTH.md"
    rounds = iter(
        [
            # Round 1: one finding on each file.
            [
                {
                    "kind": "gap",
                    "description": "on the kept file",
                    "locations": [{"path": _ARCH_DOC, "first_line": 1}],
                },
                {
                    "kind": "gap",
                    "description": "on the doomed file",
                    "locations": [{"path": doomed, "first_line": 1}],
                },
            ],
            # Round 2 (after the author drops the second file): closes the rest.
            None,
        ]
    )
    author_paths = iter([[_ARCH_DOC, doomed], [_ARCH_DOC]])

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result(*next(author_paths))
        updates = next(rounds)
        if updates is None:
            updates = [{"id": _ids(findings_dir)[0], "state": "fixed"}]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, 5
    )

    # Both findings ended up closed — one by the critic, one automatically.
    states = {f["id"]: f["state"] for f in read_findings(findings_dir, _ARCH_WP)}
    assert set(states.values()) == {"fixed"}
    assert result["review"]["outcome"] == "accepted"


@pytest.mark.asyncio
async def test_review_loop_resends_identical_instructions_every_round(tmp_path: Path) -> None:
    """Findings are no longer rendered into the author's task: every round sends
    the caller's brief unchanged, and the author reads the backlog through
    ``get_findings`` instead. That is what makes a first pass and a tenth
    identical for the agent (doc/FINDINGS.md §4)."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    findings_dir = _findings_dir(tmp_path)
    doc_path = _ARCH_DOC

    calls: list[tuple[str, dict[str, object], str]] = []
    # Round 1 opens three findings; rounds 2 and 3 close them by the ids the
    # store actually minted, which are derived from each finding's own first
    # location rather than a counter.
    round_no = iter(range(3))

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        calls.append((name, dict(task_input), findings_key))
        if name == "architect":
            return _author_result()
        n = next(round_no)
        if n == 0:
            updates: list[dict[str, object]] = [
                {
                    "kind": "gap",
                    "description": f"missing {i}",
                    "locations": [{"path": doc_path, "first_line": i + 1}],
                }
                for i in range(3)
            ]
        elif n == 1:
            updates = [{"id": fid, "state": "fixed"} for fid in _ids(findings_dir)[:2]]
        else:
            updates = [{"id": _ids(findings_dir)[2], "state": "fixed"}]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce the architecture."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 3

    author_rounds = [task for name, task, _ in calls if name == "architect"]
    assert [t["instructions"] for t in author_rounds] == ["Produce the architecture."] * 3
    # Round 1 has no files yet, so no revision target and no findings scope.
    assert "for_revision_paths" not in author_rounds[0]
    assert calls[0][2] == ""
    # From round 2 the author is handed its whole prior set — not just an entry
    # point — and its get_findings is scoped to the work product, which is the
    # only place the findings reach it.
    assert author_rounds[1]["for_revision_paths"] == [doc_path]
    assert [scope for name, _, scope in calls if name == "architect"][1:] == [_ARCH_WP] * 2
    # Nothing about the findings themselves leaked into the task.
    assert all("missing" not in str(task) for task in author_rounds)

    # Every finding really did end up closed, by id.
    states = {f["id"]: f["state"] for f in read_findings(findings_dir, _ARCH_WP)}
    assert len(states) == 3
    assert set(states.values()) == {"fixed"}


@pytest.mark.asyncio
async def test_review_loop_stops_when_a_round_closes_and_opens_nothing(tmp_path: Path) -> None:
    """The stall detector: a round that neither closes nor opens anything is
    exact no-progress, so the loop reports ``not_converging`` well short of
    ``max_rounds`` rather than orbiting until the cap."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    first = True

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        nonlocal first
        if name == "architect":
            return _author_result()
        # Round 1 raises one finding; every later round says nothing at all —
        # the finding stays outstanding, and nothing moves.
        updates = [{"kind": "gap", "description": "same finding, every round"}] if first else []
        first = False
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

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
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    doc_path = _ARCH_DOC
    findings_dir = _findings_dir(tmp_path)
    round_no = iter(range(3))

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        n = next(round_no)
        if n == 0:
            updates: list[dict[str, object]] = [
                {
                    "kind": "gap",
                    "description": "a",
                    "locations": [{"path": doc_path, "first_line": 1}],
                },
                {
                    "kind": "gap",
                    "description": "b",
                    "locations": [{"path": doc_path, "first_line": 2}],
                },
            ]
        elif n == 1:
            # Closes two and finds two: real progress, not a stall.
            updates = [{"id": fid, "state": "fixed"} for fid in _ids(findings_dir)] + [
                {
                    "kind": "gap",
                    "description": "c",
                    "locations": [{"path": doc_path, "first_line": 3}],
                },
                {
                    "kind": "gap",
                    "description": "d",
                    "locations": [{"path": doc_path, "first_line": 4}],
                },
            ]
        else:
            open_ids = [
                f["id"]
                for f in read_findings(findings_dir, _ARCH_WP)
                if f["state"] == "outstanding"
            ]
            updates = [{"id": fid, "state": "fixed"} for fid in open_ids]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

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
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    counter = iter(range(100))

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        # Every round finds something new, so progress never stalls and only
        # the caller's budget can stop the loop.
        updates = [{"kind": "gap", "description": f"c{next(counter)}"}]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

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
    await _seed_architect_inputs(engine)
    spawned: list[str] = []

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
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
    await _seed_architect_inputs(engine)
    spawned: list[str] = []

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
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
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    spawned: list[str] = []

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        spawned.append(name)
        if name == "architect":
            return {**_author_result(), "reason": "   "}
        await engine._record_findings("architect_critic", {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, None
    )

    assert result["review"]["outcome"] == "accepted"
    assert spawned == ["architect", "architect_critic"]


# ---------------------------------------------------------------------------
# _record_findings — the engine-side half of a critic round
# ---------------------------------------------------------------------------


def _seed_work_product(engine, paths: list[str]) -> str:
    """Record a work product the way the review loop does, and return its id.

    ``_record_findings`` reads the membership store for the id-minting metadata,
    so a test that calls it directly has to establish the work product first —
    exactly as a real round does before spawning the critic. The store is
    project-scoped, so it is written under the project root.
    """
    work_product, _removed = record_membership(
        engine._project_root("proj"),
        project="proj",
        agent="architect",
        responsibility_code="",
        paths=paths,
    )
    return work_product.id


@pytest.mark.asyncio
async def test_record_findings_opens_new_findings_and_leaves_the_set_unsettled(
    tmp_path: Path,
) -> None:
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    key = _seed_work_product(engine, [_ARCH_DOC])

    await engine._record_findings(
        "architect_critic",
        {
            "findings": [
                {
                    "kind": "gap",
                    "description": "missing section",
                    "locations": [{"path": _ARCH_DOC, "first_line": 4, "last_line": 6}],
                }
            ],
            "summary": "1 finding",
        },
        key,
    )

    findings = read_findings(_findings_dir(tmp_path), _ARCH_WP)
    assert len(findings) == 1
    # The id describes its subject rather than counting: <project>_<agent>_<file>_<line>.
    assert findings[0]["id"] == "proj_architect_architecture.md_4"
    assert findings[0]["kind"] == "gap"
    assert findings[0]["state"] == "outstanding"
    assert findings[0]["reported_by"] == "architect_critic"
    assert await engine._work_product_status(_wp([_ARCH_DOC])) == "needs_revision"
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
    key = _seed_work_product(engine, [_ARCH_DOC])

    await engine._record_findings("architect_critic", {"findings": [], "summary": "clean"}, key)

    # Autonomous mode auto-accepts, so the log ends on the acceptance marker.
    history = read_history(tmp_path / "specs" / "architecture.md", tmp_path)
    assert [e["type"] for e in history] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_record_findings_accepts_every_member_only_once_the_last_one_is_closed(
    tmp_path: Path,
) -> None:
    """One backlog covers the whole set, so acceptance is reached — and reached
    for every member at once — only when nothing is outstanding anywhere."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    first = _seed_revision(tmp_path, "specs/architecture.md")
    second = _seed_revision(tmp_path, "specs/design/AUTH.md")
    other = "proj/specs/design/AUTH.md"
    key = _seed_work_product(engine, [_ARCH_DOC, other])

    await engine._record_findings(
        "architect_critic",
        {
            "findings": [
                {"kind": "gap", "description": "a", "locations": [{"path": _ARCH_DOC}]},
                {"kind": "gap", "description": "b", "locations": [{"path": other}]},
            ]
        },
        key,
    )
    assert [e["type"] for e in read_history(first, tmp_path)] == ["new_revision"]
    assert [e["type"] for e in read_history(second, tmp_path)] == ["new_revision"]

    # Closing only ONE of them settles nothing — not even its own file.
    open_ids = _ids(_findings_dir(tmp_path))
    await engine._record_findings(
        "architect_critic", {"findings": [{"id": open_ids[0], "state": "fixed"}]}, key
    )
    assert [e["type"] for e in read_history(first, tmp_path)] == ["new_revision"]

    await engine._record_findings(
        "architect_critic", {"findings": [{"id": open_ids[1], "state": "fixed"}]}, key
    )
    for doc in (first, second):
        assert [e["type"] for e in read_history(doc, tmp_path)] == ["new_revision", "accepted"]


@pytest.mark.asyncio
async def test_record_findings_leaves_unmentioned_findings_alone(tmp_path: Path) -> None:
    """Silence closes nothing: a finding the round does not mention keeps its
    state, so a critic that overlooks its own backlog cannot silently resolve
    it (doc/FINDINGS.md §3)."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")
    key = _seed_work_product(engine, [_ARCH_DOC])

    await engine._record_findings(
        "architect_critic",
        {
            "findings": [
                {
                    "kind": "gap",
                    "description": "a",
                    "locations": [{"path": _ARCH_DOC, "first_line": 1}],
                },
                {
                    "kind": "gap",
                    "description": "b",
                    "locations": [{"path": _ARCH_DOC, "first_line": 2}],
                },
            ]
        },
        key,
    )
    # A whole round that says nothing at all.
    await engine._record_findings("architect_critic", {"findings": []}, key)

    states = {f["id"]: f["state"] for f in read_findings(_findings_dir(tmp_path), _ARCH_WP)}
    assert len(states) == 2
    assert set(states.values()) == {"outstanding"}
    assert await engine._work_product_status(_wp([_ARCH_DOC])) == "needs_revision"


@pytest.mark.asyncio
async def test_record_findings_ignores_a_round_with_no_work_product(tmp_path: Path) -> None:
    """A critic that ran outside a work product has nowhere to write; dropping
    the round is right, and the loop reads the stores for the real status."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_findings("architect_critic", {"findings": []}, "")

    history = read_history(tmp_path / "specs" / "architecture.md", tmp_path)
    assert [e["type"] for e in history] == ["new_revision"]


@pytest.mark.asyncio
async def test_record_findings_ignores_an_unknown_work_product(tmp_path: Path) -> None:
    """The key names the subject; one the membership store has never seen
    cannot supply the metadata ids are minted from, so the round is dropped
    rather than written under a half-known identity."""
    gate = _FakeGate()
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=gate)
    _seed_revision(tmp_path, "specs/architecture.md")

    await engine._record_findings("architect_critic", {"findings": []}, "proj/never_recorded")

    assert read_findings(_findings_dir(tmp_path), "proj/never_recorded") == []


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

    async def _fake_spawn(
        name: str,
        task_input: dict[str, object],
        findings_key: str = "",
        phase: str = "initial",
    ) -> dict[str, object]:
        if name == "architect":
            return _author_result()
        updates = [{"kind": "gap", "description": "still wrong"}]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

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


# ---------------------------------------------------------------------------
# _resolve_input_paths — inputs come from the ledger, not from a model
# ---------------------------------------------------------------------------
#
# Until 2026-09-04 the engine passed every critic a hardcoded `{"target": path}`:
# one path, under a label in no agent's vocabulary, no matter how many inputs
# its contract declared. A `requirements_critic` promised the architecture and
# handed only the document under review reconstructed the architecture's path
# from the worked example in its own schema description and read the resulting
# nonexistent file ~1133 times (session 1788543589). A short-lived inheritance
# stopgap forwarded the author's paths; declared roles replaced it on
# 2026-09-05, and these pin the replacement.


def _seed_role(engine, agent: str, paths: list[str], output: dict[str, object] | None = None):
    """Record a work product for *agent*, deriving its roles from the real spec."""
    return engine._record_work_product(agent, "", paths, output or {"paths": paths})


@pytest.mark.asyncio
async def test_resolution_supplies_the_input_that_went_missing(tmp_path: Path) -> None:
    """The traced failure, inverted: `requirements_critic` declares it consumes
    the architecture, so it now receives it — from the ledger, not from a
    label the calling model happened to choose."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_role(engine, "architect", ["proj/specs/architecture.md"])
    await _seed_role(
        engine,
        "narrative_author",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )
    reviewed = await _seed_role(engine, "requirements_author", [_ARCH_DOC])

    resolved, _unmet = await engine._resolve_input_paths(
        "requirements_critic", under_review=reviewed
    )

    assert resolved == {
        "requirements": _ARCH_DOC,
        "architecture": "proj/specs/architecture.md",
        "narrative": "proj/specs/narrative.md",
    }


@pytest.mark.asyncio
async def test_a_multi_role_producer_fills_each_role_separately(tmp_path: Path) -> None:
    """`narrative_author` writes two documents in one run. A consumer asking for
    the Narrative must get the Narrative — not both files under one label."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    work_product = await _seed_role(
        engine,
        "narrative_author",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )

    assert work_product is not None
    assert work_product.roles == {
        "narrative": ("proj/specs/narrative.md",),
        "tech_stack": ("proj/specs/tech_stack.md",),
    }
    # The architect asks for both, and gets them under their own names.
    architect_paths, unmet = await engine._resolve_input_paths("architect")
    assert architect_paths == {
        "narrative": "proj/specs/narrative.md",
        "tech_stack": "proj/specs/tech_stack.md",
    }
    assert unmet == ()


@pytest.mark.asyncio
async def test_the_remainder_role_takes_only_what_no_named_field_claimed(
    tmp_path: Path,
) -> None:
    """`functional_designer` writes the Design Plan and every Functional Design
    in one run. The Plan is named by its own output field, so the designs — the
    remainder — must not include it."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    paths = [
        "proj/specs/design_plan.md",
        "proj/specs/design/AUTH.md",
        "proj/specs/design/LEDGER.md",
    ]

    work_product = await _seed_role(
        engine,
        "functional_designer",
        paths,
        {"paths": paths, "design_plan_path": "proj/specs/design_plan.md"},
    )

    assert work_product is not None
    assert work_product.roles == {
        "design_plan": ("proj/specs/design_plan.md",),
        "functional_design": ("proj/specs/design/AUTH.md", "proj/specs/design/LEDGER.md"),
    }


@pytest.mark.asyncio
async def test_a_critics_contract_excludes_what_its_author_was_given(
    tmp_path: Path,
) -> None:
    """`code_critic` judges code AS code. Its declared needs are the file under
    review and the Tech Stack — so it receives exactly those, even though its
    `coder` was handed the design, requirements and test plan. Under the old
    inheritance stopgap this required a per-agent opt-out flag; a declaration
    that simply does not ask for them needs no exception."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_role(
        engine,
        "narrative_author",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )
    await _seed_role(engine, "requirements_author", ["proj/specs/requirements.md"])
    reviewed = await _seed_role(engine, "coder", ["proj/src/leaderboard.py"])

    resolved, _unmet = await engine._resolve_input_paths("code_critic", under_review=reviewed)

    assert resolved == {
        "code": "proj/src/leaderboard.py",
        "tech_stack": "proj/specs/tech_stack.md",
    }
    assert "requirements" not in resolved


@pytest.mark.asyncio
async def test_an_unmet_required_need_is_logged_and_left_out(tmp_path: Path) -> None:
    """Nothing has produced the architecture yet. The spawn proceeds without it
    rather than being refused — refusing outright is the intended end state,
    once resolution is trusted across every stage — but it must not invent one."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    reviewed = await _seed_role(engine, "requirements_author", [_ARCH_DOC])

    resolved, _unmet = await engine._resolve_input_paths(
        "requirements_critic", under_review=reviewed
    )

    assert resolved == {"requirements": _ARCH_DOC}
    assert "architecture" not in resolved


@pytest.mark.asyncio
async def test_resolution_keeps_projects_apart(tmp_path: Path) -> None:
    """A session may bind several projects, and one project's architecture is
    not another's."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await engine._record_work_product("architect", "", ["other/specs/architecture.md"], {})
    reviewed = await _seed_role(engine, "requirements_author", [_ARCH_DOC])

    resolved, _unmet = await engine._resolve_input_paths(
        "requirements_critic", under_review=reviewed
    )

    assert "architecture" not in resolved


@pytest.mark.asyncio
async def test_a_caller_supplied_path_survives_but_never_shadows_a_resolved_role(
    tmp_path: Path,
) -> None:
    """Transitional: an author's model-supplied `input_paths` still rides along
    for anything resolution does not cover, but a resolved role wins its own
    label. The field comes off the caller-facing schema entirely in a later
    phase, at which point resolution is the only source."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_role(
        engine,
        "narrative_author",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )

    resolved, _unmet = await engine._resolve_input_paths(
        "architect",
        caller_paths={"narrative": "proj/specs/WRONG.md", "scratch": "proj/notes.md"},
    )

    assert resolved["narrative"] == "proj/specs/narrative.md"
    assert resolved["scratch"] == "proj/notes.md"


@pytest.mark.asyncio
async def test_an_agent_that_declares_nothing_keeps_its_callers_paths(
    tmp_path: Path,
) -> None:
    """A non-pipeline agent has no `consumes`, so the engine builds nothing and
    the caller's own paths stand unchanged."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())

    resolved, _unmet = await engine._resolve_input_paths(
        "developer", caller_paths={"module": "proj/src/orders.py"}
    )

    assert resolved == {"module": "proj/src/orders.py"}


@pytest.mark.asyncio
async def test_malformed_caller_paths_are_ignored(tmp_path: Path) -> None:
    """`input_paths` is model-authored, so it is not trusted to be well-shaped."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())

    assert (await engine._resolve_input_paths("developer", caller_paths=None))[0] == {}
    assert (await engine._resolve_input_paths("developer", caller_paths="nope"))[0] == {}
    junk, _ = await engine._resolve_input_paths(
        "developer", caller_paths={"a": "", "b": None, "c": 42}
    )
    assert junk == {}


@pytest.mark.asyncio
async def test_the_architects_component_graph_is_recorded(tmp_path: Path) -> None:
    """The decomposition already exists as prose in the architecture document;
    declaring it as data is what lets a later stage be handed a component's
    neighbours instead of every reader re-deriving the same graph."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())

    await engine._record_work_product(
        "architect",
        "",
        ["proj/specs/architecture.md"],
        {
            "paths": ["proj/specs/architecture.md"],
            "components": [
                {"code": "AUTH", "depends_on": ["LEDGER"]},
                {"code": "LEDGER", "depends_on": []},
            ],
        },
    )

    assert read_components(tmp_path, "proj") == {"AUTH": ["LEDGER"], "LEDGER": []}


@pytest.mark.asyncio
async def test_only_the_architecture_producer_can_record_a_graph(tmp_path: Path) -> None:
    """No other output shape may overwrite the decomposition by accident."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())

    await engine._record_work_product(
        "requirements_author",
        "",
        ["proj/specs/requirements.md"],
        {
            "paths": ["proj/specs/requirements.md"],
            "components": [{"code": "SNEAKY", "depends_on": []}],
        },
    )

    assert read_components(tmp_path, "proj") == {}


@pytest.mark.asyncio
async def test_per_component_designs_are_attributed_from_the_authors_map(
    tmp_path: Path,
) -> None:
    """`functional_designer` writes every design in one whole-product run, so
    without this map `self` scope could only match all of them or none."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    paths = ["proj/specs/design_plan.md", "proj/d/AUTH.md", "proj/d/LEDGER.md"]

    work_product = await engine._record_work_product(
        "functional_designer",
        "",
        paths,
        {
            "paths": paths,
            "design_plan_path": "proj/specs/design_plan.md",
            "designs": {"AUTH": "proj/d/AUTH.md", "LEDGER": "proj/d/LEDGER.md"},
        },
    )

    assert work_product is not None
    assert work_product.components == {
        "proj/d/AUTH.md": "AUTH",
        "proj/d/LEDGER.md": "LEDGER",
    }
    # The Design Plan belongs to no component — it is a whole-product document.
    assert work_product.component_of("proj/specs/design_plan.md") == ""


@pytest.mark.asyncio
async def test_a_coder_gets_its_own_design_and_its_neighbours(tmp_path: Path) -> None:
    """The end-to-end shape of Phase 3: the architect's graph plus per-file
    attribution means a coder is handed exactly the designs its component
    touches, out of however many the product has."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await engine._record_work_product(
        "narrative_author",
        "",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )
    await engine._record_work_product(
        "architect",
        "",
        ["proj/specs/architecture.md"],
        {
            "paths": ["proj/specs/architecture.md"],
            "components": [
                {"code": "AUTH", "depends_on": ["LEDGER"]},
                {"code": "LEDGER", "depends_on": []},
                {"code": "REPORTS", "depends_on": []},
            ],
        },
    )
    await engine._record_work_product("requirements_author", "", ["proj/specs/requirements.md"], {})
    designs = ["proj/d/AUTH.md", "proj/d/LEDGER.md", "proj/d/REPORTS.md"]
    await engine._record_work_product(
        "functional_designer",
        "",
        designs,
        {
            "paths": designs,
            "designs": {
                "AUTH": "proj/d/AUTH.md",
                "LEDGER": "proj/d/LEDGER.md",
                "REPORTS": "proj/d/REPORTS.md",
            },
        },
    )
    await engine._record_work_product("test_designer", "AUTH", ["proj/t/AUTH.md"], {})
    await engine._record_work_product("test_coder", "AUTH", ["proj/test/AUTH_test.py"], {})

    resolved, unmet = await engine._resolve_input_paths("coder", responsibility_code="AUTH")

    assert unmet == ()
    # Its own design, plus LEDGER's because AUTH consumes it — and REPORTS,
    # which touches neither, is correctly absent. One role at two scopes is
    # accumulated into one labelled group rather than one overwriting the other.
    assert resolved["functional_design_AUTH.md"] == "proj/d/AUTH.md"
    assert resolved["functional_design_LEDGER.md"] == "proj/d/LEDGER.md"
    assert "proj/d/REPORTS.md" not in resolved.values()
    assert resolved["test_plan"] == "proj/t/AUTH.md"
    assert resolved["test_code"] == "proj/test/AUTH_test.py"


@pytest.mark.asyncio
async def test_a_reinvoked_author_is_handed_what_it_wrote_last_time(
    tmp_path: Path,
) -> None:
    """The Guide routinely calls the same author again on the same subject
    ("continue resolving outstanding findings"), and that call's round 1 is the
    work's round N. Its prior work product is seeded from the ledger, so the
    author is told which files to revise and its findings are scoped from the
    very first round — rather than starting as if nothing existed."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    # A previous run_subagent call already produced this work product.
    await engine._record_work_product("architect", "", [_ARCH_DOC], {"paths": [_ARCH_DOC]})

    calls: list[tuple[str, dict[str, object], str]] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        calls.append((name, dict(task_input), findings_key))
        if name == "architect":
            return _author_result()
        await engine._record_findings(name, {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Carry on."}, None
    )

    first_author_round = calls[0][1]
    assert first_author_round["for_revision_paths"] == [_ARCH_DOC]
    # …and its get_findings is scoped to the same work product from round one.
    assert calls[0][2] == _ARCH_WP


@pytest.mark.asyncio
async def test_a_first_ever_round_has_nothing_to_revise(tmp_path: Path) -> None:
    """The other half: with no prior work product, the author is not handed an
    empty or invented revision target."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")

    calls: list[tuple[str, dict[str, object], str]] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        calls.append((name, dict(task_input), findings_key))
        if name == "architect":
            return _author_result()
        await engine._record_findings(name, {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop(
        "architect", "architect_critic", {"instructions": "Produce it."}, None
    )

    assert "for_revision_paths" not in calls[0][1]
    assert calls[0][2] == ""


@pytest.mark.asyncio
async def test_engine_resolved_paths_replace_anything_a_caller_still_sends(
    tmp_path: Path,
) -> None:
    """`input_paths` is off the caller-facing tool now, but a model can still
    put arbitrary keys in a task. A resolved role must win its own label."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")

    calls: list[dict[str, object]] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        calls.append(dict(task_input))
        if name == "architect":
            return _author_result()
        await engine._record_findings(name, {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop(
        "architect",
        "architect_critic",
        {
            "instructions": "Produce it.",
            "input_paths": {"narrative": "proj/specs/INVENTED.md"},
        },
        None,
    )

    assert calls[0]["input_paths"]["narrative"] == "proj/specs/narrative.md"


@pytest.mark.asyncio
async def test_a_responsibility_code_aimed_at_a_product_level_stage_is_dropped(
    tmp_path: Path,
) -> None:
    """`architect` is product-level: it has no `responsibility_code` on its tool
    at all. A model that sends one anyway must not be able to split its work
    product — `proj/architect/AUTH` and `proj/architect` are two records, and a
    later stage asking for "the architecture" finds whichever the last call
    happened to write. The engine drops the field rather than trusting the
    caller to have read the rule."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")

    calls: list[dict[str, object]] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        calls.append(dict(task_input))
        if name == "architect":
            return _author_result()
        await engine._record_findings(name, {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop(
        "architect",
        "architect_critic",
        {"instructions": "Produce it.", "responsibility_code": "AUTH"},
        None,
    )

    # Recorded under the un-suffixed id, exactly as a call without the stray
    # field would have been…
    assert read_work_product(tmp_path, _ARCH_WP) is not None
    assert read_work_product(tmp_path, work_product_id("proj", "architect", "AUTH")) is None
    # …and it never reaches the rendered brief either, so the author is not told
    # it is working on one component of a product-level document.
    assert "responsibility_code" not in calls[0]


@pytest.mark.asyncio
async def test_a_per_component_stage_still_carries_its_responsibility_code(
    tmp_path: Path,
) -> None:
    """The other half: for a stage whose spec declares the field it is a real
    caller decision — which component to run next — and it still scopes both the
    work product and the brief."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await engine._record_work_product(
        "functional_designer",
        "",
        ["proj/specs/design_plan.md", "proj/specs/design/AUTH.md"],
        {
            "plan_path": "proj/specs/design_plan.md",
            "designs": {"AUTH": "proj/specs/design/AUTH.md"},
        },
    )
    await engine._record_work_product(
        "requirements_author", "", ["proj/specs/requirements.md"], {"paths": []}
    )
    await engine._record_work_product(
        "narrative_author",
        "",
        ["proj/specs/narrative.md", "proj/specs/tech_stack.md"],
        {
            "narrative_path": "proj/specs/narrative.md",
            "tech_stack_path": "proj/specs/tech_stack.md",
        },
    )
    _seed_revision(tmp_path, "specs/test_plan/AUTH.md")
    plan = "proj/specs/test_plan/AUTH.md"

    calls: list[dict[str, object]] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        calls.append(dict(task_input))
        if name == "test_designer":
            return {"paths": [plan], "summary": "wrote it"}
        await engine._record_findings(name, {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop(
        "test_designer",
        "test_design_critic",
        {"instructions": "Plan AUTH's tests.", "responsibility_code": "AUTH"},
        None,
    )

    assert read_work_product(tmp_path, work_product_id("proj", "test_designer", "AUTH")) is not None
    assert calls[0]["responsibility_code"] == "AUTH"


# ---------------------------------------------------------------------------
# The user-review gate as a loop of its own, and the phase each round runs in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gate_only_author_loops_until_the_user_accepts(tmp_path: Path) -> None:
    """An author with `user_review: true` and no critic still runs a loop: the
    user is the reviewer, their rejection is a finding, and the next round is
    the author resolving it."""
    gate = _FakeGate(action="feedback", feedback="not what I meant")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    engine._registry = _FakeAgentRegistry(critics={"architect": ""})
    engine._emitters = _FakeEmitters()

    rounds: list[str] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        rounds.append(phase)
        return _author_result(_ARCH_DOC)

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop("architect", "", {"instructions": "go"}, 3)

    # The user rejected every round, so the budget is what ended it — and no
    # critic was ever spawned.
    assert result["review"]["outcome"] == "max_rounds"
    assert result["review"]["rounds"] == 3
    assert len(gate.calls) == 3
    # Round 1 writes from the inputs; every later round is a correction pass.
    assert rounds == ["initial", "revision", "revision"]
    # Each rejection reached the author the same way a critic's finding would.
    assert len(_ids(_findings_dir(tmp_path))) == 3


@pytest.mark.asyncio
async def test_a_gate_only_author_stops_the_moment_the_user_agrees(tmp_path: Path) -> None:
    gate = _FakeGate(action="agree")
    engine = _bare_engine(project_root=tmp_path, autonomous=False, gate=gate)
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    engine._registry = _FakeAgentRegistry(critics={"architect": ""})
    engine._emitters = _FakeEmitters()

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        return _author_result(_ARCH_DOC)

    engine._spawn_subagent = _fake_spawn

    result = await engine._run_review_loop("architect", "", {"instructions": "go"}, 5)

    assert result["review"]["outcome"] == "accepted"
    assert result["review"]["rounds"] == 1
    assert len(gate.calls) == 1


@pytest.mark.asyncio
async def test_a_first_round_is_initial_and_the_next_is_a_revision(tmp_path: Path) -> None:
    """The phase follows the same signal as `for_revision_paths`: whether a work
    product with members already exists."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    phases: list[str] = []
    round_no = 0

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        nonlocal round_no
        if name == "architect":
            phases.append(phase)
            return _author_result(_ARCH_DOC)
        round_no += 1
        # Round 1 opens a finding so a second round runs; round 2 closes it.
        updates = (
            [{"kind": "gap", "description": "missing", "locations": [{"path": _ARCH_DOC}]}]
            if round_no == 1
            else [{"id": _ids(_findings_dir(tmp_path))[0], "state": "fixed"}]
        )
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop("architect", "architect_critic", {"instructions": "go"}, 3)

    assert phases == ["initial", "revision"]


@pytest.mark.asyncio
async def test_a_reinvoked_author_starts_in_the_revision_phase(tmp_path: Path) -> None:
    """The ledger is read before round 1, so a *separate* call continuing the
    same work is a correction pass from its very first round."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")
    await engine._record_work_product("architect", "", [_ARCH_DOC], _author_result(_ARCH_DOC))
    phases: list[str] = []

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        if name == "architect":
            phases.append(phase)
            return _author_result(_ARCH_DOC)
        await engine._record_findings("architect_critic", {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop("architect", "architect_critic", {"instructions": "go"}, 3)

    assert phases == ["revision"]


@pytest.mark.asyncio
async def test_each_review_round_pushes_the_users_findings_table(tmp_path: Path) -> None:
    """The table is the only thing that tells the *user* what is wrong. It is
    pushed as an event, never into any agent's context."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        if name == "architect":
            return _author_result(_ARCH_DOC)
        updates = [
            {
                "kind": "gap",
                "description": "missing",
                "locations": [{"path": _ARCH_DOC, "first_line": 12}],
            }
        ]
        await engine._record_findings("architect_critic", {"findings": updates}, findings_key)
        return {"findings": updates}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop("architect", "architect_critic", {"instructions": "go"}, 2)

    tables = engine._emitters.review_findings
    assert len(tables) == 2
    first = tables[0]
    assert first["work_product_id"] == _ARCH_WP
    assert first["agent"] == "architect"
    assert first["reviewer_name"] == "architect_critic"
    assert (first["iteration"], first["max_rounds"]) == (1, 2)
    assert [f["description"] for f in first["findings"]] == ["missing"]
    # The counter rises, which is what makes the table readable as progress.
    assert tables[1]["iteration"] == 2


@pytest.mark.asyncio
async def test_a_clean_first_round_pushes_no_table(tmp_path: Path) -> None:
    """Work that was right first time has nothing to table; emitting an empty
    one on every clean accept would train the reader to skip it."""
    engine = _bare_engine(project_root=tmp_path, autonomous=True, gate=_FakeGate())
    await _seed_architect_inputs(engine)
    _seed_revision(tmp_path, "specs/architecture.md")

    async def _fake_spawn(name, task_input, findings_key="", phase="initial"):
        if name == "architect":
            return _author_result(_ARCH_DOC)
        await engine._record_findings("architect_critic", {"findings": []}, findings_key)
        return {"findings": []}

    engine._spawn_subagent = _fake_spawn

    await engine._run_review_loop("architect", "architect_critic", {"instructions": "go"}, 2)

    assert engine._emitters.review_findings == []
