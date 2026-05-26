"""Node.js toolchain plugin: init, add_dependency, build, test, format."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from kodo.toolchains._interface import (
    ToolchainBuildResult,
    ToolchainPlugin,
    ToolchainTestResult,
    ToolchainTestScope,
)

from ._vitest import parse_vitest_stdout

__all__ = ["NodePlugin"]

_log = logging.getLogger(__name__)

_PACKAGE_JSON_TEMPLATE = """\
{{
  "name": "{name}",
  "version": "0.1.0",
  "type": "module",
  "scripts": {{
    "test": "vitest run",
    "build": "tsc --noEmit"
  }},
  "devDependencies": {{
    "vitest": "^1.0.0",
    "typescript": "^5.0.0"
  }}
}}
"""


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


class NodePlugin(ToolchainPlugin):
    """Node.js toolchain plugin backed by vitest + npm (FR-TC-04)."""

    __project_root: Path

    def __init__(self, project_root: Path) -> None:
        """Initialise the plugin for a specific project.

        Args:
            project_root (Path): Root of the Kodo project.
        """
        self.__project_root = project_root

    @property
    def name(self) -> str:
        return "node"

    @property
    def languages(self) -> list[str]:
        return ["javascript", "typescript", "node"]

    async def init(self, project_root: Path) -> None:
        """Create package.json and gen/ directory if absent.

        Args:
            project_root (Path): Root of the Kodo project.
        """
        pkg_json = project_root / "package.json"
        if not pkg_json.exists():
            proj_name = project_root.name.replace(" ", "-").lower() or "kodo-project"
            pkg_json.write_text(_PACKAGE_JSON_TEMPLATE.format(name=proj_name), encoding="utf-8")
            _log.info("Created package.json at %s", pkg_json)

        gen_dir = project_root / "gen"
        gen_dir.mkdir(exist_ok=True)

        if not shutil.which("npm"):
            _log.warning("npm not found — skipping npm install")
            return

        code, _, err = await _run("npm install", project_root)
        if code != 0:
            _log.warning("npm install failed: %s", err)

    async def add_dependency(self, name: str, version: str | None = None) -> None:
        """Add a dependency via npm.

        Args:
            name (str): Package name.
            version (str | None): Version constraint.
        """
        pkg = f"{name}@{version}" if version else name
        code, _, err = await _run(f"npm install {pkg}", self.__project_root)
        if code != 0:
            _log.warning("npm install %s failed: %s", pkg, err)
        else:
            _log.info("Installed %s", pkg)

    async def build(self, component_dir: Path) -> ToolchainBuildResult:
        """Run ``npm run build`` for the component.

        Args:
            component_dir (Path): Component directory.

        Returns:
            ToolchainBuildResult: Success flag and tool output.
        """
        pkg_json = self.__project_root / "package.json"
        if not pkg_json.exists():
            return ToolchainBuildResult(success=True, output="(no package.json — skipping build)")

        scripts: dict[str, str] = {}
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
        except (json.JSONDecodeError, OSError):
            pass

        if "build" not in scripts:
            return ToolchainBuildResult(success=True, output="(no build script defined)")

        code, stdout, stderr = await _run("npm run build", self.__project_root)
        return ToolchainBuildResult(success=code == 0, output=stdout + stderr)

    async def test(self, scope: ToolchainTestScope) -> ToolchainTestResult:
        """Run vitest for the given scope.

        Args:
            scope (ToolchainTestScope): Selects component and test kind.

        Returns:
            ToolchainTestResult: Pass/fail counts and per-test details.
        """
        test_pattern = f"gen/{scope.component}/tests" if scope.component else "gen"

        code, stdout, stderr = await _run(f"npx vitest run {test_pattern}", self.__project_root)
        _log.info("vitest exit=%d component=%s", code, scope.component)
        return parse_vitest_stdout(stdout, stderr)

    def source_filename(self, filename_hint: str) -> str:
        """Derive a TypeScript source file name from a hint.

        Args:
            filename_hint (str): Stem or description, e.g. ``'order_manager'``.

        Returns:
            str: camelCase ``.ts`` file name, e.g. ``'orderManager.ts'``.
        """
        words = filename_hint.strip().replace("-", "_").replace(" ", "_").split("_")
        camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
        return f"{camel}.ts"

    def test_filename(self, filename_hint: str) -> str:
        """Derive a Vitest test file name from a hint.

        Args:
            filename_hint (str): Stem or description of the module under test.

        Returns:
            str: camelCase file name with ``.test.ts`` suffix, e.g. ``'orderManager.test.ts'``.
        """
        words = filename_hint.strip().replace("-", "_").replace(" ", "_").split("_")
        camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
        return f"{camel}.test.ts"

    async def format(self, paths: list[Path]) -> None:
        """Format JS/TS files using prettier if available.

        Args:
            paths (list[Path]): Files or directories to format.
        """
        if (
            not shutil.which("prettier")
            and not (self.__project_root / "node_modules" / ".bin" / "prettier").exists()
        ):
            _log.warning("prettier not found — skipping format")
            return
        targets = " ".join(f'"{p}"' for p in paths)
        code, _, err = await _run(f"npx prettier --write {targets}", self.__project_root)
        if code != 0:
            _log.warning("prettier failed: %s", err)
