"""The Agent Builder's service — doc/AGENT_BUILDER.md §6.2.

:class:`AgentWorkshop` is what the builder's tools reach through the
``AgentWorkshopLike`` protocol in :mod:`kodo.tools`: it lists and reads agents,
copies a built-in, writes and edits user agents, and validates and lints them.
It plays the part :class:`kodo.llms.model_import.LocalCatalogService` plays for
the Model Importer: the agent only *states* what it wants, and the service is
the authority on layout, naming and what may be written (D11).

What the service guarantees
===========================

- **It builds every path itself.** A call names an agent and a kind, never a
  path, so nothing can be written outside the user root, and a built-in
  (``kodo_*``) or the reserved ``subagents`` name is refused before anything
  is touched.
- **A file it writes passes its strict loader.** Every write is rendered into a
  temporary directory and loaded with the same loader the store uses —
  :func:`~kodo.agents.load_agent`, :func:`~kodo.agents.load_top_agent`,
  :func:`~kodo.agents.subagents.load_spec` — before it replaces anything. What
  the loaders cannot check alone (a ``subagents:`` entry not written yet, a
  critic that does not exist) is cross-agent, so the registry is reloaded and
  the call returns the agent's :class:`~kodo.agents.AgentReport`, load error
  and lint findings included.
- **No delete** (D12). The builder creates and revises; removing an agent stays
  the user's action in the Settings panel.

Every method returns JSON-ready data and raises :class:`AgentWorkshopError` — a
:class:`ValueError`, which is what the protocol promises — with a message
written for the calling agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from kodo.toolspecs import RETURN_RESULT

from ._builtin import PACKAGED_AGENTS_DIR
from ._copy import AgentCopyError, agent_name_error, copy_agent, copy_sources, plan_copy
from ._install import KIND_AGENT, KIND_SUBAGENT
from ._lint import lint_prompt
from ._loader import AgentLoadError, SubAgent, load_agent
from ._registry import AgentRegistry
from ._topagent import TOP_AGENT_SUFFIX, TopAgentLoadError, load_top_agent
from ._userstore import BUILTIN_NAME_PREFIX, BrokenAgent, UserAgentStore
from ._validate import AgentValidator
from .subagents import SPEC_SUFFIX, SpecLoadError, load_spec

__all__ = [
    "AGENT_FILE_CONFIG",
    "AGENT_FILE_PROMPT",
    "AGENT_FILE_SPEC",
    "SOURCE_BUILTIN",
    "SOURCE_USER",
    "AgentWorkshop",
    "AgentWorkshopError",
]

#: ``source`` of a row for an agent Kōdo ships.
SOURCE_BUILTIN = "builtin"
#: ``source`` of a row for an agent under the user root.
SOURCE_USER = "user"

#: ``edit_agent``'s file selectors: the prompt, a top-level agent's config, a
#: sub-agent's spec.
AGENT_FILE_PROMPT = "prompt"
AGENT_FILE_CONFIG = "config"
AGENT_FILE_SPEC = "spec"

# Where a built-in sub-agent's spec sits, relative to its prompt's directory.
_BUILTIN_SPECS_SUBDIR = "specs"

# A sub-agent's description in a listing is the first sentence of its
# ``## Purpose``, cut at this many characters: the whole section runs to a
# paragraph for most built-ins, and a listing of thirty of them is read whole.
_DESCRIPTION_LIMIT = 240
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*`])")


class AgentWorkshopError(ValueError):
    """Raised for any request the workshop refuses or cannot complete.

    A :class:`ValueError` because that is what ``AgentWorkshopLike`` promises
    the tools, which cannot name a type from this package's tier.
    """


class AgentWorkshop:
    """List, read, copy, write, edit, validate and lint agents for Agent Builder.

    Bound to the process registry: every write lands in its user root and is
    followed by one :meth:`~kodo.agents.AgentRegistry.reload`, so the agent a
    call wrote is what the next listing, the picker and every session see.

    Args:
        registry: The process-wide registry. Its
            :attr:`~kodo.agents.AgentRegistry.user_dir` is the root every write
            goes to; a registry with none refuses every write.
        builtin_dir: The packaged agents directory copies are read from.
            Injectable for tests.
    """

    __registry: AgentRegistry
    __builtin_dir: Path

    def __init__(self, registry: AgentRegistry, *, builtin_dir: Path = PACKAGED_AGENTS_DIR) -> None:
        self.__registry = registry
        self.__builtin_dir = builtin_dir

    async def list_agents(self) -> dict[str, object]:
        """Every built-in and user agent, loaded or broken.

        Returns:
            dict[str, object]: ``{agents: [...], root}``. A row holds ``name``,
            ``kind``, ``source``, ``label``, ``description``, ``subagents``,
            ``critic``, ``role``, ``standalone``, ``copyable`` and ``error``;
            a top-level row adds ``selectable`` and ``interactive``.
        """
        copyable = await asyncio.to_thread(self.__copyable)
        configs = {top.name: top for top in self.__registry.top_agents()}
        users = self.__registry.user_agent_names
        rows: list[dict[str, object]] = []
        for name in sorted(self.__registry.agent_names()):
            agent = self.__registry.get(name)
            row: dict[str, object] = {
                "name": name,
                "kind": _kind(agent),
                "source": SOURCE_USER if name in users else SOURCE_BUILTIN,
                "label": agent.display_name,
                "description": _first_sentence(agent.purpose),
                "subagents": list(agent.subagent_order),
                "critic": agent.critic,
                "role": agent.role,
                "standalone": agent.standalone,
                "copyable": name in copyable,
                "error": "",
            }
            config = configs.get(name)
            if config is not None:
                row["label"] = config.label
                row["description"] = config.description
                row["selectable"] = config.selectable
                row["interactive"] = config.interactive
            rows.append(row)
        for broken in self.__registry.broken_agents:
            rows.append(
                {
                    "name": broken.name,
                    "kind": KIND_AGENT if broken.is_top_level else KIND_SUBAGENT,
                    "source": SOURCE_USER,
                    "label": broken.name,
                    "description": "",
                    "subagents": [],
                    "critic": "",
                    "role": "",
                    "standalone": False,
                    "copyable": False,
                    "error": broken.error,
                }
            )
        root = self.__registry.user_dir
        return {"agents": rows, "root": str(root) if root is not None else ""}

    async def read_agent(self, name: str) -> dict[str, object]:
        """One agent's files as written, and its frontmatter as the loader reads it.

        Args:
            name: A built-in, a loaded user agent, or a user agent that failed
                to load.

        Returns:
            dict[str, object]: ``{name, kind, source, error, prompt_path,
            prompt, json_path, json, frontmatter}``. ``prompt`` is the raw file,
            ``{SHARED:…}`` tokens unexpanded; ``frontmatter`` is empty when the
            prompt does not parse.

        Raises:
            AgentWorkshopError: No agent by that name is loaded or installed.
        """
        return await asyncio.to_thread(self.__read, name)

    async def copy_builtin(self, name: str, prefix: str, replace: bool | None) -> dict[str, object]:
        """Copy built-in *name*, with its whole pipeline, under *prefix* (§3).

        Args:
            name: The built-in to copy.
            prefix: Replaces ``kodo_`` on every copied member.
            replace: What to do with members whose names are taken: ``True``
                replaces them, ``False`` keeps them. ``None`` means the user has
                not been asked: when anything is taken nothing is written and
                the plan comes back for the question.

        Returns:
            dict[str, object]: ``{done, members, conflicts, copied, kept,
            failed}``. ``done`` is ``False`` only when the plan came back
            unanswered; ``failed`` names each copied member the reloaded
            registry could not load, with why.

        Raises:
            AgentWorkshopError: The source cannot be copied, the prefix is
                invalid, the copy could not be written, or there is no user root.
        """
        root = self.__root()
        try:
            plan = await asyncio.to_thread(
                plan_copy, name, prefix, root, builtin_dir=self.__builtin_dir
            )
        except AgentCopyError as exc:
            raise AgentWorkshopError(str(exc)) from exc
        members = [{"source": m.source, "name": m.name, "kind": m.kind} for m in plan.members]
        if plan.conflicting and replace is None:
            return {
                "done": False,
                "members": members,
                "conflicts": plan.conflict_report(),
                "copied": [],
                "kept": [],
                "failed": [],
            }
        try:
            result = await asyncio.to_thread(
                copy_agent,
                name,
                prefix,
                root,
                replace=bool(replace),
                builtin_dir=self.__builtin_dir,
            )
        except AgentCopyError as exc:
            raise AgentWorkshopError(str(exc)) from exc
        self.__reload()
        copied = frozenset(result.copied)
        failed = [f"{b.name}: {b.error}" for b in self.__registry.broken_agents if b.name in copied]
        return {
            "done": True,
            "members": members,
            "conflicts": "",
            "copied": list(result.copied),
            "kept": list(result.kept),
            "failed": failed,
        }

    async def write_agent(
        self, kind: str, name: str, prompt: str, config: Mapping[str, object]
    ) -> dict[str, object]:
        """Create or overwrite one user agent: its prompt and its config or spec.

        Args:
            kind: ``agent`` (a top-level agent: prompt + config) or
                ``subagent`` (prompt + spec).
            name: The agent's name. It names the directory and both files.
            prompt: The whole prompt file, frontmatter included.
            config: The top-level config or the sub-agent spec. Its
                ``"name"`` is filled in when absent.

        Returns:
            dict[str, object]: ``{files, report}`` — the two paths written, and
            the agent's report after the registry reloaded.

        Raises:
            AgentWorkshopError: An invalid kind or name, a name the other kind
                already uses, a file that does not pass its loader, or a write
                that failed.
        """
        root = self.__root()
        _check_kind(kind)
        _check_new_name(name)
        other = self.__directory(root, name, top_level=kind != KIND_AGENT)
        if other.is_dir():
            other_kind = KIND_SUBAGENT if kind == KIND_AGENT else KIND_AGENT
            raise AgentWorkshopError(
                f"a user {other_kind} named {name!r} already exists, and agent names are one "
                f"namespace across both kinds — choose another name"
            )
        declared = config.get("name", name)
        if declared != name:
            raise AgentWorkshopError(
                f'the config\'s "name" is {declared!r} but the agent is {name!r} — they must agree'
            )
        files = {
            _prompt_filename(kind, name): prompt if prompt.endswith("\n") else prompt + "\n",
            f"{name}{_json_suffix(kind)}": (
                json.dumps({**config, "name": name}, indent=2, ensure_ascii=False) + "\n"
            ),
        }
        directory = self.__directory(root, name, top_level=kind == KIND_AGENT)
        written = await asyncio.to_thread(self.__write, kind, directory, files)
        return {"files": [str(path) for path in written], "report": self.__reloaded_report(name)}

    async def edit_agent(
        self, name: str, file: str, old_string: str, new_string: str
    ) -> dict[str, object]:
        """Replace one exact string in one user agent file.

        Args:
            name: A user agent, loaded or broken.
            file: ``prompt``, ``config`` (a top-level agent's) or ``spec``
                (a sub-agent's).
            old_string: Text that occurs exactly once in the file.
            new_string: What replaces it.

        Returns:
            dict[str, object]: ``{files, report}``, as :meth:`write_agent`.

        Raises:
            AgentWorkshopError: No such user agent, a file selector that does
                not fit its kind, *old_string* absent or ambiguous, an edited
                file that does not pass its loader, or a write that failed.
        """
        root = self.__root()
        kind, directory = self.__user_directory(root, name)
        if file == AGENT_FILE_PROMPT:
            filename = _prompt_filename(kind, name)
        elif file == (AGENT_FILE_CONFIG if kind == KIND_AGENT else AGENT_FILE_SPEC):
            filename = f"{name}{_json_suffix(kind)}"
        else:
            expected = AGENT_FILE_CONFIG if kind == KIND_AGENT else AGENT_FILE_SPEC
            raise AgentWorkshopError(
                f"file must be {AGENT_FILE_PROMPT!r} or {expected!r} for the {kind} {name!r}, "
                f"not {file!r}"
            )
        written = await asyncio.to_thread(
            self.__edit, kind, directory, filename, old_string, new_string
        )
        return {"files": [str(written)], "report": self.__reloaded_report(name)}

    async def validate_agent(self, name: str) -> dict[str, object]:
        """The load check and lint for one agent (§5.1, §5.2).

        Args:
            name: A built-in, a loaded user agent, or a broken one.

        Returns:
            dict[str, object]: The :meth:`~kodo.agents.AgentReport.as_payload`
            report.

        Raises:
            AgentWorkshopError: No agent by that name is loaded or installed.
        """
        report = AgentValidator(self.__registry).validate(name)
        if report is None:
            raise AgentWorkshopError(_unknown(name))
        return report.as_payload()

    async def lint_agent(self, kind: str, name: str, prompt: str) -> dict[str, object]:
        """Lint a draft prompt that has not been written (§5.2).

        Args:
            kind: ``agent`` or ``subagent`` — decides the sections the draft
                needs, and that a sub-agent is granted ``return_result``.
            name: The name the draft will be written under.
            prompt: The whole draft file, frontmatter included.

        Returns:
            dict[str, object]: ``{name, kind, findings}``.

        Raises:
            AgentWorkshopError: An invalid kind or name, or a draft its loader
                refuses — the lint needs a draft that parses.
        """
        _check_kind(kind)
        error = agent_name_error(name)
        if error:
            raise AgentWorkshopError(error)
        agent = await asyncio.to_thread(_parse_draft, kind, name, prompt)
        granted = agent.tools | {RETURN_RESULT.name} if kind == KIND_SUBAGENT else agent.tools
        validator = AgentValidator(self.__registry)
        findings = lint_prompt(
            agent,
            prompt,
            granted,
            validator.context,
            authors=self.__registry.authors_of(name),
        )
        return {"name": name, "kind": kind, "findings": [f.as_payload() for f in findings]}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def __root(self) -> Path:
        """The user root every write goes to."""
        root = self.__registry.user_dir
        if root is None:
            raise AgentWorkshopError("this server has no user agents directory to write to")
        return root

    def __copyable(self) -> frozenset[str]:
        """The built-ins ``copy_builtin`` accepts as a source."""
        try:
            return frozenset(source.name for source in copy_sources(self.__builtin_dir))
        except AgentCopyError:
            return frozenset()

    def __reload(self) -> None:
        """Rebuild the registry so what was just written is what everyone sees."""
        try:
            self.__registry.reload()
        except AgentLoadError as exc:
            raise AgentWorkshopError(
                f"the files were written, but the agent registry could not reload: {exc}"
            ) from exc

    def __reloaded_report(self, name: str) -> dict[str, object]:
        """Reload, then report on *name* as the new registry sees it."""
        self.__reload()
        report = AgentValidator(self.__registry).validate(name)
        if report is None:
            # Written but not found by the store: only a layout bug can do that.
            raise AgentWorkshopError(f"{name!r} was written but the registry does not list it")
        return report.as_payload()

    @staticmethod
    def __directory(root: Path, name: str, *, top_level: bool) -> Path:
        """Where user agent *name* of the given kind lives under *root*."""
        store = UserAgentStore(root)
        return (store.root if top_level else store.subagents_dir) / name

    def __user_directory(self, root: Path, name: str) -> tuple[str, Path]:
        """The kind and directory of the existing user agent *name*."""
        if name.startswith(BUILTIN_NAME_PREFIX):
            raise AgentWorkshopError(
                f"{name!r} is a built-in agent and is read-only — copy it with "
                f"copy_builtin_agent and edit the copy"
            )
        error = agent_name_error(name)
        if error:
            raise AgentWorkshopError(error)
        top = self.__directory(root, name, top_level=True)
        sub = self.__directory(root, name, top_level=False)
        if top.is_dir():
            return KIND_AGENT, top
        if sub.is_dir():
            return KIND_SUBAGENT, sub
        raise AgentWorkshopError(f"no user agent named {name!r} is installed")

    def __read(self, name: str) -> dict[str, object]:
        """Read *name*'s files; see :meth:`read_agent`."""
        error = ""
        if name in self.__registry.agent_names():
            source_path = self.__registry.get(name).source_path
            source = SOURCE_USER if name in self.__registry.user_agent_names else SOURCE_BUILTIN
        else:
            broken = _find_broken(self.__registry.broken_agents, name)
            if broken is None:
                raise AgentWorkshopError(_unknown(name))
            source = SOURCE_USER
            error = broken.error
            stem = "agent_" if broken.is_top_level else "subagent_"
            directory = broken.path if broken.path.is_dir() else broken.path.parent
            source_path = directory / f"{stem}{name}.md"
        top_level = source_path.name.startswith("agent_")
        kind = KIND_AGENT if top_level else KIND_SUBAGENT
        if top_level or source == SOURCE_USER:
            json_path = source_path.parent / f"{name}{_json_suffix(kind)}"
        else:
            json_path = source_path.parent / _BUILTIN_SPECS_SUBDIR / f"{name}{SPEC_SUFFIX}"
        frontmatter: dict[str, object] = {}
        with contextlib.suppress(AgentLoadError, OSError):
            frontmatter = _frontmatter(load_agent(source_path))
        return {
            "name": name,
            "kind": kind,
            "source": source,
            "error": error,
            "prompt_path": str(source_path),
            "prompt": _read_text(source_path),
            "json_path": str(json_path),
            "json": _read_text(json_path),
            "frontmatter": frontmatter,
        }

    @staticmethod
    def __write(kind: str, directory: Path, files: dict[str, str]) -> list[Path]:
        """Check *files* with their loaders, then write each one atomically."""
        with tempfile.TemporaryDirectory(prefix="kodo-agent-draft-") as staging:
            for filename, text in files.items():
                (Path(staging) / filename).write_text(text, encoding="utf-8")
            for filename in files:
                _check_file(kind, Path(staging) / filename)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            written = []
            for filename, text in files.items():
                written.append(_atomic_write(directory / filename, text))
        except OSError as exc:
            raise AgentWorkshopError(f"could not write {directory}: {exc}") from exc
        return written

    @staticmethod
    def __edit(kind: str, directory: Path, filename: str, old: str, new: str) -> Path:
        """Apply one exact replacement to *filename*, checked before it is written."""
        path = directory / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentWorkshopError(f"could not read {path}: {exc}") from exc
        if not old:
            raise AgentWorkshopError("old_string is empty — give text that occurs in the file")
        count = text.count(old)
        if count == 0:
            raise AgentWorkshopError(
                f"old_string does not occur in {filename} — read the file with read_agent and "
                f"copy the text exactly"
            )
        if count > 1:
            raise AgentWorkshopError(
                f"old_string occurs {count} times in {filename} — include more of the "
                f"surrounding text so it occurs once"
            )
        edited = text.replace(old, new, 1)
        with tempfile.TemporaryDirectory(prefix="kodo-agent-draft-") as staging:
            staged = Path(staging) / filename
            staged.write_text(edited, encoding="utf-8")
            _check_file(kind, staged)
        try:
            return _atomic_write(path, edited)
        except OSError as exc:
            raise AgentWorkshopError(f"could not write {path}: {exc}") from exc


def _kind(agent: SubAgent) -> str:
    """``agent`` for a top-level agent, ``subagent`` otherwise."""
    return KIND_AGENT if agent.is_top_level else KIND_SUBAGENT


def _check_kind(kind: str) -> None:
    """Refuse anything but the two kinds."""
    if kind not in (KIND_AGENT, KIND_SUBAGENT):
        raise AgentWorkshopError(f"kind must be {KIND_AGENT!r} or {KIND_SUBAGENT!r}, not {kind!r}")


def _check_new_name(name: str) -> None:
    """Refuse a name Kōdo would not write."""
    error = agent_name_error(name)
    if error:
        raise AgentWorkshopError(error)


def _prompt_filename(kind: str, name: str) -> str:
    """``agent_<name>.md`` or ``subagent_<name>.md``."""
    return f"{'agent' if kind == KIND_AGENT else 'subagent'}_{name}.md"


def _json_suffix(kind: str) -> str:
    """The config's or the spec's extension."""
    return TOP_AGENT_SUFFIX if kind == KIND_AGENT else SPEC_SUFFIX


def _check_file(kind: str, path: Path) -> None:
    """Load *path* with its strict loader, and refuse what a user agent may not declare."""
    try:
        if path.suffix == ".md":
            load_agent(path)
        elif kind == KIND_AGENT:
            config = load_top_agent(path)
            if config.default:
                raise AgentWorkshopError(
                    f'{path.name} declares "default": true, which a user agent cannot claim — '
                    f"remove it"
                )
            if not config.interactive:
                raise AgentWorkshopError(
                    f'{path.name} declares "interactive": false, which only a built-in agent '
                    f"can — remove it"
                )
        else:
            load_spec(path)
    except (AgentLoadError, TopAgentLoadError, SpecLoadError) as exc:
        reason = str(exc).replace(f"{path}: ", "")
        raise AgentWorkshopError(f"{path.name} does not load: {reason}") from exc


def _parse_draft(kind: str, name: str, prompt: str) -> SubAgent:
    """Parse a draft prompt as the store would parse it under its real filename."""
    with tempfile.TemporaryDirectory(prefix="kodo-agent-draft-") as staging:
        path = Path(staging) / _prompt_filename(kind, name)
        path.write_text(prompt, encoding="utf-8")
        try:
            return load_agent(path)
        except AgentLoadError as exc:
            reason = str(exc).replace(f"{path}: ", "")
            raise AgentWorkshopError(f"the draft does not parse: {reason}") from exc


def _atomic_write(path: Path, text: str) -> Path:
    """Write *text* to *path* through a hidden temp file and ``os.replace``."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _read_text(path: Path) -> str:
    """A file's text, or ``""`` when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _frontmatter(agent: SubAgent) -> dict[str, object]:
    """The frontmatter keys as the loader resolved them."""
    return {
        "name": agent.name,
        "display_name": agent.display_name,
        "capability": agent.capability,
        "tools": sorted(agent.tools),
        "subagents": list(agent.subagent_order),
        "role": agent.role,
        "critic": agent.critic,
        "standalone": agent.standalone,
        "user_review": agent.user_review,
        "planner": agent.planner,
        "version": agent.version,
    }


def _find_broken(broken: tuple[BrokenAgent, ...], name: str) -> BrokenAgent | None:
    """The broken row named *name*, if any."""
    return next((entry for entry in broken if entry.name == name), None)


def _first_sentence(text: str) -> str:
    """The first sentence of *text*, cut at :data:`_DESCRIPTION_LIMIT` characters."""
    flat = " ".join(text.split())
    sentence = _SENTENCE_END_RE.split(flat, maxsplit=1)[0]
    if len(sentence) <= _DESCRIPTION_LIMIT:
        return sentence
    return sentence[: _DESCRIPTION_LIMIT - 1].rstrip() + "…"


def _unknown(name: str) -> str:
    """The refusal for a name no agent has."""
    return f"no agent named {name!r} is loaded or installed — call list_agents for the names"
