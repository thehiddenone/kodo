"""Behavior tests for kodo.skills and the ``use_skill`` tool (doc/SKILLS.md).

Skills are third-party, hand-installed content, so most of what matters here is
how the store behaves on input it did not write: malformed frontmatter, a stray
file, a name that tries to escape the store. The rule throughout is that a bad
skill degrades to a visible, deletable row and never raises past the caller.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kodo.skills import (
    SKILL_FILE,
    GitNotAvailableError,
    Skill,
    SkillDeleteError,
    SkillInstallError,
    SkillStore,
    install_local_skill,
    install_skills,
    load_skill,
    render_catalog,
    scan_repository,
)
from kodo.tools import UseSkillTool
from kodo.toolspecs import USE_SKILL

_GIT_AVAILABLE = shutil.which("git") is not None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install(root: Path, name: str, text: str) -> Path:
    """Write a skill directory holding *text* as its SKILL.md."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / SKILL_FILE).write_text(text, encoding="utf-8")
    return directory


def _skill_md(name: str, description: str, body: str = "Do the thing.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def _git_repo(tmp_path: Path, dirname: str, files: dict[str, str]) -> Path:
    """A local git repo at ``tmp_path/dirname`` holding *files* (path -> content), one commit.

    ``git clone`` accepts a local path as its URL, which is what lets
    :func:`~kodo.skills.scan_repository`/:func:`~kodo.skills.install_skills`
    be exercised without any network access.
    """
    repo = tmp_path / dirname
    repo.mkdir(parents=True)
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    env_args = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
    subprocess.run(["git", *env_args, "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", *env_args, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env_args, "commit", "--quiet", "-m", "init"], cwd=repo, check=True)
    return repo


pytestmark_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git CLI not on PATH")


# ---------------------------------------------------------------------------
# load_skill
# ---------------------------------------------------------------------------


def test_load_skill_reads_name_description_and_body(tmp_path: Path) -> None:
    directory = _install(
        tmp_path, "pdf", _skill_md("pdf", "Work with PDF files.", "# Guide\n\nUse pypdf.")
    )
    skill = load_skill(directory)
    assert skill.name == "pdf"
    assert skill.description == "Work with PDF files."
    assert skill.body == "# Guide\n\nUse pypdf."
    assert skill.usable
    assert skill.skill_md == directory / SKILL_FILE


def test_load_skill_identity_is_the_directory_not_the_frontmatter(tmp_path: Path) -> None:
    """The directory is what Open/Delete/``use_skill`` all act on."""
    directory = _install(tmp_path, "my-pdf-skill", _skill_md("my-pdf-skill", "Desc."))
    assert load_skill(directory).name == "my-pdf-skill"


def test_load_skill_rejects_a_name_that_disagrees_with_the_directory(tmp_path: Path) -> None:
    directory = _install(tmp_path, "pdf", _skill_md("pdfs", "Desc."))
    skill = load_skill(directory)
    assert not skill.usable
    assert "does not match the directory name" in skill.error


def test_load_skill_accepts_a_skill_that_declares_no_name(tmp_path: Path) -> None:
    """``name`` is redundant with the directory, so its absence is not an error."""
    directory = _install(tmp_path, "pdf", "---\ndescription: Desc.\n---\n\nBody.\n")
    assert load_skill(directory).usable


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no frontmatter at all\n", "no `---` YAML frontmatter"),
        ("---\nname: pdf\n---\n\nBody.\n", "no `description`"),
        ("---\nname: pdf\ndescription: Desc.\n---\n\n", "no instructions"),
    ],
)
def test_load_skill_reports_malformed_content_instead_of_raising(
    tmp_path: Path, text: str, expected: str
) -> None:
    skill = load_skill(_install(tmp_path, "pdf", text))
    assert not skill.usable
    assert expected in skill.error
    assert skill.name == "pdf", "a broken skill still lists under its directory name"


def test_load_skill_reports_a_directory_with_no_skill_md(tmp_path: Path) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()
    skill = load_skill(directory)
    assert not skill.usable
    assert SKILL_FILE in skill.error


def test_load_skill_survives_undecodable_bytes(tmp_path: Path) -> None:
    """A skill copied from anywhere may not be UTF-8; it must not kill the listing."""
    directory = tmp_path / "binary"
    directory.mkdir()
    (directory / SKILL_FILE).write_bytes(
        b"---\nname: binary\ndescription: \xff\xfe bad\n---\n\nBody.\n"
    )
    assert load_skill(directory).name == "binary"


