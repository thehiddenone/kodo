"""Behavioral tests for materialization_path() and materialize().

Tests verify that artifact types land at the correct project paths per
STATE_AND_LIFECYCLE.md §1.1, and that materialize() writes the file there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kodo.toolchains import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)
from kodo.workspace import (
    Artifact,
    ArtifactType,
    ComponentRegistry,
    materialization_path,
    materialize,
)

# ---------------------------------------------------------------------------
# Stub toolchain
# ---------------------------------------------------------------------------


class _StubToolchain(ToolchainPlugin):
    @property
    def name(self) -> str:
        return "stub"

    @property
    def languages(self) -> list[str]:
        return ["stub"]

    async def init(self, project_root: Path) -> None:
        pass

    async def add_dependency(self, name: str, version: str | None = None) -> None:
        pass

    async def build(self, component_dir: Path) -> ToolchainBuildResult:
        return ToolchainBuildResult(success=True, output="")

    async def test(self, scope: ToolchainTestScope) -> ToolchainTestResult:
        return ToolchainTestResult(passed=0, failed=0)

    async def format(self, paths: list[Path]) -> None:
        pass

    def source_filename(self, filename_hint: str) -> str:
        return filename_hint if "." in filename_hint else f"{filename_hint}.py"

    def test_filename(self, filename_hint: str) -> str:
        stem = filename_hint.split(".")[0]
        return f"test_{stem}.py"


_TOOLCHAIN = _StubToolchain()

_ARCH_CONTENT = (
    "| Codename | Display name        |\n"
    "| -------- | ------------------- |\n"
    "| AUTH     | User Authentication |\n"
    "| TRADE    | Trade Execution     |\n"
)

_REG = ComponentRegistry(_ARCH_CONTENT)


def _artifact(type: ArtifactType, responsibility_code: str, filename_hint: str) -> Artifact:
    return Artifact(
        id="test-id",
        type=type,
        author="agent",
        project_code="PROJ",
        responsibility_code=responsibility_code,
        created_at=datetime.now(tz=UTC),
        content="content",
        filename_hint=filename_hint,
    )


ROOT = Path("/project")


# ---------------------------------------------------------------------------
# Project-wide artifact paths
# ---------------------------------------------------------------------------


def test_narrative_lands_in_specs_narrative() -> None:
    a = _artifact(ArtifactType.NARRATIVE, "PROJ", "narrative.md")
    assert materialization_path(a, ROOT, _TOOLCHAIN, _REG) == ROOT / "specs/narrative/narrative.md"


def test_tech_stack_lands_in_specs_tech_stack() -> None:
    a = _artifact(ArtifactType.TECH_STACK, "PROJ", "tech_stack.md")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG) == ROOT / "specs/tech_stack/tech_stack.md"
    )


def test_requirements_lands_in_specs_requirements() -> None:
    a = _artifact(ArtifactType.REQUIREMENTS, "PROJ", "requirements.md")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "specs/requirements/requirements.md"
    )


def test_architecture_lands_in_specs_architecture() -> None:
    a = _artifact(ArtifactType.ARCHITECTURE, "PROJ", "architecture.md")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "specs/architecture/architecture.md"
    )


def test_design_plan_lands_in_specs_design() -> None:
    a = _artifact(ArtifactType.DESIGN_PLAN, "PROJ", "design_plan.md")
    assert materialization_path(a, ROOT, _TOOLCHAIN, _REG) == ROOT / "specs/design/design_plan.md"


# ---------------------------------------------------------------------------
# Per-component artifact paths (with registry)
# ---------------------------------------------------------------------------


def test_functional_design_uses_display_name_dir() -> None:
    a = _artifact(ArtifactType.FUNCTIONAL_DESIGN, "AUTH", "design.md")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "specs/design/user_authentication/design.md"
    )


def test_test_plan_uses_display_name_dir() -> None:
    a = _artifact(ArtifactType.TEST_PLAN, "TRADE", "test_plan.md")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "specs/test_design/trade_execution/test_plan.md"
    )


def test_code_lands_in_src_with_toolchain_extension() -> None:
    a = _artifact(ArtifactType.CODE, "AUTH", "auth_service")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "src/user_authentication/auth_service.py"
    )


def test_test_lands_in_test_with_toolchain_prefix() -> None:
    a = _artifact(ArtifactType.TEST, "TRADE", "order_handler")
    assert (
        materialization_path(a, ROOT, _TOOLCHAIN, _REG)
        == ROOT / "test/trade_execution/test_order_handler.py"
    )


# ---------------------------------------------------------------------------
# Fallback when codename not in registry
# ---------------------------------------------------------------------------


def test_unknown_codename_falls_back_to_raw_code() -> None:
    a = _artifact(ArtifactType.FUNCTIONAL_DESIGN, "NOTIFY", "design.md")
    assert materialization_path(a, ROOT, _TOOLCHAIN, _REG) == ROOT / "specs/design/NOTIFY/design.md"


def test_no_registry_falls_back_to_raw_code() -> None:
    a = _artifact(ArtifactType.CODE, "AUTH", "auth_service")
    assert materialization_path(a, ROOT, _TOOLCHAIN, None) == ROOT / "src/AUTH/auth_service.py"


# ---------------------------------------------------------------------------
# feedback is not materialized
# ---------------------------------------------------------------------------


def test_feedback_returns_none() -> None:
    a = _artifact(ArtifactType.FEEDBACK, "PROJ", "feedback.md")
    assert materialization_path(a, ROOT, _TOOLCHAIN, _REG) is None


# ---------------------------------------------------------------------------
# materialize() writes file to the computed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_writes_content_to_correct_path(tmp_path: Path) -> None:
    """
    Given a narrative artifact,
    when materialize() is called,
    then the file appears at specs/narrative/<filename_hint> with the correct content.
    """
    a = _artifact(ArtifactType.NARRATIVE, "PROJ", "narrative.md")
    written = await materialize(a, tmp_path, _TOOLCHAIN, _REG)
    expected = tmp_path / "specs" / "narrative" / "narrative.md"
    assert written == expected
    assert expected.read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_materialize_returns_none_for_feedback(tmp_path: Path) -> None:
    a = _artifact(ArtifactType.FEEDBACK, "PROJ", "fb.md")
    result = await materialize(a, tmp_path, _TOOLCHAIN, _REG)
    assert result is None
