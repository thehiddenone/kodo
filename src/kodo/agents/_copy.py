"""Copying a built-in agent, with its whole sub-agent pipeline, into the user root.

doc/AGENT_BUILDER.md §3. A copy is the starting point for editing a built-in
without changing Kōdo's code: the files land in ``~/.kodo/agents`` as an
ordinary user agent, under new names, and from then on they are the user's.

What gets copied
================

Copying agent ``A`` copies its **closure**: ``A``, every agent named in its
``subagents:``, every agent named in a member's ``critic:``, repeated until
nothing new is added (D1). Only built-in (``kodo_*``) names are followed — the
source is always a built-in (D28), and a built-in never names a user agent.

The engine-driven sub-agents (:data:`~kodo.agents.ENGINE_DRIVEN_AGENTS`) appear
in no ``subagents:`` or ``critic:``, so the closure excludes them as a
consequence of how it is defined — which is right, because the engine calls
them by their built-in name and a renamed copy would never run. For the same
reason one of them cannot be the source of a copy, and neither can a top-level
agent with ``interactive: false``, which a user agent may not declare.

Renaming
========

The user gives one prefix ``P`` that replaces ``kodo_`` on every member (D2):
``kodo_guide`` → ``acme_guide``, ``kodo_coder`` → ``acme_coder``. The new name
is written into the directory and file names, ``name:``, every ``subagents:``
entry and ``critic:``, the config's or spec's ``"name"``, and every whole-word
``kodo_<n>`` / ``run_subagent_kodo_<n>`` token in the prompt body — for closure
members only, so a prose mention of an agent outside the closure is left as
written. Token replacement is whole-word, never substring: the rule for
``kodo_coder`` cannot touch ``kodo_test_coder``.

``label`` and ``display_name`` get the prefix as a suffix, minus any trailing
``_`` (D21): with ``acme_``, "Guide" becomes "Guide (acme)". ``"default": true``
is dropped, because a user agent may not claim it. ``{SHARED:…}``, ``{SKILLS}``
and ``{PHASE:…}`` are copied verbatim (D3), so a copy keeps picking up Kōdo's
fixes to the shared blocks. Nothing records where a copy came from (D4).

Two steps, and all-or-nothing
=============================

:func:`plan_copy` reports what a copy *would* write and which members already
exist under their new names; :func:`copy_agent` writes it. They are separate
for the same reason as :func:`~kodo.agents.scan_source` and
:func:`~kodo.agents.install_source`: the user answers **keep** or **replace**
once, for every conflict, between the two (§3.4).

Writing is all-or-nothing. Every file is rendered into a temporary directory
beside the user root and checked with the strict loaders first; only then are
the members moved into place, and a failure part way through moves back what
had already been moved. Reloading the registry afterwards is the caller's job,
as it is after an install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ._builtin import ENGINE_DRIVEN_AGENTS, PACKAGED_AGENTS_DIR
from ._install import KIND_AGENT, KIND_SUBAGENT, UNVERSIONED, Candidate, SourceScan
from ._loader import AgentLoadError, SubAgent, load_agent, split_frontmatter
from ._registry import SUBAGENTS_SUBDIR
from ._topagent import (
    TOP_AGENT_SUFFIX,
    TopAgent,
    TopAgentLoadError,
    load_top_agent,
    load_top_agents,
)
from ._userstore import BUILTIN_NAME_PREFIX, UserAgentStore, reserved_name_error
from .subagents import SPEC_SUFFIX, SpecLoadError, load_spec

__all__ = [
    "AgentCopyError",
    "CopyMember",
    "CopyPlan",
    "CopyResult",
    "CopySource",
    "copy_agent",
    "copy_sources",
    "plan_copy",
]

# What a copied name may look like: the shape of every built-in name. Lowercase
# so a name is the same on case-insensitive filesystems, and an identifier so
# it can be part of the generated ``run_subagent_<name>`` tool name.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The ``run_subagent_<name>`` tool name must fit the 64-character limit LLM
# APIs put on a tool name.
_RUN_SUBAGENT_PREFIX = "run_subagent_"
_TOOL_NAME_LIMIT = 64

# A whole-word built-in agent token, optionally as its generated tool name. The
# look-behind is what keeps ``subagent_kodo_coder.md`` (a file name) and
# ``my_kodo_coder`` untouched, and ``\w+`` being greedy is what makes the match
# the *whole* word, so ``kodo_test_coder`` is one token, never ``kodo_test``.
_TOKEN_RE = re.compile(r"(?<!\w)((?:run_subagent_)?)(kodo_\w+)", re.ASCII)

_DISPLAY_NAME_RE = re.compile(r"(?m)^display_name:[ \t]*(.*?)[ \t]*$")
_NAME_LINE_RE = re.compile(r"(?m)^name:.*$")

# Where the specs sit, relative to the packaged agents directory.
_SPECS_SUBDIR = "specs"


class AgentCopyError(Exception):
    """Raised when a copy is refused, or its files cannot be written."""


@dataclass(frozen=True)
class CopySource:
    """One built-in agent that can be the source of a copy.

    Attributes:
        name: The built-in's name (``kodo_guide``).
        kind: :data:`~kodo.agents.KIND_AGENT` or :data:`~kodo.agents.KIND_SUBAGENT`.
        label: What to call it in a picker: the config's ``label`` for a
            top-level agent, the ``display_name`` otherwise.
    """

    name: str
    kind: str
    label: str


@dataclass(frozen=True)
class CopyMember:
    """One closure member, and what its copy would replace.

    Attributes:
        source: The built-in being copied (``kodo_coder``).
        name: The name of its copy (``acme_coder``).
        kind: :data:`~kodo.agents.KIND_AGENT` or :data:`~kodo.agents.KIND_SUBAGENT`.
        installed_version: The version of what already sits at the copy's
            destination — :data:`~kodo.agents.UNVERSIONED` when it declares
            none or does not load — or ``""`` when the destination is free.
    """

    source: str
    name: str
    kind: str
    installed_version: str = ""

    @property
    def conflicts(self) -> bool:
        """Whether copying this member would replace something already there."""
        return bool(self.installed_version)


@dataclass(frozen=True)
class CopyPlan:
    """What copying one built-in would write, without having written it.

    Attributes:
        source: The built-in the user asked to copy.
        prefix: The prefix that replaces ``kodo_``.
        members: The closure, the source first, then in the order its
            ``subagents:`` and ``critic:`` reach each member.
    """

    source: str
    prefix: str
    members: tuple[CopyMember, ...]

    @property
    def conflicting(self) -> tuple[CopyMember, ...]:
        """The members whose destination is already taken."""
        return tuple(m for m in self.members if m.conflicts)

    def conflict_report(self) -> str:
        """The existing-vs-incoming list the user decides on, as an install shows it.

        Rendered by :meth:`kodo.agents.SourceScan.conflict_report`, so the CLI
        and the Settings panel ask keep-or-replace about a copy in the same
        words as about an install. A copy declares no version, so the incoming
        side always reads :data:`~kodo.agents.UNVERSIONED`.

        Returns:
            str: The report, or ``""`` when nothing conflicts.
        """
        candidates = tuple(
            Candidate(m.name, m.kind, UNVERSIONED, m.installed_version) for m in self.members
        )
        return SourceScan(self.source, candidates).conflict_report()


@dataclass(frozen=True)
class CopyResult:
    """What a copy actually wrote.

    Attributes:
        copied: Names of the copies written, in plan order.
        kept: Names left alone because something already sat at their
            destination and the caller chose to keep it.
    """

    copied: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()


def copy_sources(builtin_dir: Path = PACKAGED_AGENTS_DIR) -> tuple[CopySource, ...]:
    """Every built-in agent that can be copied, top-level agents first.

    Leaves out exactly what :func:`plan_copy` refuses as a source: the
    engine-driven sub-agents and any top-level agent with
    ``interactive: false``.

    Args:
        builtin_dir: The packaged agents directory. Injectable for tests.

    Returns:
        tuple[CopySource, ...]: Top-level agents, then sub-agents, each group
        name-sorted.

    Raises:
        AgentCopyError: A packaged agent file does not load.
    """
    builtins = _Builtins(builtin_dir)
    rows = [
        CopySource(agent.name, builtins.kind(agent), builtins.label(agent))
        for agent in builtins.agents()
        if not builtins.refusal(agent)
    ]
    return tuple(sorted(rows, key=lambda s: (s.kind != KIND_AGENT, s.name)))


def plan_copy(
    name: str, prefix: str, root: Path, *, builtin_dir: Path = PACKAGED_AGENTS_DIR
) -> CopyPlan:
    """Work out what copying built-in *name* under *prefix* would write.

    Args:
        name: The built-in agent to copy (``kodo_guide``).
        prefix: The prefix that replaces ``kodo_`` on every closure member
            (``acme_``).
        root: The user agents root the copy would be written to, read to find
            which destinations are already taken.
        builtin_dir: The packaged agents directory. Injectable for tests.

    Returns:
        CopyPlan: The closure, with the new names and the conflicts.

    Raises:
        AgentCopyError: *name* is not a built-in that can be copied, or
            *prefix* does not produce a valid user agent name for every member.
    """
    builtins = _Builtins(builtin_dir)
    source = builtins.find(name)
    refusal = builtins.refusal(source)
    if refusal:
        raise AgentCopyError(refusal)
    _check_prefix(prefix)
    closure = builtins.closure(source)
    for member in closure:
        _check_name(_renamed(member.name, prefix))
    installed = _installed(root)
    members = tuple(
        CopyMember(
            source=member.name,
            name=_renamed(member.name, prefix),
            kind=builtins.kind(member),
            installed_version=installed.get(_destination(root, member, prefix), ""),
        )
        for member in closure
    )
    return CopyPlan(source=name, prefix=prefix, members=members)


def copy_agent(
    name: str,
    prefix: str,
    root: Path,
    *,
    replace: bool,
    builtin_dir: Path = PACKAGED_AGENTS_DIR,
) -> CopyResult:
    """Copy built-in *name*, with its closure, into *root* under *prefix*.

    Re-plans rather than trusting an earlier :func:`plan_copy`, so a
    destination taken in between is handled by *replace* like any other.

    Args:
        name: The built-in agent to copy.
        prefix: The prefix that replaces ``kodo_``.
        root: The user agents root. Created if absent.
        replace: What to do about a member whose destination is already taken.
            ``True`` replaces it; ``False`` keeps what is there and reports it
            in :attr:`CopyResult.kept`. The user answers this after seeing
            :meth:`CopyPlan.conflict_report`.
        builtin_dir: The packaged agents directory. Injectable for tests.

    Returns:
        CopyResult: What was copied and what was kept.

    Raises:
        AgentCopyError: The copy is refused (see :func:`plan_copy`), a rendered
            file fails its strict loader, or the files cannot be written. In
            the last two cases nothing under *root* has changed.
    """
    plan = plan_copy(name, prefix, root, builtin_dir=builtin_dir)
    builtins = _Builtins(builtin_dir)
    wanted = [m for m in plan.members if replace or not m.conflicts]
    kept = tuple(m.name for m in plan.members if m.conflicts and not replace)
    if not wanted:
        return CopyResult(kept=kept)
    store = UserAgentStore(root)
    try:
        store.ensure_root()
        staging = Path(tempfile.mkdtemp(prefix=".agents-copy-", dir=root.parent))
    except OSError as exc:
        raise AgentCopyError(f"could not prepare the copy: {exc}") from exc
    try:
        mapping = {m.source: m.name for m in plan.members}
        suffix = prefix.rstrip("_")
        staged = [
            (member, _stage(builtins, member, mapping, suffix, staging / "new"))
            for member in wanted
        ]
        _move_into_place(staged, store, staging / "old")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return CopyResult(copied=tuple(m.name for m in wanted), kept=kept)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Builtins:
    """The packaged agents, parsed once for one plan or copy.

    Args:
        builtin_dir: The packaged agents directory.

    Raises:
        AgentCopyError: A packaged prompt or config does not load.
    """

    __dir: Path
    __agents: dict[str, SubAgent]
    __configs: dict[str, TopAgent]

    def __init__(self, builtin_dir: Path) -> None:
        self.__dir = builtin_dir
        paths = sorted((builtin_dir / SUBAGENTS_SUBDIR).glob("subagent_*.md")) + sorted(
            builtin_dir.glob("agent_*.md")
        )
        try:
            self.__agents = {agent.name: agent for agent in map(load_agent, paths)}
            self.__configs = {cfg.name: cfg for cfg in load_top_agents(builtin_dir)}
        except (AgentLoadError, TopAgentLoadError) as exc:
            raise AgentCopyError(f"a built-in agent does not load: {exc}") from exc

    def agents(self) -> list[SubAgent]:
        """Every packaged agent, top-level and sub-agent alike."""
        return list(self.__agents.values())

    def find(self, name: str) -> SubAgent:
        """The packaged agent called *name*.

        Raises:
            AgentCopyError: No built-in agent has that name.
        """
        agent = self.__agents.get(name)
        if agent is None:
            raise AgentCopyError(
                f"no built-in agent named {name!r} — only Kōdo's own agents (named "
                f"{BUILTIN_NAME_PREFIX}…) can be copied"
            )
        return agent

    def kind(self, agent: SubAgent) -> str:
        """``KIND_AGENT`` for a top-level agent, ``KIND_SUBAGENT`` otherwise."""
        return KIND_AGENT if agent.is_top_level else KIND_SUBAGENT

    def label(self, agent: SubAgent) -> str:
        """The config's ``label`` for a top-level agent, else the display name."""
        config = self.__configs.get(agent.name)
        return (config.label if config is not None else "") or agent.display_name

    def config_path(self, agent: SubAgent) -> Path:
        """Where a top-level agent's ``<name>.json`` sits."""
        return self.__dir / f"{agent.name}{TOP_AGENT_SUFFIX}"

    def spec_path(self, agent: SubAgent) -> Path:
        """Where a sub-agent's spec JSON sits."""
        return self.__dir / SUBAGENTS_SUBDIR / _SPECS_SUBDIR / f"{agent.name}{SPEC_SUFFIX}"

    def refusal(self, agent: SubAgent) -> str:
        """Why *agent* cannot be the source of a copy, or ``""`` when it can."""
        if agent.name in ENGINE_DRIVEN_AGENTS:
            return (
                f"{agent.name!r} is called by Kōdo's engine by its built-in name, so a "
                f"renamed copy would never run"
            )
        config = self.__configs.get(agent.name)
        if config is not None and not config.interactive:
            return (
                f'{agent.name!r} runs only through agent.run ("interactive": false), '
                f"which a user agent may not declare"
            )
        return ""

    def closure(self, source: SubAgent) -> list[SubAgent]:
        """*source* and every built-in its ``subagents:``/``critic:`` reach."""
        order = [source]
        seen = {source.name}
        index = 0
        while index < len(order):
            agent = order[index]
            index += 1
            for name in (*agent.subagent_order, agent.critic):
                if not name.startswith(BUILTIN_NAME_PREFIX) or name in seen:
                    continue
                seen.add(name)
                order.append(self.find(name))
        return order