# ---------------------------------------------------------------------------
# Frontmatter shapes real skills use
# ---------------------------------------------------------------------------


def test_load_skill_keeps_colons_inside_a_description(tmp_path: Path) -> None:
    directory = _install(tmp_path, "pdf", _skill_md("pdf", "Use when: the user says PDF."))
    assert load_skill(directory).description == "Use when: the user says PDF."


def test_load_skill_strips_quotes_around_a_value(tmp_path: Path) -> None:
    directory = _install(
        tmp_path, "pdf", '---\nname: pdf\ndescription: "Quoted desc."\n---\n\nB.\n'
    )
    assert load_skill(directory).description == "Quoted desc."


def test_load_skill_folds_a_block_scalar_description(tmp_path: Path) -> None:
    directory = _install(
        tmp_path,
        "pdf",
        "---\nname: pdf\ndescription: >\n  First line\n  second line\n---\n\nBody.\n",
    )
    assert load_skill(directory).description == "First line second line"


def test_load_skill_ignores_unrelated_frontmatter_keys(tmp_path: Path) -> None:
    """Real skills carry ``license``/``allowed-tools``; Kōdo reads neither."""
    directory = _install(
        tmp_path,
        "pdf",
        "---\nname: pdf\nlicense: Proprietary. LICENSE.txt has terms\n"
        "allowed-tools:\n  - Read\n  - Write\ndescription: Desc.\n---\n\nBody.\n",
    )
    assert load_skill(directory).usable


# ---------------------------------------------------------------------------
# SkillStore
# ---------------------------------------------------------------------------


def test_store_lists_skills_name_sorted(tmp_path: Path) -> None:
    _install(tmp_path, "zebra", _skill_md("zebra", "Z."))
    _install(tmp_path, "alpha", _skill_md("alpha", "A."))
    assert [s.name for s in SkillStore(tmp_path).entries()] == ["alpha", "zebra"]


def test_store_skips_stray_files_but_lists_broken_directories(tmp_path: Path) -> None:
    _install(tmp_path, "good", _skill_md("good", "G."))
    _install(tmp_path, "broken", "junk\n")
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")

    entries = SkillStore(tmp_path).entries()

    assert [s.name for s in entries] == ["broken", "good"]
    assert [s.name for s in SkillStore(tmp_path).usable()] == ["good"]


def test_store_on_a_missing_root_is_empty_not_an_error(tmp_path: Path) -> None:
    assert SkillStore(tmp_path / "nope").entries() == []


def test_store_ensure_root_creates_the_directory(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = SkillStore(root)
    assert store.ensure_root() == root
    assert root.is_dir()
    store.ensure_root()  # idempotent


def test_store_get_returns_none_for_a_broken_skill(tmp_path: Path) -> None:
    """A broken skill exists for the panel and not at all for an agent."""
    _install(tmp_path, "broken", "junk\n")
    assert SkillStore(tmp_path).get("broken") is None


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "../etc", "sub/skill", "sub\\skill"])
def test_store_get_refuses_a_name_that_is_not_a_direct_child(tmp_path: Path, name: str) -> None:
    _install(tmp_path, "pdf", _skill_md("pdf", "P."))
    assert SkillStore(tmp_path).get(name) is None


