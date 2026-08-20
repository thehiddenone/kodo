"""One installed Agent Skill: its identity, metadata, and SKILL.md parser.

A skill is a **directory** under ``~/.kodo/skills`` holding a ``SKILL.md`` in
the open Agent Skill format — YAML frontmatter carrying at least ``name`` and
``description``, followed by the instruction body — plus whatever bundled files
that body references (``REFERENCE.md``, ``scripts/``, ``references/``,
``assets/``; see doc/SKILLS.md).

Everything here treats a skill as **untrusted input**. Skills are installed by
hand, from anywhere, so a malformed one must degrade to a visible broken row in
the Kōdo Settings panel rather than raise past the caller: :func:`load_skill`
never raises, it returns a :class:`Skill` whose :attr:`Skill.error` says what is
wrong. Only error-free skills reach an agent's prompt catalog.

The frontmatter parser is deliberately its own, not shared with
:mod:`kodo.subagents`'s: that one parses first-party agent files and may fail
loudly on anything it does not recognise, while this one parses third-party
text and must always produce *something*. It covers the shapes real skills use
— plain scalars, quoted scalars, ``>``/``|`` block scalars, and block
sequences — and ignores anything else rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SKILL_FILE", "Skill", "load_skill"]

# The one file that makes a directory a skill.
SKILL_FILE = "SKILL.md"

_FRONT_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)

# A frontmatter key line: ``key:`` optionally followed by a value on the same
# line. Keys are unquoted and may contain ``-`` (``allowed-tools``).
_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+):(?P<rest>.*)$")

# How much of a skill's body is worth showing a model in one tool result. Real
# skills run a few hundred lines; this only guards against a pathological file.
_MAX_BODY_CHARS = 200_000


@dataclass(frozen=True)
class Skill:
    """One skill directory under the skills root.

    Attributes:
        name: The skill's identity — its **directory name**, not the
            frontmatter ``name``. The directory is what the user created, what
            the Settings panel's Open/Delete act on, and what ``use_skill``
            looks up, so making it the key keeps all three unambiguous even
            when two skills declare the same frontmatter ``name``. A
            frontmatter ``name`` that disagrees is reported through
            :attr:`error` rather than silently preferred.
        description: The frontmatter ``description`` — the *only* thing an
            agent sees about this skill until it decides to load it, so a skill
            without one is useless to the model and is treated as broken.
        root: Absolute path to the skill's directory. Handed to the model
            alongside the body so it can open the files that body references
            (an absolute path resolves unrestricted through
            ``LogicalPathResolver``; see doc/SKILLS.md §4).
        body: Everything after the frontmatter — the instructions ``use_skill``
            returns. Empty when the skill is broken.
        error: Empty for a usable skill; otherwise a one-sentence, user-facing
            explanation of why it is not usable. Broken skills are still listed
            (so the panel can show and delete them) but are never offered to an
            agent.
    """

    name: str
    description: str
    root: Path
    body: str
    error: str = ""

    @property
    def usable(self) -> bool:
        """Whether this skill can be offered to an agent (no load error)."""
        return not self.error

    @property
    def skill_md(self) -> Path:
        """Absolute path to this skill's ``SKILL.md``."""
        return self.root / SKILL_FILE


def load_skill(directory: Path) -> Skill:
    """Load the skill in *directory*, never raising.

    Args:
        directory: A directory directly under the skills root.

    Returns:
        Skill: Populated on success; carrying a non-empty
            :attr:`Skill.error` (and empty ``description``/``body``) when the
            directory holds no readable, well-formed ``SKILL.md``.
    """
    name = directory.name
    path = directory / SKILL_FILE

    if not path.is_file():
        return _broken(name, directory, f"No {SKILL_FILE} in this directory.")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _broken(name, directory, f"{SKILL_FILE} could not be read: {exc.strerror or exc}.")

    match = _FRONT_RE.match(text)
    if match is None:
        return _broken(name, directory, f"{SKILL_FILE} has no `---` YAML frontmatter block.")

    fields = _parse_frontmatter(match.group(1))
    body = text[match.end() :].strip()

    description = fields.get("description", "").strip()
    if not description:
        return _broken(name, directory, "Frontmatter has no `description`.")
    if not body:
        return _broken(name, directory, f"{SKILL_FILE} has frontmatter but no instructions.")

    declared = fields.get("name", "").strip()
    if declared and declared != name:
        return _broken(
            name,
            directory,
            f"Frontmatter `name: {declared}` does not match the directory name "
            f"`{name}` — rename one to match the other.",
        )

    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n\n[truncated]"

    return Skill(name=name, description=description, root=directory, body=body)


def _broken(name: str, directory: Path, error: str) -> Skill:
    """A listed-but-unusable skill carrying *error* for the Settings panel."""
    return Skill(name=name, description="", root=directory, body="", error=error)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse a skill's YAML frontmatter into flat ``key -> scalar`` pairs.

    Handles the shapes real skills use and nothing more: ``key: value``,
    quoted values, ``key: >`` / ``key: |`` block scalars (continuation lines
    are those indented past the key), and ``key:`` followed by a ``- item``
    block sequence (joined with ``", "``, since every field this package reads
    is a scalar). Nested mappings and unrecognised syntax are skipped rather
    than guessed at — a skill that needs them simply loses that one key, which
    surfaces as a normal "no description" load error instead of an exception.
    """
    fields: dict[str, str] = {}
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        match = _KEY_RE.match(lines[index])
        if match is None:
            index += 1
            continue

        key = match.group("key")
        rest = match.group("rest").strip()
        indent = len(match.group("indent").expandtabs(4))
        index += 1

        if rest in ("|", ">", "|-", ">-", "|+", ">+"):
            block, index = _consume_indented(lines, index, indent)
            # Folded (``>``) joins with spaces, literal (``|``) with newlines;
            # every consumer of these fields renders on one line anyway, so
            # both collapse to spaces here.
            fields[key] = " ".join(part.strip() for part in block if part.strip())
        elif rest:
            fields[key] = _unquote(rest)
        else:
            items, index = _consume_sequence(lines, index)
            if items:
                fields[key] = ", ".join(items)

    return fields


def _consume_indented(lines: list[str], start: int, indent: int) -> tuple[list[str], int]:
    """Collect the block-scalar continuation lines indented past *indent*."""
    collected: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        collected.append(line)
        index += 1
    return collected, index


def _consume_sequence(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect a ``- item`` block sequence following a bare ``key:`` line.

    Stops at the first non-blank line that is not an item, leaving *index* on
    it so the caller's loop sees it as the next key.
    """
    items: list[str] = []
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if not stripped.startswith("- "):
            break
        items.append(_unquote(stripped[2:].strip()))
        index += 1
    return items, index


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes and any trailing ``#`` comment."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value.strip()
