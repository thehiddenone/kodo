"""Behavior tests for PythonPlugin and NodePlugin.

Tests cover the pure-computation public methods (name, languages,
source_filename, test_filename, build) and the filesystem-side effects
of init(). Methods that invoke actual subprocesses (add_dependency,
test, format) are covered only for cases where the subprocess is not
required to be present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.toolchains._interface import ToolchainBuildResult
from kodo.toolchains.node._plugin import NodePlugin
from kodo.toolchains.python._plugin import PythonPlugin

# ---------------------------------------------------------------------------
# PythonPlugin — identity properties
# ---------------------------------------------------------------------------


def test_python_plugin_name() -> None:
    """
    Given a PythonPlugin,
    when name is accessed,
    then it equals 'python'.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.name == "python"


def test_python_plugin_languages_includes_python() -> None:
    """
    Given a PythonPlugin,
    when languages is accessed,
    then 'python' is in the list.
    """
    plugin = PythonPlugin(Path("."))
    assert "python" in plugin.languages


# ---------------------------------------------------------------------------
# PythonPlugin — source_filename
# ---------------------------------------------------------------------------


def test_python_source_filename_adds_py_extension() -> None:
    """
    Given a plain stem hint,
    when source_filename is called,
    then the result ends with .py.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.source_filename("order_manager").endswith(".py")


def test_python_source_filename_lowercases_hint() -> None:
    """
    Given a mixed-case hint,
    when source_filename is called,
    then the stem is lowercase.
    """
    plugin = PythonPlugin(Path("."))
    name = plugin.source_filename("OrderManager")
    assert name == name.lower()


def test_python_source_filename_replaces_spaces_with_underscores() -> None:
    """
    Given a hint with spaces,
    when source_filename is called,
    then spaces become underscores.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.source_filename("order manager") == "order_manager.py"


def test_python_source_filename_replaces_hyphens_with_underscores() -> None:
    """
    Given a hint with hyphens,
    when source_filename is called,
    then hyphens become underscores.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.source_filename("order-manager") == "order_manager.py"


def test_python_source_filename_strips_whitespace() -> None:
    """
    Given a hint with leading/trailing whitespace,
    when source_filename is called,
    then the result is trimmed.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.source_filename("  foo  ") == "foo.py"


# ---------------------------------------------------------------------------
# PythonPlugin — test_filename
# ---------------------------------------------------------------------------


def test_python_test_filename_adds_test_prefix() -> None:
    """
    Given a plain stem hint,
    when test_filename is called,
    then the result starts with 'test_'.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.test_filename("order_manager").startswith("test_")


def test_python_test_filename_ends_with_py() -> None:
    """
    Given any hint,
    when test_filename is called,
    then the result ends with .py.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.test_filename("calc").endswith(".py")


def test_python_test_filename_full_result() -> None:
    """
    Given a hint 'order_manager',
    when test_filename is called,
    then the result is 'test_order_manager.py'.
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.test_filename("order_manager") == "test_order_manager.py"


def test_python_test_filename_normalises_hint() -> None:
    """
    Given a mixed-case hint with spaces,
    when test_filename is called,
    then the result is normalised (snake_case with test_ prefix).
    """
    plugin = PythonPlugin(Path("."))
    assert plugin.test_filename("Order Manager") == "test_order_manager.py"


# ---------------------------------------------------------------------------
# PythonPlugin — build (always succeeds, no subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_build_returns_success(tmp_path: Path) -> None:
    """
    Given a Python project directory,
    when build() is called,
    then the result indicates success (Python has no compile step).
    """
    plugin = PythonPlugin(tmp_path)
    result = await plugin.build(tmp_path)
    assert isinstance(result, ToolchainBuildResult)
    assert result.success is True


@pytest.mark.asyncio
async def test_python_build_output_is_non_empty(tmp_path: Path) -> None:
    """
    Given any project directory,
    when build() is called,
    then the output string is non-empty (explains the no-build stance).
    """
    plugin = PythonPlugin(tmp_path)
    result = await plugin.build(tmp_path)
    assert result.output != ""


# ---------------------------------------------------------------------------
# PythonPlugin — init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_init_creates_pyproject_toml(tmp_path: Path) -> None:
    """
    Given a fresh project directory with no pyproject.toml,
    when init() is called,
    then pyproject.toml is created at the project root.
    """
    plugin = PythonPlugin(tmp_path)
    await plugin.init(tmp_path)
    assert (tmp_path / "pyproject.toml").exists()


@pytest.mark.asyncio
async def test_python_init_creates_gen_directory(tmp_path: Path) -> None:
    """
    Given a fresh project directory,
    when init() is called,
    then a gen/ directory is created.
    """
    plugin = PythonPlugin(tmp_path)
    await plugin.init(tmp_path)
    assert (tmp_path / "gen").is_dir()


@pytest.mark.asyncio
async def test_python_init_creates_gen_init_py(tmp_path: Path) -> None:
    """
    Given a fresh project directory,
    when init() is called,
    then gen/__init__.py exists.
    """
    plugin = PythonPlugin(tmp_path)
    await plugin.init(tmp_path)
    assert (tmp_path / "gen" / "__init__.py").exists()


@pytest.mark.asyncio
async def test_python_init_idempotent_when_pyproject_exists(tmp_path: Path) -> None:
    """
    Given a project directory that already has pyproject.toml,
    when init() is called again,
    then the existing pyproject.toml is not overwritten.
    """
    existing = tmp_path / "pyproject.toml"
    existing.write_text("# custom", encoding="utf-8")
    plugin = PythonPlugin(tmp_path)
    await plugin.init(tmp_path)
    assert existing.read_text(encoding="utf-8") == "# custom"


@pytest.mark.asyncio
async def test_python_init_pyproject_contains_project_name(tmp_path: Path) -> None:
    """
    Given a project directory named 'my-project',
    when init() is called,
    then pyproject.toml contains 'my-project' as the package name.
    """
    project_dir = tmp_path / "my-project"
    project_dir.mkdir()
    plugin = PythonPlugin(project_dir)
    await plugin.init(project_dir)
    content = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "my-project" in content


# ---------------------------------------------------------------------------
# NodePlugin — identity properties
# ---------------------------------------------------------------------------


def test_node_plugin_name() -> None:
    """
    Given a NodePlugin,
    when name is accessed,
    then it equals 'node'.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.name == "node"