def test_store_delete_removes_the_whole_directory(tmp_path: Path) -> None:
    directory = _install(tmp_path, "pdf", _skill_md("pdf", "P."))
    (directory / "scripts").mkdir()
    (directory / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")

    SkillStore(tmp_path).delete("pdf")

    assert not directory.exists()


def test_store_delete_removes_a_broken_skill_too(tmp_path: Path) -> None:
    """Deleting is how a user fixes a broken skill, so it must not need a valid one."""
    directory = _install(tmp_path, "broken", "junk\n")
    SkillStore(tmp_path).delete("broken")
    assert not directory.exists()


@pytest.mark.parametrize("name", ["", "..", "../victim", "sub/skill"])
def test_store_delete_refuses_to_escape_the_store(tmp_path: Path, name: str) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keepme.txt").write_text("important", encoding="utf-8")

    with pytest.raises(SkillDeleteError):
        SkillStore(root).delete(name)

    assert (victim / "keepme.txt").exists()


def test_store_delete_of_an_unknown_name_raises(tmp_path: Path) -> None:
    with pytest.raises(SkillDeleteError, match="ghost"):
        SkillStore(tmp_path).delete("ghost")


def test_store_delete_does_not_follow_a_symlink_out_of_the_store(tmp_path: Path) -> None:
    """``__resolve`` compares the *resolved* parent, so a symlinked entry is refused."""
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keepme.txt").write_text("important", encoding="utf-8")
    try:
        (root / "sneaky").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")

    with pytest.raises(SkillDeleteError):
        SkillStore(root).delete("sneaky")

    assert (outside / "keepme.txt").exists()


# ---------------------------------------------------------------------------
# render_catalog
# ---------------------------------------------------------------------------


def test_catalog_lists_one_row_per_skill_with_the_full_description() -> None:
    long_description = "Use this skill whenever " + "x" * 400
    skills = [
        Skill(name="pdf", description=long_description, root=Path("/s/pdf"), body="B"),
        Skill(name="docx", description="Word files.", root=Path("/s/docx"), body="B"),
    ]

    catalog = render_catalog(skills)

    assert "## Available skills" in catalog
    assert f"- **pdf** — {long_description}" in catalog, "descriptions are the routing signal"
    assert "- **docx** — Word files." in catalog
    assert "B" not in catalog.replace("**", ""), "bodies never reach the catalog"


def test_catalog_collapses_a_multiline_description_to_one_row() -> None:
    skill = Skill(name="pdf", description="First\nsecond\n  third", root=Path("/s"), body="B")
    assert "- **pdf** — First second third" in render_catalog([skill])


def test_catalog_with_no_skills_says_so_explicitly() -> None:
    catalog = render_catalog([])
    assert "## Available skills" in catalog
    assert "No skills are installed." in catalog


# ---------------------------------------------------------------------------
# The use_skill tool
# ---------------------------------------------------------------------------


class _Context:
    """The two ``ToolContext`` fields ``UseSkillTool`` actually reads."""

    agent_name = "problem_solver"
    session_id = "test-session"


async def _use_skill(name: object) -> dict[str, object]:
    tool = UseSkillTool(_Context())  # type: ignore[arg-type]
    result = json.loads(await tool.handle({"name": name} if name is not None else {}))
    assert isinstance(result, dict)
    return result


@pytest.fixture
def skills_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``kodo_skills_dir()`` at a temp store by relocating ``$HOME``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".kodo" / "skills"
    root.mkdir(parents=True)
    return root


@pytest.mark.asyncio
async def test_use_skill_returns_the_body_and_the_directory_path(skills_home: Path) -> None:
    directory = _install(skills_home, "pdf", _skill_md("pdf", "PDF work.", "Read REFERENCE.md."))

    result = await _use_skill("pdf")

    assert result["name"] == "pdf"
    assert result["description"] == "PDF work."
    assert result["instructions"] == "Read REFERENCE.md."
    assert result["path"] == str(directory), "the path is how companion files get opened"


@pytest.mark.asyncio
async def test_use_skill_output_matches_its_declared_schema(skills_home: Path) -> None:
    _install(skills_home, "pdf", _skill_md("pdf", "PDF work."))
    result = await _use_skill("pdf")
    required = USE_SKILL.output_schema["required"]
    assert isinstance(required, list)
    assert set(required) <= set(result)


@pytest.mark.asyncio
async def test_use_skill_names_the_alternatives_when_the_skill_is_unknown(
    skills_home: Path,
) -> None:
    _install(skills_home, "pdf", _skill_md("pdf", "PDF work."))
    result = await _use_skill("pdfs")
    assert "pdf" in str(result["error"])


@pytest.mark.asyncio
async def test_use_skill_reports_an_empty_store_clearly(skills_home: Path) -> None:
    assert "none installed" in str((await _use_skill("pdf"))["error"])


@pytest.mark.asyncio
async def test_use_skill_refuses_a_broken_skill(skills_home: Path) -> None:
    _install(skills_home, "broken", "junk\n")
    assert "error" in await _use_skill("broken")


@pytest.mark.asyncio
async def test_use_skill_refuses_a_traversing_name(skills_home: Path) -> None:
    (skills_home.parent / "etc").mkdir(parents=True, exist_ok=True)
    assert "error" in await _use_skill("../etc")


@pytest.mark.asyncio
async def test_use_skill_with_no_name_is_an_error_not_a_crash(skills_home: Path) -> None:
    assert "error" in await _use_skill(None)


@pytest.mark.asyncio
async def test_use_skill_sees_a_skill_installed_after_the_session_started(
    skills_home: Path,
) -> None:
    """The store is re-scanned per call — that is the whole point of no caching."""
    assert "error" in await _use_skill("late")
    _install(skills_home, "late", _skill_md("late", "Arrived later."))
    assert (await _use_skill("late"))["description"] == "Arrived later."


# ---------------------------------------------------------------------------
# require_git / scan_repository / install_skills (doc/SKILLS.md §2)
# ---------------------------------------------------------------------------


def test_require_git_raises_when_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(GitNotAvailableError):
        from kodo.skills._install import require_git

        require_git()


def test_scan_repository_raises_when_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(GitNotAvailableError):
        scan_repository("https://example.invalid/whatever.git")


def test_install_skills_raises_when_git_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(GitNotAvailableError):
        install_skills("https://example.invalid/whatever.git", {"pdf": False}, tmp_path)


@pytestmark_git
def test_scan_repository_finds_multiple_skills_and_skips_broken_ones(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path,
        "skillpack",
        {
            "pdf/SKILL.md": _skill_md("pdf", "Work with PDF files."),
            "docx/SKILL.md": _skill_md("docx", "Work with Word files."),
            "broken/SKILL.md": "junk, no frontmatter\n",
            "README.md": "not a skill\n",
        },
    )

    found = {s.name: s for s in scan_repository(str(repo))}

    assert set(found) == {"pdf", "docx"}
    assert found["pdf"].description == "Work with PDF files."
    assert all(s.usable for s in found.values())


@pytestmark_git
def test_scan_repository_names_a_root_level_skill_after_the_repo(tmp_path: Path) -> None:
    """A ``SKILL.md`` at the clone root has no directory of its own to be named after."""
    repo = _git_repo(
        tmp_path, "my-cool-skill", {"SKILL.md": "---\ndescription: Root skill.\n---\n\nBody.\n"}
    )

    found = scan_repository(str(repo))

    assert [s.name for s in found] == ["my-cool-skill"]


@pytestmark_git
def test_scan_repository_raises_for_an_unreachable_repo(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError):
        scan_repository(str(tmp_path / "does-not-exist"))


@pytestmark_git
def test_install_skills_copies_the_whole_directory_including_companions(tmp_path: Path) -> None:
    repo = _git_repo(
        tmp_path,
        "skillpack",
        {
            "pdf/SKILL.md": _skill_md("pdf", "Work with PDF files.", "See REFERENCE.md."),
            "pdf/REFERENCE.md": "Advanced notes.",
        },
    )
    skills_root = tmp_path / "installed"

    result = install_skills(str(repo), {"pdf": False}, skills_root)

    assert result.installed == ["pdf"]
    assert (skills_root / "pdf" / SKILL_FILE).is_file()
    assert (skills_root / "pdf" / "REFERENCE.md").read_text(encoding="utf-8") == "Advanced notes."


@pytestmark_git
def test_install_skills_reports_missing_for_a_name_not_in_the_repo(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "skillpack", {"pdf/SKILL.md": _skill_md("pdf", "P.")})
    result = install_skills(str(repo), {"ghost": False}, tmp_path / "installed")
    assert result.missing == ["ghost"]
    assert result.installed == []


@pytestmark_git
def test_install_skills_conflicts_without_overwrite_and_leaves_existing_untouched(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path, "skillpack", {"pdf/SKILL.md": _skill_md("pdf", "New version.")})
    skills_root = tmp_path / "installed"
    _install(skills_root, "pdf", _skill_md("pdf", "Old version."))

    result = install_skills(str(repo), {"pdf": False}, skills_root)

    assert result.conflicts == ["pdf"]
    assert result.installed == []
    assert load_skill(skills_root / "pdf").description == "Old version."


@pytestmark_git
def test_install_skills_overwrites_when_confirmed(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "skillpack", {"pdf/SKILL.md": _skill_md("pdf", "New version.")})
    skills_root = tmp_path / "installed"
    _install(skills_root, "pdf", _skill_md("pdf", "Old version."))

    result = install_skills(str(repo), {"pdf": True}, skills_root)

    assert result.installed == ["pdf"]
    assert load_skill(skills_root / "pdf").description == "New version."


# ---------------------------------------------------------------------------
# install_local_skill (doc/SKILLS.md §2)
# ---------------------------------------------------------------------------


def test_install_local_skill_from_a_directory(tmp_path: Path) -> None:
    source = _install(tmp_path / "source", "pdf", _skill_md("pdf", "Work with PDF files."))
    skills_root = tmp_path / "installed"

    result = install_local_skill(str(source), skills_root, overwrite=False)

    assert result.installed == ["pdf"]
    assert result.conflicts == []
    assert load_skill(skills_root / "pdf").description == "Work with PDF files."


def test_install_local_skill_from_a_direct_skill_md_path(tmp_path: Path) -> None:
    source = _install(tmp_path / "source", "pdf", _skill_md("pdf", "Work with PDF files."))
    skills_root = tmp_path / "installed"

    result = install_local_skill(str(source / SKILL_FILE), skills_root, overwrite=False)

    assert result.installed == ["pdf"]
    assert load_skill(skills_root / "pdf").description == "Work with PDF files."


def test_install_local_skill_copies_companion_files(tmp_path: Path) -> None:
    source = _install(
        tmp_path / "source", "pdf", _skill_md("pdf", "Work with PDF files.", "See REFERENCE.md.")
    )
    (source / "REFERENCE.md").write_text("Advanced notes.", encoding="utf-8")
    skills_root = tmp_path / "installed"

    install_local_skill(str(source), skills_root, overwrite=False)

    assert (skills_root / "pdf" / "REFERENCE.md").read_text(encoding="utf-8") == "Advanced notes."


def test_install_local_skill_resolves_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path / "source", "pdf", _skill_md("pdf", "Work with PDF files."))
    monkeypatch.chdir(tmp_path)
    skills_root = tmp_path / "installed"

    result = install_local_skill("source/pdf", skills_root, overwrite=False)

    assert result.installed == ["pdf"]