def _renamed(name: str, prefix: str) -> str:
    """``kodo_<n>`` → ``<prefix><n>``."""
    return prefix + name[len(BUILTIN_NAME_PREFIX) :]


def _check_prefix(prefix: str) -> None:
    """Refuse a prefix that cannot start a user agent name."""
    if not prefix:
        raise AgentCopyError("the prefix is empty — give one such as 'acme_'")
    if prefix.startswith(BUILTIN_NAME_PREFIX):
        raise AgentCopyError(
            f"the prefix {prefix!r} starts with {BUILTIN_NAME_PREFIX!r}, which is reserved "
            f"for Kōdo's built-in agents"
        )
    if not _NAME_RE.match(prefix):
        raise AgentCopyError(
            f"the prefix {prefix!r} must start with a lowercase letter and hold only "
            f"lowercase letters, digits and underscores"
        )


def _check_name(name: str) -> None:
    """Refuse a copied name the user store or a tool name would not accept."""
    reserved = reserved_name_error(name)
    if reserved:
        raise AgentCopyError(reserved)
    if not _NAME_RE.match(name):
        raise AgentCopyError(f"{name!r} is not a valid agent name")
    if len(_RUN_SUBAGENT_PREFIX + name) > _TOOL_NAME_LIMIT:
        raise AgentCopyError(
            f"{name!r} is too long: its run_subagent_{name} tool name would exceed "
            f"{_TOOL_NAME_LIMIT} characters — choose a shorter prefix"
        )


