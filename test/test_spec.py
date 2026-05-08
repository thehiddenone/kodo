"""Behavior tests for kodo.workflow._spec."""

from __future__ import annotations

import pytest

from kodo.workflow._spec import StageSpec, build_component_stages
from kodo.workflow._stages import Stage

# ---------------------------------------------------------------------------
# build_component_stages
# ---------------------------------------------------------------------------


def test_build_component_stages_empty_returns_empty() -> None:
    assert build_component_stages([]) == ()


def test_build_component_stages_creates_five_specs_per_component() -> None:
    specs = build_component_stages(["core", "api"])
    assert len(specs) == 10  # 5 stages per component


def test_build_component_stages_stage_order() -> None:
    specs = build_component_stages(["core"])
    stages = [s.stage for s in specs]
    assert stages == [
        Stage.REQUIREMENTS,
        Stage.DESIGN,
        Stage.TEST_PLAN,
        Stage.TEST_CODING,
        Stage.IMPLEMENTATION,
    ]


def test_build_component_stages_sorted_alphabetically() -> None:
    specs = build_component_stages(["zeta", "alpha"])
    components = [s.component for s in specs]
    # alpha comes first (5 specs), then zeta (5 specs)
    assert components[:5] == ["alpha"] * 5
    assert components[5:] == ["zeta"] * 5


def test_build_component_stages_artifact_paths() -> None:
    specs = build_component_stages(["api"])
    artifacts = [s.artifact for s in specs]
    assert artifacts == [
        "src/api/requirements.kd",
        "src/api/design.kd",
        "src/api/test_plan.kd",
        "gen/api/tests/test_api.py",
        "gen/api/src/api.py",
    ]


def test_build_component_stages_gate_types() -> None:
    specs = build_component_stages(["core"])
    gate_types = [s.gate_type for s in specs]
    assert gate_types == ["requirements", "design", "test_plan", "test_coding", "implementation"]


def test_build_component_stages_component_field_set() -> None:
    specs = build_component_stages(["broker"])
    assert all(s.component == "broker" for s in specs)


def test_build_component_stages_agents_assigned() -> None:
    specs = build_component_stages(["core"])
    reqs, design, plan, test_coding, impl = specs
    assert reqs.author == "requirements_author"
    assert reqs.critic == "requirements_reviewer"
    assert design.author == "functional_designer"
    assert design.critic == "functional_design_critic"
    assert plan.author == "test_designer"
    assert plan.critic == "test_design_critic"
    assert test_coding.author == "test_coder"
    assert test_coding.critic is None
    assert impl.author == "coder"
    assert impl.critic == "code_reviewer"


def test_build_component_stages_returns_frozen_tuple() -> None:
    result = build_component_stages(["x"])
    assert isinstance(result, tuple)
    assert all(isinstance(s, StageSpec) for s in result)


# ---------------------------------------------------------------------------
# StageSpec.build_task_message — REQUIREMENTS
# ---------------------------------------------------------------------------


@pytest.fixture()
def requirements_spec() -> StageSpec:
    return StageSpec(
        stage=Stage.REQUIREMENTS,
        author="requirements_author",
        critic="requirements_reviewer",
        artifact="src/mycomp/requirements.kd",
        gate_type="requirements",
        component="mycomp",
    )


def test_requirements_message_includes_component(requirements_spec: StageSpec) -> None:
    msg = requirements_spec.build_task_message({})
    assert "mycomp" in msg


def test_requirements_message_includes_artifact_path(requirements_spec: StageSpec) -> None:
    msg = requirements_spec.build_task_message({})
    assert "requirements.kd" in msg


def test_requirements_message_includes_narrative(requirements_spec: StageSpec) -> None:
    ctx = {"src/narrative.kd": "Project builds a trading bot."}
    msg = requirements_spec.build_task_message(ctx)
    assert "trading bot" in msg


def test_requirements_message_includes_responsibilities(requirements_spec: StageSpec) -> None:
    ctx = {"src/responsibilities.kd": "## Order Execution\nPlaces trades."}
    msg = requirements_spec.build_task_message(ctx)
    assert "Order Execution" in msg


def test_requirements_message_fallback_when_context_empty(
    requirements_spec: StageSpec,
) -> None:
    msg = requirements_spec.build_task_message({})
    assert "not available" in msg


# ---------------------------------------------------------------------------
# StageSpec.build_task_message — DESIGN
# ---------------------------------------------------------------------------


@pytest.fixture()
def design_spec() -> StageSpec:
    return StageSpec(
        stage=Stage.DESIGN,
        author="functional_designer",
        critic="functional_design_critic",
        artifact="src/mycomp/design.kd",
        gate_type="design",
        component="mycomp",
    )


def test_design_message_includes_component(design_spec: StageSpec) -> None:
    msg = design_spec.build_task_message({})
    assert "mycomp" in msg


def test_design_message_includes_artifact_path(design_spec: StageSpec) -> None:
    msg = design_spec.build_task_message({})
    assert "design.kd" in msg


def test_design_message_includes_requirements(design_spec: StageSpec) -> None:
    ctx = {"src/mycomp/requirements.kd": "FR-01. Must place orders."}
    msg = design_spec.build_task_message(ctx)
    assert "FR-01" in msg


# ---------------------------------------------------------------------------
# StageSpec.build_task_message — TEST_PLAN
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_plan_spec() -> StageSpec:
    return StageSpec(
        stage=Stage.TEST_PLAN,
        author="test_designer",
        critic="test_design_critic",
        artifact="src/mycomp/test_plan.kd",
        gate_type="test_plan",
        component="mycomp",
    )


def test_test_plan_message_includes_component(test_plan_spec: StageSpec) -> None:
    msg = test_plan_spec.build_task_message({})
    assert "mycomp" in msg


def test_test_plan_message_includes_artifact_path(test_plan_spec: StageSpec) -> None:
    msg = test_plan_spec.build_task_message({})
    assert "test_plan.kd" in msg


def test_test_plan_message_includes_requirements(test_plan_spec: StageSpec) -> None:
    ctx = {"src/mycomp/requirements.kd": "FR-01. Must place orders."}
    msg = test_plan_spec.build_task_message(ctx)
    assert "FR-01" in msg


def test_test_plan_message_includes_design(test_plan_spec: StageSpec) -> None:
    ctx = {"src/mycomp/design.kd": "## OrderManager interface"}
    msg = test_plan_spec.build_task_message(ctx)
    assert "OrderManager" in msg