def test_install_local_skill_does_not_scan_recursively(tmp_path: Path) -> None:
    """Unlike the repo flow, a bundle of skills under one directory is not discovered —
    the caller must point this at each skill's own subdirectory (doc/SKILLS.md §2)."""
    bundle = tmp_path / "bundle"
    _install(bundle, "pdf", _skill_md("pdf", "P."))
    _install(bundle, "docx", _skill_md("docx", "D."))

    with pytest.raises(SkillInstallError):
        install_local_skill(str(bundle), tmp_path / "installed", overwrite=False)


def test_install_local_skill_raises_for_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(SkillInstallError):
        install_local_skill(
            str(tmp_path / "does-not-exist"), tmp_path / "installed", overwrite=False
        )


def test_install_local_skill_raises_for_a_non_skill_md_file(tmp_path: Path) -> None:
    stray = tmp_path / "README.md"
    stray.write_text("not a skill", encoding="utf-8")
    with pytest.raises(SkillInstallError):
        install_local_skill(str(stray), tmp_path / "installed", overwrite=False)


def test_install_local_skill_raises_for_a_broken_skill_md(tmp_path: Path) -> None:
    source = _install(tmp_path / "source", "pdf", "junk, no frontmatter\n")
    with pytest.raises(SkillInstallError):
        install_local_skill(str(source), tmp_path / "installed", overwrite=False)


