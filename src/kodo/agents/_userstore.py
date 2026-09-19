"""The user-installed agent store — scan ``~/.kodo/agents`` and report what is there.

Layout (doc/USER_AGENTS.md §2)
==============================

::

    ~/.kodo/agents/
      reviewer/                     one directory per user top-level agent
        reviewer.json               how it is selected (a TopAgent config)
        agent_reviewer.md           its prompt — exactly one per directory
      subagents/                    the shared user sub-agent directory
        subagent_auditor.md         a sub-agent's prompt
        auditor.json                that sub-agent's contract (a SubAgentSpec)

The directory name **is** the top-level agent's name, and both of its files are
named after it, so a bundle cannot disagree with itself about what it is called.
User sub-agents live in one shared directory rather than inside a bundle because
they are shared: any user top-level agent may list any of them, and a sub-agent
installed twice from two sources is one file, not two copies that drift.

Two regimes, one parser
=======================

:mod:`kodo.agents` parses first-party files and fails loudly. This module parses
**third-party** files and must always produce *something*: a directory that
fails to load becomes a :class:`BrokenAgent` row carrying the reason, so the
Settings panel can show the user what is wrong and offer to delete it, and every
*other* user agent still loads.

That is the whole difference, and it is deliberately not a second parser. The
strict loaders (:func:`~kodo.agents.load_agent`, :func:`~kodo.agents.load_top_agent`,
:func:`~kodo.agents.subagents.load_spec`) are called unchanged; this module wraps
each call in a ``try`` and turns the exception into a row. One parser, two call
sites, two regimes — the rule :mod:`kodo.skills` established, applied without
forking anything.

Reserved names
==============

Two names a user file may not take, both checked here rather than in the
loaders, because both are facts about *this root* and not about the file format:

- anything starting with :data:`BUILTIN_NAME_PREFIX` — every built-in agent
  carries it, so refusing it on a user file is what makes a collision between
  the two roots impossible rather than merely unlikely;
- :data:`SHARED_SUBAGENTS_DIRNAME` as a top-level agent's name, which would be
  indistinguishable from the shared sub-agent directory that sits beside it.

Nothing here validates *across* agents. Whether a declared ``critic:`` resolves,
whether a ``subagents:`` entry exists, whether a consumed artifact role is
produced by anybody — all of that is cross-agent knowledge that spans both
roots, so it stays in :class:`~kodo.agents.AgentRegistry`, which holds the union.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ._loader import AgentLoadError, SubAgent, load_agent
from ._topagent import TOP_AGENT_SUFFIX, TopAgent, TopAgentLoadError, load_top_agent
from .subagents import SPEC_SUFFIX, SpecLoadError, SubAgentSpec, load_spec

__all__ = [
    "BUILTIN_NAME_PREFIX",
    "SHARED_SUBAGENTS_DIRNAME",
    "BrokenAgent",
    "UserAgentDeleteError",
    "UserAgentStore",
    "UserAgents",
    "reserved_name_error",
]

#: The prefix every **built-in** agent's name carries (``kodo_guide``,
#: ``kodo_coder``, …). Reserved: a user agent or sub-agent whose name starts
#: with it is refused, which is what guarantees the two roots share one flat
#: namespace without ever colliding in it.
BUILTIN_NAME_PREFIX = "kodo_"

#: The one directory under the agents root that is not a top-level agent's
#: bundle. Reserved as an agent name for exactly that reason.
SHARED_SUBAGENTS_DIRNAME = "subagents"


class UserAgentDeleteError(Exception):
    """Raised when a user agent cannot be deleted (unknown, escaping, or I/O)."""


@dataclass(frozen=True)
class BrokenAgent:
    """A user agent that could not be loaded, and why.

    A separate record rather than an ``error`` field on
    :class:`~kodo.agents.SubAgent`, because a ``SubAgent`` has required fields
    (``system_prompt``, ``tools``) that a broken file cannot supply — the same
    reasoning that gave :class:`kodo.skills.Skill` its own shape.

    Attributes:
        name: The name the entry *claims*, derived from its directory or
            filename rather than from the frontmatter that may be the thing
            that failed to parse. Always non-empty, so the row can be shown and
            deleted by name.
        path: The directory (for a top-level agent) or file (for a sub-agent)
            the user would delete to make the row go away.
        error: What is wrong, in one sentence, written for the user who wrote
            the file — not a traceback.
        is_top_level: Whether the entry was found as a top-level agent bundle
            rather than in the shared ``subagents/`` directory. Decides which
            list a UI shows it in.
    """

    name: str
    path: Path
    error: str
    is_top_level: bool


@dataclass(frozen=True)
class UserAgents:
    """Everything one scan of the user root found.

    The three loaded tuples are deliberately parallel to what the packaged root
    yields, so :class:`~kodo.agents.AgentRegistry` can concatenate rather than
    special-case: prompts are prompts, configs are configs, specs are specs, and
    only :attr:`broken` has no packaged counterpart.

    Attributes:
        agents: Every user prompt that parsed — top-level agents and sub-agents
            together, exactly as the packaged glob returns them.
        configs: The ``<agent_name>.json`` config of every user top-level agent
            that parsed.
        specs: The ``<name>.json`` contract of every user sub-agent that parsed.
        broken: One row per entry that did not load, with the reason.
    """

    agents: tuple[SubAgent, ...] = ()
    configs: tuple[TopAgent, ...] = ()
    specs: tuple[SubAgentSpec, ...] = ()
    broken: tuple[BrokenAgent, ...] = ()


def reserved_name_error(name: str) -> str:
    """Why *name* may not be used by a user agent, or ``""`` when it may.

    Args:
        name: The proposed agent or sub-agent name.

    Returns:
        str: A one-sentence reason, or ``""`` when the name is acceptable.
    """
    if not name:
        return "the name is empty"
    if name.startswith(BUILTIN_NAME_PREFIX):
        return (
            f"{name!r} starts with the reserved prefix {BUILTIN_NAME_PREFIX!r}, "
            f"which every built-in Kōdo agent carries — choose a name without it"
        )
    if name == SHARED_SUBAGENTS_DIRNAME:
        return (
            f"{name!r} is reserved: it is the shared user sub-agent directory "
            f"that sits beside the top-level agent bundles"
        )
    return ""


class UserAgentStore:
    """Read and delete the agents installed under one user root.

    Stateless between calls, like :class:`kodo.skills.SkillStore`: every
    :meth:`scan` re-reads the directory, so an agent installed while the server
    is running is found by the next scan with no cache to invalidate. The
    *registry* caches the result of a scan — because its validation is
    cross-agent and cannot be redone per lookup — and reloads explicitly.

    Args:
        root: The user agents root (``~/.kodo/agents``). Need not exist — a
            missing root simply means no user agents are installed.
    """

    __root: Path

    def __init__(self, root: Path) -> None:
        self.__root = root

    @property
    def root(self) -> Path:
        """The user agents root this store scans."""
        return self.__root

    @property
    def subagents_dir(self) -> Path:
        """The shared user sub-agent directory under this root."""
        return self.__root / SHARED_SUBAGENTS_DIRNAME

    def ensure_root(self) -> Path:
        """Create the root (and its ``subagents/``) if absent, and return it.

        Called on server startup so the directory the user is told to install
        agents into actually exists, and so a Settings-panel "open the agents
        folder" action always has something to open. Never raises for a root
        that already exists.
        """
        self.subagents_dir.mkdir(parents=True, exist_ok=True)
        return self.__root

    def scan(self) -> UserAgents:
        """Load every user agent and sub-agent under the root.

        Never raises: a malformed entry becomes a :class:`BrokenAgent` row and
        the scan continues, so one bad third-party file cannot take the rest of
        the user's agents (or the server) down with it.

        Returns:
            UserAgents: The loaded prompts, configs and specs, plus one row per
            entry that failed.
        """
        if not self.__root.is_dir():
            return UserAgents()
        agents: list[SubAgent] = []
        configs: list[TopAgent] = []
        specs: list[SubAgentSpec] = []
        broken: list[BrokenAgent] = []
        try:
            entries = sorted(self.__root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            return UserAgents(broken=(BrokenAgent(self.__root.name, self.__root, str(exc), True),))
        for entry in entries:
            if not entry.is_dir():
                # A stray README.md or .DS_Store beside the bundles is not an
                # agent and is not an error either — only directories are read.
                continue
            if entry.name == SHARED_SUBAGENTS_DIRNAME:
                self.__scan_subagents(entry, agents, specs, broken)
                continue
            self.__scan_bundle(entry, agents, configs, broken)
        return UserAgents(tuple(agents), tuple(configs), tuple(specs), tuple(broken))

    def delete(self, name: str, *, top_level: bool) -> None:
        """Remove one installed user agent or sub-agent from the root.

        Args:
            name: The agent's name, as :class:`BrokenAgent` or the registry
                reports it.
            top_level: ``True`` to delete a top-level agent's whole bundle
                directory, ``False`` to delete a sub-agent's prompt and contract
                from the shared directory.

        Raises:
            UserAgentDeleteError: The name is empty, escapes the root, names
                nothing installed, or could not be removed.
        """
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise UserAgentDeleteError(f"{name!r} is not a valid agent name")
        try:
            if top_level:
                target = (self.__root / name).resolve()
                target.relative_to(self.__root.resolve())
                if not target.is_dir():
                    raise UserAgentDeleteError(f"no user agent named {name!r} is installed")
                shutil.rmtree(target)
                return
            removed = False
            for path in self.__subagent_files(name):
                if path.exists():
                    path.unlink()
                    removed = True
            if not removed:
                raise UserAgentDeleteError(f"no user sub-agent named {name!r} is installed")
        except UserAgentDeleteError:
            raise
        except (OSError, ValueError) as exc:
            raise UserAgentDeleteError(f"could not delete {name!r}: {exc}") from exc

    def __subagent_files(self, name: str) -> tuple[Path, ...]:
        """The two files that make up one user sub-agent."""
        return (
            self.subagents_dir / f"subagent_{name}.md",
            self.subagents_dir / f"{name}{SPEC_SUFFIX}",
        )

    def __scan_bundle(
        self,
        directory: Path,
        agents: list[SubAgent],
        configs: list[TopAgent],
        broken: list[BrokenAgent],
    ) -> None:
        """Load one top-level agent bundle, recording a row on any failure."""
        name = directory.name
        reserved = reserved_name_error(name)
        if reserved:
            broken.append(BrokenAgent(name, directory, reserved, True))
            return
        prompts = sorted(directory.glob("agent_*.md"))
        if not prompts:
            broken.append(
                BrokenAgent(
                    name,
                    directory,
                    f"no agent_{name}.md — a top-level agent's prompt is named "
                    f"after the directory it lives in",
                    True,
                )
            )
            return
        if len(prompts) > 1:
            # Point 5 of the layout contract: one prompt per bundle. Two would
            # make "which agent is this directory?" a question the name alone
            # could no longer answer.
            found = ", ".join(sorted(p.name for p in prompts))
            broken.append(
                BrokenAgent(
                    name,
                    directory,
                    f"{len(prompts)} agent prompts in one directory ({found}) — a "
                    f"bundle holds exactly one, named agent_{name}.md",
                    True,
                )
            )
            return
        try:
            agent = load_agent(prompts[0])
        except AgentLoadError as exc:
            broken.append(BrokenAgent(name, directory, _reason(exc, prompts[0]), True))
            return
        if agent.name != name:
            broken.append(
                BrokenAgent(
                    name,
                    directory,
                    f"declares name {agent.name!r} but lives in a directory named "
                    f"{name!r} — the two must agree",
                    True,
                )
            )
            return
        config_path = directory / f"{name}{TOP_AGENT_SUFFIX}"
        if not config_path.is_file():
            broken.append(
                BrokenAgent(
                    name,
                    directory,
                    f"no {name}{TOP_AGENT_SUFFIX} — a top-level agent declares how it "
                    f"is selected (label, description, rank) beside its prompt",
                    True,
                )
            )
            return
        try:
            configs.append(load_top_agent(config_path))
        except TopAgentLoadError as exc:
            broken.append(BrokenAgent(name, directory, _reason(exc, config_path), True))
            return
        agents.append(agent)

    def __scan_subagents(
        self,
        directory: Path,
        agents: list[SubAgent],
        specs: list[SubAgentSpec],
        broken: list[BrokenAgent],
    ) -> None:
        """Load every sub-agent in the shared directory, one row per failure."""
        try:
            prompts = sorted(directory.glob("subagent_*.md"))
        except OSError as exc:
            broken.append(BrokenAgent(directory.name, directory, str(exc), False))
            return
        for prompt in prompts:
            name = prompt.stem[len("subagent_") :]
            reserved = reserved_name_error(name)
            if reserved:
                broken.append(BrokenAgent(name, prompt, reserved, False))
                continue
            try:
                agent = load_agent(prompt)
            except AgentLoadError as exc:
                broken.append(BrokenAgent(name, prompt, _reason(exc, prompt), False))
                continue
            spec_path = directory / f"{name}{SPEC_SUFFIX}"
            if not spec_path.is_file():
                broken.append(
                    BrokenAgent(
                        name,
                        prompt,
                        f"no {name}{SPEC_SUFFIX} beside it — a sub-agent declares "
                        f"its input/output contract in a JSON file of the same name",
                        False,
                    )
                )
                continue
            try:
                specs.append(load_spec(spec_path))
            except SpecLoadError as exc:
                broken.append(BrokenAgent(name, spec_path, _reason(exc, spec_path), False))
                continue
            agents.append(agent)


def _reason(exc: Exception, path: Path) -> str:
    """Strip the path prefix the strict loaders put on every message.

    The row already carries the path, and repeating an absolute path inside the
    sentence is noise in a UI that is showing the file name next to it.
    """
    return str(exc).replace(f"{path}: ", "")
