"""Behavior tests for kodo.skills and the ``use_skill`` tool (doc/SKILLS.md).

Skills are third-party, hand-installed content, so most of what matters here is
how the store behaves on input it did not write: malformed frontmatter, a stray
file, a name that tries to escape the store. The rule throughout is that a bad
skill degrades to a visible, deletable row and never raises past the caller.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.skills import SKILL_FILE, Skill, SkillDeleteError, SkillStore, load_skill, render_catalog
from kodo.tools import UseSkillTool
from kodo.toolspecs import USE_SKILL

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