def test_node_plugin_languages_includes_javascript_and_typescript() -> None:
    """
    Given a NodePlugin,
    when languages is accessed,
    then both 'javascript' and 'typescript' are in the list.
    """
    plugin = NodePlugin(Path("."))
    assert "javascript" in plugin.languages
    assert "typescript" in plugin.languages


# ---------------------------------------------------------------------------
# NodePlugin — source_filename
# ---------------------------------------------------------------------------


def test_node_source_filename_adds_ts_extension() -> None:
    """
    Given a plain hint,
    when source_filename is called,
    then the result ends with .ts.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.source_filename("order_manager").endswith(".ts")


def test_node_source_filename_converts_to_camel_case() -> None:
    """
    Given a snake_case hint,
    when source_filename is called,
    then the result is camelCase.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.source_filename("order_manager") == "orderManager.ts"


def test_node_source_filename_handles_single_word() -> None:
    """
    Given a single-word hint,
    when source_filename is called,
    then the result is lowercase with .ts extension.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.source_filename("calculator") == "calculator.ts"


def test_node_source_filename_handles_hyphenated_hint() -> None:
    """
    Given a hyphenated hint,
    when source_filename is called,
    then hyphens become camelCase word boundaries.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.source_filename("order-manager") == "orderManager.ts"


def test_node_source_filename_handles_spaced_hint() -> None:
    """
    Given a space-separated hint,
    when source_filename is called,
    then spaces become camelCase word boundaries.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.source_filename("order manager") == "orderManager.ts"


# ---------------------------------------------------------------------------
# NodePlugin — test_filename
# ---------------------------------------------------------------------------


def test_node_test_filename_adds_test_ts_suffix() -> None:
    """
    Given a plain hint,
    when test_filename is called,
    then the result ends with .test.ts.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.test_filename("order_manager").endswith(".test.ts")