def _destination(root: Path, agent: SubAgent, prefix: str) -> Path:
    """The directory a member's copy is written to."""
    store = UserAgentStore(root)
    parent = store.root if agent.is_top_level else store.subagents_dir
    return parent / _renamed(agent.name, prefix)


def _installed(root: Path) -> dict[Path, str]:
    """``{directory: version}`` for every entry directory under *root*.

    Keyed by directory rather than by loaded name, so a destination holding a
    bundle that does not load still counts as taken — writing into it would
    otherwise mix the copy's files with whatever is already there.
    """
    store = UserAgentStore(root)
    loaded = {agent.source_path.parent: agent.version for agent in store.scan().agents}
    taken: dict[Path, str] = {}
    for parent in (store.root, store.subagents_dir):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if entry.is_dir() and entry != store.subagents_dir:
                taken[entry] = loaded.get(entry, "") or UNVERSIONED
    return taken


def _rewrite_tokens(text: str, mapping: dict[str, str]) -> str:
    """Rename every whole-word closure-member token in *text*."""

    def _swap(match: re.Match[str]) -> str:
        new = mapping.get(match.group(2))
        return match.group(0) if new is None else match.group(1) + new

    return _TOKEN_RE.sub(_swap, text)


def _render_prompt(agent: SubAgent, mapping: dict[str, str], suffix: str) -> str:
    """The copy's prompt file: tokens renamed, ``display_name`` suffixed."""
    text = agent.source_path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(_rewrite_tokens(text, mapping))
    display = f"display_name: {agent.display_name} ({suffix})"
    if _DISPLAY_NAME_RE.search(frontmatter):
        frontmatter = _DISPLAY_NAME_RE.sub(lambda _: display, frontmatter, count=1)
    else:
        # Written explicitly: left absent, it would default to the title-cased
        # new name ("Acme Guide") and break the "Guide (acme)" convention.
        frontmatter = _NAME_LINE_RE.sub(lambda m: f"{m.group(0)}\n{display}", frontmatter, count=1)
    return frontmatter + body


