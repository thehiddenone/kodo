"""Installing user agents from a local directory or a git repository.

A **source** — a directory on disk, or a repository URL — is laid out the way
one bundle is authored (doc/USER_AGENTS.md §3)::

    <source>/
      reviewer.json            the top-level agent's config
      agent_reviewer.md        its prompt
      subagents/
        auditor/
          subagent_auditor.md  a sub-agent's prompt
          auditor.json         that sub-agent's contract

Installing moves each half to where the registry reads it: the top-level
agent's two files into ``~/.kodo/agents/<name>/``, and each
``subagents/<name>/`` directory into the **shared**
``~/.kodo/agents/subagents/<name>/``. That
redistribution is the whole reason this module exists rather than telling
people to copy a directory — the source is shaped for authoring one bundle, the
destination is shaped for a registry that shares sub-agents across every
installed agent.

A source may carry a top-level agent, sub-agents, or both. Sub-agents alone is a
legitimate bundle: it publishes specialists for agents that are already
installed.

Two steps, never one
====================

:func:`scan_source` reports what a source *would* install — each entry's name,
kind and version, and the version of whatever is already installed under that
name. :func:`install_source` then performs it. They are separate because of the
conflict rule (doc/USER_AGENTS.md §4): when a source carries something already
installed, the user is shown both versions and decides, so a decision has to
happen *between* reading the source and writing to disk.

For a repository that means cloning twice — once to scan, once to install —
matching :mod:`kodo.skills`'s convention and for the same reason: nothing is
cached between the two calls, so no temporary directory has to stay alive across
a round trip to the user. A source that changes underneath the two calls is
covered by :attr:`InstallResult.missing`.

Both entry points are synchronous. They block on the network and the disk, so a
server call site wraps them in :func:`asyncio.to_thread`, exactly as the skills
installer and the local-model installer already do.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kodo.skills import GitNotAvailableError

from ._loader import AgentLoadError, load_agent
from ._topagent import TOP_AGENT_SUFFIX, TopAgentLoadError, load_top_agent
from ._userstore import SHARED_SUBAGENTS_DIRNAME, UserAgentStore, reserved_name_error
from .subagents import SPEC_SUFFIX, SpecLoadError, load_spec

# ``GitNotAvailableError`` is :mod:`kodo.skills`'s, re-exported rather than
# redefined: "the git CLI is missing" is one condition, and a caller installing
# both skills and agents should not have to catch two identical exceptions that
# happen to live in different packages.
__all__ = [
    "KIND_AGENT",
    "KIND_SUBAGENT",
    "UNVERSIONED",
    "AgentInstallError",
    "Candidate",
    "GitNotAvailableError",
    "InstallResult",
    "SourceScan",
    "install_source",
    "require_git",
    "scan_source",
]

#: What a candidate is. A top-level agent the user selects and talks to…
KIND_AGENT = "agent"
#: …or a sub-agent another agent delegates to.
KIND_SUBAGENT = "subagent"

#: Shown in a conflict list where a bundle declared no ``version:``. A missing
#: version never blocks an install — it is a reason for the user to look at what
#: they are replacing, not a reason to refuse it.
UNVERSIONED = "(unversioned)"

# `git clone` runs against a URL the user typed; an unreachable host must not
# hang the calling thread (or, on a server, the asyncio.to_thread worker).
_CLONE_TIMEOUT_SECONDS = 60

# A source that looks like something to clone rather than a path to read.
_REMOTE_RE = re.compile(r"^(https?://|git@|ssh://|git://|file://)")


class AgentInstallError(Exception):
    """Raised when a source cannot be read, cloned, or installed from."""


@dataclass(frozen=True)
class Candidate:
    """One agent or sub-agent a source offers, and what it would replace.

    Attributes:
        name: The entry's name — the ``agent_<name>.md`` stem for a top-level
            agent, the ``subagents/<name>/`` directory name for a sub-agent.
        kind: :data:`KIND_AGENT` or :data:`KIND_SUBAGENT`.
        version: The ``version:`` its prompt declares, or :data:`UNVERSIONED`.
        installed_version: The version of what is **already installed** under
            this name, or ``""`` when nothing is. A non-empty value is what
            makes this entry a conflict the user must decide about.
        error: Why this entry cannot be installed, or ``""`` when it can. A
            source is reported whole — a bad entry is listed with its reason
            rather than silently dropped, so the user can see why the bundle
            they downloaded is short one agent.
        files: The source files that would be copied. Empty when :attr:`error`
            is set.
    """

    name: str
    kind: str
    version: str
    installed_version: str = ""
    error: str = ""
    files: tuple[Path, ...] = ()

    @property
    def installable(self) -> bool:
        """Whether this entry could be installed as it stands."""
        return not self.error

    @property
    def conflicts(self) -> bool:
        """Whether installing this would replace something already installed."""
        return bool(self.installed_version) and self.installable


@dataclass(frozen=True)
class SourceScan:
    """What one source offers, without having installed any of it.

    Attributes:
        source: The directory or URL that was read, as the user gave it.
        candidates: Every entry found, installable or not, agents before
            sub-agents and name-sorted within each kind.
    """

    source: str
    candidates: tuple[Candidate, ...] = ()

    @property
    def installable(self) -> tuple[Candidate, ...]:
        """The subset that could be installed."""
        return tuple(c for c in self.candidates if c.installable)

    @property
    def conflicting(self) -> tuple[Candidate, ...]:
        """The subset that would replace something already installed."""
        return tuple(c for c in self.candidates if c.conflicts)

    def conflict_report(self) -> str:
        """The multi-line existing-vs-incoming list the user decides on.

        One line per conflict, naming the kind, the name, and both versions —
        the question "keep what I have or take what this source brings?" cannot
        be answered without seeing all of them at once, so it is rendered here
        rather than assembled by each caller.

        Returns:
            str: The report, or ``""`` when nothing conflicts.
        """
        rows = self.conflicting
        if not rows:
            return ""
        width = max(len(f"{c.kind} {c.name}") for c in rows)
        return "\n".join(
            f"  {c.kind} {c.name}".ljust(width + 2)
            + f"   installed: {c.installed_version}   incoming: {c.version}"
            for c in rows
        )


@dataclass(frozen=True)
class InstallResult:
    """What an install actually did.

    Attributes:
        installed: Names of entries written to the user root.
        kept: Names of entries left alone because something was already
            installed under that name and the caller chose to keep it.
        skipped: Names of entries that could not be installed, each with its
            reason, as ``"<name>: <why>"``.
        missing: Names the caller asked for that the source no longer offers —
            the source changed between the scan and the install.
    """

    installed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def require_git() -> None:
    """Check that the ``git`` CLI is available.

    Raises:
        GitNotAvailableError: ``git`` is not on ``PATH``.
    """
    if shutil.which("git") is None:
        raise GitNotAvailableError(
            "the 'git' command is not available — install git, or install the "
            "agent from a local directory instead"
        )


def scan_source(source: str, root: Path) -> SourceScan:
    """Report what *source* offers, without installing anything.

    Args:
        source: A local directory path, or a git repository URL.
        root: The user agents root the result is compared against, so each
            candidate carries the version of whatever it would replace.

    Returns:
        SourceScan: Every entry found, with its reason when it cannot be
        installed and the installed version when it would replace something.

    Raises:
        AgentInstallError: The source is not a readable directory, or the clone
            failed.
        GitNotAvailableError: The source is a URL and ``git`` is not available.
    """
    installed = _installed_versions(root)
    if _is_remote(source):
        require_git()
        with tempfile.TemporaryDirectory(prefix="kodo-agents-") as tmp:
            return SourceScan(source, _read_source(_clone(source, Path(tmp)), installed))
    return SourceScan(source, _read_source(_local_dir(source), installed))


def install_source(
    source: str,
    root: Path,
    *,
    replace: bool,
    names: tuple[str, ...] | None = None,
) -> InstallResult:
    """Install from *source* into *root*.

    Re-reads the source rather than trusting a previous :func:`scan_source`,
    which is what makes :attr:`InstallResult.missing` meaningful: a source that
    changed in between is reported, not silently half-installed.

    Args:
        source: A local directory path, or a git repository URL.
        root: The user agents root to install into. Created if absent.
        replace: What to do about an entry already installed under the same
            name. ``True`` overwrites it; ``False`` keeps what is there and
            reports it in :attr:`InstallResult.kept`. The user answers this
            question after seeing :meth:`SourceScan.conflict_report`.
        names: Install only these entries. ``None`` installs everything the
            source offers.

    Returns:
        InstallResult: What was installed, kept, skipped and missing.

    Raises:
        AgentInstallError: The source is unreadable, the clone failed, or a file
            could not be written.
        GitNotAvailableError: The source is a URL and ``git`` is not available.
    """
    store = UserAgentStore(root)
    store.ensure_root()
    installed = _installed_versions(root)
    if _is_remote(source):
        require_git()
        with tempfile.TemporaryDirectory(prefix="kodo-agents-") as tmp:
            found = _read_source(_clone(source, Path(tmp)), installed)
            return _copy(found, store, replace=replace, names=names)
    found = _read_source(_local_dir(source), installed)
    return _copy(found, store, replace=replace, names=names)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_remote(source: str) -> bool:
    """Whether *source* should be cloned rather than read off disk."""
    return bool(_REMOTE_RE.match(source.strip()))


def _local_dir(source: str) -> Path:
    """Resolve *source* to a readable directory, or raise."""
    path = Path(source).expanduser()
    if not path.is_dir():
        raise AgentInstallError(f"{source}: not a directory")
    return path


def _clone(url: str, into: Path) -> Path:
    """Shallow-clone *url* into *into* and return the clone root."""
    target = into / "clone"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentInstallError(f"cloning {url} timed out after {_CLONE_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        raise AgentInstallError(f"cloning {url} failed: {exc.stderr.strip()}") from exc
    return target


def _installed_versions(root: Path) -> dict[tuple[str, str], str]:
    """``{(kind, name): version}`` for everything already installed under *root*."""
    versions: dict[tuple[str, str], str] = {}
    for agent in UserAgentStore(root).scan().agents:
        kind = KIND_AGENT if agent.is_top_level else KIND_SUBAGENT
        versions[(kind, agent.name)] = agent.version or UNVERSIONED
    return versions


def _read_source(directory: Path, installed: dict[tuple[str, str], str]) -> tuple[Candidate, ...]:
    """Find every agent and sub-agent *directory* offers."""
    agents = [_read_agent(p, directory, installed) for p in sorted(directory.glob("agent_*.md"))]
    shared = directory / SHARED_SUBAGENTS_DIRNAME
    subs = (
        [_read_subagent(p, installed) for p in sorted(shared.iterdir()) if p.is_dir()]
        if shared.is_dir()
        else []
    )
    return tuple(agents + subs)


def _read_agent(prompt: Path, directory: Path, installed: dict[tuple[str, str], str]) -> Candidate:
    """Build the candidate for one ``agent_<name>.md`` and its config."""
    name = prompt.stem[len("agent_") :]
    have = installed.get((KIND_AGENT, name), "")
    config = directory / f"{name}{TOP_AGENT_SUFFIX}"
    reserved = reserved_name_error(name)
    if reserved:
        return Candidate(name, KIND_AGENT, UNVERSIONED, have, reserved)
    if not config.is_file():
        return Candidate(
            name,
            KIND_AGENT,
            UNVERSIONED,
            have,
            f"no {name}{TOP_AGENT_SUFFIX} beside the prompt",
        )
    try:
        agent = load_agent(prompt)
        load_top_agent(config)
    except (AgentLoadError, TopAgentLoadError) as exc:
        return Candidate(name, KIND_AGENT, UNVERSIONED, have, _reason(exc))
    return Candidate(name, KIND_AGENT, agent.version or UNVERSIONED, have, "", (prompt, config))


def _read_subagent(directory: Path, installed: dict[tuple[str, str], str]) -> Candidate:
    """Build the candidate for one ``subagents/<name>/`` directory."""
    name = directory.name
    have = installed.get((KIND_SUBAGENT, name), "")
    prompt = directory / f"subagent_{name}.md"
    spec = directory / f"{name}{SPEC_SUFFIX}"
    reserved = reserved_name_error(name)
    if reserved:
        return Candidate(name, KIND_SUBAGENT, UNVERSIONED, have, reserved)
    if not prompt.is_file():
        return Candidate(
            name,
            KIND_SUBAGENT,
            UNVERSIONED,
            have,
            f"no subagent_{name}.md — a sub-agent's prompt is named after its directory",
        )
    if not spec.is_file():
        return Candidate(
            name, KIND_SUBAGENT, UNVERSIONED, have, f"no {name}{SPEC_SUFFIX} beside the prompt"
        )
    try:
        agent = load_agent(prompt)
        load_spec(spec)
    except (AgentLoadError, SpecLoadError) as exc:
        return Candidate(name, KIND_SUBAGENT, UNVERSIONED, have, _reason(exc))
    return Candidate(name, KIND_SUBAGENT, agent.version or UNVERSIONED, have, "", (prompt, spec))


def _copy(
    candidates: tuple[Candidate, ...],
    store: UserAgentStore,
    *,
    replace: bool,
    names: tuple[str, ...] | None,
) -> InstallResult:
    """Write the selected candidates into the store."""
    by_name = {c.name: c for c in candidates}
    wanted = names if names is not None else tuple(c.name for c in candidates)
    installed: list[str] = []
    kept: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for name in wanted:
        candidate = by_name.get(name)
        if candidate is None:
            missing.append(name)
            continue
        if not candidate.installable:
            skipped.append(f"{name}: {candidate.error}")
            continue
        if candidate.conflicts and not replace:
            kept.append(name)
            continue
        parent = store.root if candidate.kind == KIND_AGENT else store.subagents_dir
        destination = parent / name
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for path in candidate.files:
                shutil.copy2(path, destination / path.name)
        except OSError as exc:
            raise AgentInstallError(f"could not install {name!r}: {exc}") from exc
        installed.append(name)
    return InstallResult(tuple(installed), tuple(kept), tuple(skipped), tuple(missing))


def _reason(exc: Exception) -> str:
    """One sentence, with the absolute source path stripped off the front."""
    text = str(exc)
    _, _, tail = text.partition(": ")
    return tail or text
