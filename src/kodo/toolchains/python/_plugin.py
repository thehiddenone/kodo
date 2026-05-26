"""Python toolchain plugin: init, add_dependency, build, test, format."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from kodo.toolchains._interface import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)

from ._pytest import parse_pytest_stdout

__all__ = ["PythonPlugin"]

_log = logging.getLogger(__name__)

_PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.pytest.ini_options]
testpaths = ["gen"]
"""


def _uv_or_pip() -> str:
    """Return 'uv pip' if uv is on PATH, else 'pip'."""
    return "uv pip" if shutil.which("uv") else "pip"


async def _run(cmd: str, cwd: Path) -> tuple[int, str, str]:
    """Run a shell command; return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    out_b, err_b = await proc.communicate()
    return (proc.returncode or 0, out_b.decode(errors="replace"), err_b.decode(errors="replace"))


class PythonPlugin(ToolchainPlugin):
    """Python toolchain plugin backed by pytest + ruff + uv/pip (FR-TC-03)."""

    __project_root: Path

    def __init__(self, project_root: Path) -> None:
        """Initialise the plugin for a specific project.

        Args:
            project_root (Path): Root of the Kodo project.
        """
        self.__project_root = project_root

    @property
    def name(self) -> str:
        return "python"

    @property
    def languages(self) -> list[str]:
        return ["python"]

    async def init(self, project_root: Path) -> None:
        """Create pyproject.toml and gen/ directory if absent.

        Args:
            project_root (Path): Root of the Kodo project.
        """
        pyproject = project_root / "pyproject.toml"
        if not pyproject.exists():
            proj_name = project_root.name.replace(" ", "-").lower() or "kodo-project"
            pyproject.write_text(_PYPROJECT_TEMPLATE.format(name=proj_name), encoding="utf-8")
            _log.info("Created pyproject.toml at %s", pyproject)

        gen_dir = project_root / "gen"
        gen_dir.mkdir(exist_ok=True)
        init = gen_dir / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")

    async def add_dependency(self, name: str, version: str | None = None) -> None:
        """Add a dependency via uv or pip.

        Args:
            name (str): Package name.
            version (str | None): Version constraint.
        """
        pkg = f"{name}=={version}" if version else name
        installer = _uv_or_pip()
        code, out, err = await _run(f"{installer} install {pkg}", self.__project_root)
        if code != 0:
            _log.warning("add_dependency failed for %s: %s", pkg, err)
        else:
            _log.info("Installed %s", pkg)

    async def build(self, component_dir: Path) -> ToolchainBuildResult:
        """No-op build for pure-Python components.

        Args:
            component_dir (Path): Component directory (unused for Python).

        Returns:
            ToolchainBuildResult: Always succeeds; no compilation needed.
        """
        return ToolchainBuildResult(success=True, output="(no build step for Python)")

    async def test(self, scope: ToolchainTestScope) -> ToolchainTestResult:
        """Run pytest for the given scope.

        Args:
            scope (ToolchainTestScope): Selects component and test kind.

        Returns:
            ToolchainTestResult: Pass/fail counts and per-test details.
        """
        test_path = f"gen/{scope.component}/tests" if scope.component else "gen"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            report_path = Path(tmp.name)

        try:
            cmd = f"python -m pytest {test_path} -v --tb=short --no-header -q"
            code, stdout, stderr = await _run(cmd, self.__project_root)
            _log.info("pytest exit=%d component=%s", code, scope.component)
            return parse_pytest_stdout(stdout, stderr)
        finally:
            if report_path.exists():
                report_path.unlink(missing_ok=True)

    def source_filename(self, filename_hint: str) -> str:
        """Derive a Python source file name from a hint.

        Args:
            filename_hint (str): Stem or description, e.g. ``'order_manager'``.

        Returns:
            str: Snake-case ``.py`` file name, e.g. ``'order_manager.py'``.
        """
        stem = filename_hint.strip().lower().replace(" ", "_").replace("-", "_")
        return f"{stem}.py"

    def test_filename(self, filename_hint: str) -> str:
        """Derive a pytest test file name from a hint.

        Args:
            filename_hint (str): Stem or description of the module under test.

        Returns:
            str: Snake-case file name with ``test_`` prefix, e.g. ``'test_order_manager.py'``.
        """
        stem = filename_hint.strip().lower().replace(" ", "_").replace("-", "_")
        return f"test_{stem}.py"

    async def format(self, paths: list[Path]) -> None:
        """Format Python files using ruff.

        Args:
            paths (list[Path]): Files or directories to format.
        """
        if not shutil.which("ruff"):
            _log.warning("ruff not found — skipping format")
            return
        targets = " ".join(f'"{p}"' for p in paths)
        code, _, err = await _run(f"ruff format {targets}", self.__project_root)
        if code != 0:
            _log.warning("ruff format failed: %s", err)