def test_node_test_filename_converts_to_camel_case() -> None:
    """
    Given a snake_case hint,
    when test_filename is called,
    then the stem is camelCase.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.test_filename("order_manager") == "orderManager.test.ts"


def test_node_test_filename_full_result() -> None:
    """
    Given a multi-word hint,
    when test_filename is called,
    then the full result follows the camelCase.test.ts convention.
    """
    plugin = NodePlugin(Path("."))
    assert plugin.test_filename("price calculator") == "priceCalculator.test.ts"


# ---------------------------------------------------------------------------
# NodePlugin — build (no package.json → skips gracefully)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_build_skips_gracefully_when_no_package_json(tmp_path: Path) -> None:
    """
    Given a project directory without package.json,
    when build() is called,
    then the result indicates success (no build to run).
    """
    plugin = NodePlugin(tmp_path)
    result = await plugin.build(tmp_path)
    assert isinstance(result, ToolchainBuildResult)
    assert result.success is True


@pytest.mark.asyncio
async def test_node_build_skips_when_no_build_script(tmp_path: Path) -> None:
    """
    Given a package.json with no 'build' script,
    when build() is called,
    then the result indicates success.
    """
    import json

    pkg = {"name": "test", "scripts": {"test": "vitest run"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    plugin = NodePlugin(tmp_path)
    result = await plugin.build(tmp_path)
    assert result.success is True


@pytest.mark.asyncio
async def test_node_build_handles_invalid_json_package_gracefully(tmp_path: Path) -> None:
    """
    Given a package.json that is not valid JSON,
    when build() is called,
    then the result indicates success (JSON error is swallowed gracefully).
    """
    (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
    plugin = NodePlugin(tmp_path)
    result = await plugin.build(tmp_path)
    assert result.success is True


# ---------------------------------------------------------------------------
# NodePlugin — init (filesystem effects only, npm not required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_init_creates_package_json(tmp_path: Path) -> None:
    """
    Given a fresh project directory,
    when init() is called,
    then package.json is created at the project root.
    """
    plugin = NodePlugin(tmp_path)
    await plugin.init(tmp_path)
    assert (tmp_path / "package.json").exists()


@pytest.mark.asyncio
async def test_node_init_creates_gen_directory(tmp_path: Path) -> None:
    """
    Given a fresh project directory,
    when init() is called,
    then a gen/ directory is created.
    """
    plugin = NodePlugin(tmp_path)
    await plugin.init(tmp_path)
    assert (tmp_path / "gen").is_dir()


@pytest.mark.asyncio
async def test_node_init_idempotent_when_package_json_exists(tmp_path: Path) -> None:
    """
    Given a project directory that already has package.json,
    when init() is called again,
    then the existing package.json is not overwritten.
    """
    existing = tmp_path / "package.json"
    existing.write_text('{"name":"custom"}', encoding="utf-8")
    plugin = NodePlugin(tmp_path)
    await plugin.init(tmp_path)
    assert existing.read_text(encoding="utf-8") == '{"name":"custom"}'


@pytest.mark.asyncio
async def test_node_init_package_json_contains_project_name(tmp_path: Path) -> None:
    """
    Given a project directory named 'my-app',
    when init() is called,
    then package.json contains 'my-app' as the project name.
    """
    project_dir = tmp_path / "my-app"
    project_dir.mkdir()
    plugin = NodePlugin(project_dir)
    await plugin.init(project_dir)
    content = (project_dir / "package.json").read_text(encoding="utf-8")
    assert "my-app" in content


# ---------------------------------------------------------------------------
# PythonPlugin — subprocess methods (add_dependency, test, format)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_add_dependency_with_invalid_package_does_not_raise(
    tmp_path: Path,
) -> None:
    """
    Given PythonPlugin and a package name that does not exist on PyPI,
    when add_dependency() is called,
    then no exception is raised (failure is logged as a warning).
    """
    plugin = PythonPlugin(tmp_path)
    await plugin.add_dependency("__kodo_nonexistent_test_pkg_xyz9999__")


@pytest.mark.asyncio
async def test_python_test_returns_toolchain_result_when_no_gen_dir(
    tmp_path: Path,
) -> None:
    """
    Given a project directory with no gen/ directory,
    when test() is called with no component,
    then a ToolchainTestResult is returned without raising.
    """
    from kodo.toolchains._interface import ToolchainTestResult, ToolchainTestScope

    plugin = PythonPlugin(tmp_path)
    scope = ToolchainTestScope(component=None)
    result = await plugin.test(scope)
    assert isinstance(result, ToolchainTestResult)


@pytest.mark.asyncio
async def test_python_format_with_empty_path_list_does_not_raise(
    tmp_path: Path,
) -> None:
    """
    Given an empty list of paths,
    when format() is called,
    then no exception is raised.
    """
    plugin = PythonPlugin(tmp_path)
    await plugin.format([])


@pytest.mark.asyncio
async def test_python_format_with_python_file_does_not_raise(
    tmp_path: Path,
) -> None:
    """
    Given a Python file,
    when format() is called,
    then no exception is raised (ruff formats it in place).
    """
    py_file = tmp_path / "sample.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    plugin = PythonPlugin(tmp_path)
    await plugin.format([py_file])


# ---------------------------------------------------------------------------
# NodePlugin — subprocess methods (test, add_dependency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_test_returns_toolchain_result(tmp_path: Path) -> None:
    """
    Given a project directory,
    when test() is called (even if npx/vitest is absent),
    then a ToolchainTestResult is returned without raising.
    """
    from kodo.toolchains._interface import ToolchainTestResult, ToolchainTestScope

    plugin = NodePlugin(tmp_path)
    scope = ToolchainTestScope(component=None)
    result = await plugin.test(scope)
    assert isinstance(result, ToolchainTestResult)


@pytest.mark.asyncio
async def test_node_add_dependency_does_not_raise(tmp_path: Path) -> None:
    """
    Given a package name,
    when add_dependency() is called (npm may or may not be installed),
    then no exception is raised.
    """
    plugin = NodePlugin(tmp_path)
    await plugin.add_dependency("__kodo_nonexistent_test_pkg__")