def _render_config(path: Path, name: str, suffix: str) -> str:
    """The copy's ``<name>.json``: renamed, suffixed, never the default."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["name"] = name
    raw.pop("default", None)
    if raw.get("label"):
        raw["label"] = f"{raw['label']} ({suffix})"
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _render_spec(path: Path, name: str) -> str:
    """The copy's spec JSON: only ``"name"`` changes."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["name"] = name
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def _stage(
    builtins: _Builtins,
    member: CopyMember,
    mapping: dict[str, str],
    suffix: str,
    into: Path,
) -> Path:
    """Render one member's two files into a staging directory, and check them.

    Returns:
        Path: The staged directory, named after the copy.
    """
    agent = builtins.find(member.source)
    directory = into / member.kind / member.name
    try:
        directory.mkdir(parents=True)
        if agent.is_top_level:
            prompt = directory / f"agent_{member.name}.md"
            prompt.write_text(_render_prompt(agent, mapping, suffix), encoding="utf-8")
            config = directory / f"{member.name}{TOP_AGENT_SUFFIX}"
            config.write_text(
                _render_config(builtins.config_path(agent), member.name, suffix), encoding="utf-8"
            )
            load_agent(prompt)
            load_top_agent(config)
        else:
            prompt = directory / f"subagent_{member.name}.md"
            prompt.write_text(_render_prompt(agent, mapping, suffix), encoding="utf-8")
            spec = directory / f"{member.name}{SPEC_SUFFIX}"
            spec.write_text(_render_spec(builtins.spec_path(agent), member.name), encoding="utf-8")
            load_agent(prompt)
            load_spec(spec)
    except OSError as exc:
        raise AgentCopyError(f"could not write the copy of {member.source!r}: {exc}") from exc
    except (AgentLoadError, TopAgentLoadError, SpecLoadError) as exc:
        raise AgentCopyError(f"the copy of {member.source!r} does not load: {exc}") from exc
    return directory


def _move_into_place(
    staged: list[tuple[CopyMember, Path]], store: UserAgentStore, backups: Path
) -> None:
    """Move every staged member to its destination, or none of them.

    A destination that is already taken is moved aside into *backups* first;
    if any move fails, every member already moved is removed and its backup
    restored, so the user root ends exactly as it started.
    """
    done: list[tuple[Path, Path | None]] = []
    try:
        for member, directory in staged:
            parent = store.root if member.kind == KIND_AGENT else store.subagents_dir
            destination = parent / member.name
            backup: Path | None = None
            if destination.exists():
                backup = backups / member.kind / member.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
            done.append((destination, backup))
            os.replace(directory, destination)
    except OSError as exc:
        for destination, backup in reversed(done):
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        raise AgentCopyError(f"could not copy into {store.root}: {exc}") from exc