def test_install_local_skill_conflicts_without_overwrite_and_leaves_existing_untouched(
    tmp_path: Path,
) -> None:
    source = _install(tmp_path / "source", "pdf", _skill_md("pdf", "New version."))
    skills_root = tmp_path / "installed"
    _install(skills_root, "pdf", _skill_md("pdf", "Old version."))

    result = install_local_skill(str(source), skills_root, overwrite=False)

    assert result.conflicts == ["pdf"]
    assert result.installed == []
    assert load_skill(skills_root / "pdf").description == "Old version."


def test_install_local_skill_overwrites_when_confirmed(tmp_path: Path) -> None:
    source = _install(tmp_path / "source", "pdf", _skill_md("pdf", "New version."))
    skills_root = tmp_path / "installed"
    _install(skills_root, "pdf", _skill_md("pdf", "Old version."))

    result = install_local_skill(str(source), skills_root, overwrite=True)

    assert result.installed == ["pdf"]
    assert load_skill(skills_root / "pdf").description == "New version."


def test_install_local_skill_raises_when_source_is_already_the_installed_copy(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "installed"
    _install(skills_root, "pdf", _skill_md("pdf", "P."))

    with pytest.raises(SkillInstallError):
        install_local_skill(str(skills_root / "pdf"), skills_root, overwrite=False)
