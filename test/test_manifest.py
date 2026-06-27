"""Behavior tests for kodo.project._manifest and kodo.project._layout.

Tests verify observable outcomes (parse succeeds / fails with specific errors)
rather than implementation details.
"""

from pathlib import Path

import pytest

from kodo.project import (  # noqa: I001
    ManifestError,
    ProjectLayout,
    ProjectLayoutError,
    parse_manifest,
)

# ------------------------------------------------------------------
# parse_manifest — success paths
# ------------------------------------------------------------------


def test_parse_minimal_kodo_md(tmp_path: Path) -> None:
    kodo_md = tmp_path / "kodo.md"
    kodo_md.write_text(
        "# Kodo Project\n\n## Toolchain\n\n- python\n\n## Components\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    manifest = parse_manifest(kodo_md)
    assert manifest.toolchain == "python"
    assert manifest.components == []


def test_parse_kodo_md_with_components(tmp_path: Path) -> None:
    kodo_md = tmp_path / "kodo.md"
    kodo_md.write_text(
        "# Kodo Project\n\n## Toolchain\n\n- node\n\n"
        "## Components\n\n- trading-engine\n- market-data\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    manifest = parse_manifest(kodo_md)
    assert manifest.toolchain == "node"
    assert manifest.components == ["trading-engine", "market-data"]


def test_parse_toolchain_case_insensitive(tmp_path: Path) -> None:
    kodo_md = tmp_path / "kodo.md"
    kodo_md.write_text(
        "# Kodo Project\n\n## Toolchain\n\n- Python\n\n## Components\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    manifest = parse_manifest(kodo_md)
    assert manifest.toolchain == "python"


# ------------------------------------------------------------------
# parse_manifest — failure paths
# ------------------------------------------------------------------


def test_missing_file_raises_manifest_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="not found"):
        parse_manifest(tmp_path / "kodo.md")


def test_missing_required_heading_raises_manifest_error(tmp_path: Path) -> None:
    kodo_md = tmp_path / "kodo.md"
    # Missing '# Kodo Project'
    kodo_md.write_text(
        "## Toolchain\n\n- python\n\n## Components\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="# Kodo Project"):
        parse_manifest(kodo_md)


def test_missing_toolchain_entry_raises_manifest_error(tmp_path: Path) -> None:
    kodo_md = tmp_path / "kodo.md"
    kodo_md.write_text(
        "# Kodo Project\n\n## Toolchain\n\n(empty)\n\n## Components\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="## Toolchain"):
        parse_manifest(kodo_md)


# ------------------------------------------------------------------
# ProjectLayout.validate
# ------------------------------------------------------------------


def test_validate_accepts_valid_project(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)
    layout.kodo_md.write_text(
        "# Kodo Project\n\n## Toolchain\n\n- python\n\n## Components\n\n## Settings overrides\n",
        encoding="utf-8",
    )
    layout.validate()  # must not raise


def test_validate_rejects_missing_kodo_md(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    with pytest.raises(ProjectLayoutError, match="kodo.md"):
        layout.validate()


def test_validate_rejects_kodo_md_without_marker(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    layout.kodo_dir.mkdir(parents=True, exist_ok=True)
    layout.kodo_md.write_text("No marker heading here.\n", encoding="utf-8")
    with pytest.raises(ProjectLayoutError, match="# Kodo Project"):
        layout.validate()


# ------------------------------------------------------------------
# ProjectLayout.init
# ------------------------------------------------------------------


def test_init_creates_expected_structure(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    layout.init()
    assert layout.kodo_md.exists()
    assert layout.src_dir.is_dir()
    assert layout.gen_dir.is_dir()
    assert layout.kodo_dir.is_dir()
    assert "# Kodo Project" in layout.kodo_md.read_text(encoding="utf-8")


def test_init_refuses_existing_project_without_force(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    layout.init()
    with pytest.raises(ProjectLayoutError, match="already exists"):
        layout.init()


def test_init_overwrites_with_force(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path)
    layout.init()
    layout.init(force=True)  # must not raise
    assert layout.kodo_md.exists()
