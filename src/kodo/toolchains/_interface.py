"""ToolchainPlugin abstract base class and shared result types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ToolchainPlugin",
    "BuildResult",
    "TestResult",
    "TestCase",
    "TestScope",
]


@dataclass(frozen=True)
class TestCase:
    """Result for a single test case.

    Attributes:
        name: Fully qualified test name.
        passed: Whether the test passed.
        message: Failure message or empty string on pass.
    """

    name: str
    passed: bool
    message: str = ""


@dataclass(frozen=True)
class TestResult:
    """Aggregated test run outcome.

    Attributes:
        passed: Number of tests that passed.
        failed: Number of tests that failed.
        cases: Per-test details.
        coverage_path: Path to coverage report file, or ``None``.
    """

    passed: int
    failed: int
    cases: list[TestCase] = field(default_factory=list)
    coverage_path: Path | None = None

    @property
    def all_passed(self) -> bool:
        """True when no tests failed."""
        return self.failed == 0


@dataclass(frozen=True)
class BuildResult:
    """Outcome of a build operation.

    Attributes:
        success: Whether the build succeeded.
        output: Combined stdout/stderr from the build tool.
    """

    success: bool
    output: str


@dataclass(frozen=True)
class TestScope:
    """Scope selector for :meth:`ToolchainPlugin.test`.

    Attributes:
        component: Limit tests to a specific component directory, or ``None``
            for all components.
        kind: ``'unit'``, ``'integration'``, or ``'e2e'``.
    """

    component: str | None = None
    kind: str = "unit"


class ToolchainPlugin(ABC):
    """Abstract toolchain plugin (FR-TC-01).

    Each concrete implementation knows how to initialise, build, test, and
    format projects for a specific language ecosystem.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin identifier, e.g. ``'python'``."""

    @property
    @abstractmethod
    def languages(self) -> list[str]:
        """Language identifiers this plugin handles, e.g. ``['python']``."""

    @abstractmethod
    async def init(self, project_root: Path) -> None:
        """Initialise a new project at ``project_root``.

        Creates the language-specific manifest (``pyproject.toml`` or
        ``package.json``) and any required directories.

        Args:
            project_root (Path): Root of the Kodo project.
        """

    @abstractmethod
    async def add_dependency(self, name: str, version: str | None = None) -> None:
        """Add a runtime dependency to the project manifest.

        Args:
            name (str): Package name.
            version (str | None): Version constraint, or ``None`` for latest.
        """

    @abstractmethod
    async def build(self, component_dir: Path) -> BuildResult:
        """Build a component.

        Args:
            component_dir (Path): Directory containing the component source.

        Returns:
            BuildResult: Success flag and tool output.
        """

    @abstractmethod
    async def test(self, scope: TestScope) -> TestResult:
        """Run tests for the given scope.

        Args:
            scope (TestScope): Selects which tests to run.

        Returns:
            TestResult: Aggregated pass/fail counts and per-test details.
        """

    @abstractmethod
    async def format(self, paths: list[Path]) -> None:
        """Format source files in place.

        Args:
            paths (list[Path]): Files or directories to format.
        """
