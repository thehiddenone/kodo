"""Entry point for ``python -m kodo`` and the ``kodo`` CLI — prompt-generation
and tool-definition diagnostics.

``--system-prompt AGENT`` (``-p AGENT``) prints the exact system prompt kodo
would send for that agent, by calling the real runtime code rather than
reimplementing it: :meth:`~kodo.subagents.AgentRegistry.get` renders the
agent's body with every ``{SHARED:…}`` block expanded — exactly as
``kodo/server/_app.py`` does for a live session.

An agent's granted tools are deliberately **not** part of that prompt; they
reach the model through the LLM tool-definition ``tools`` argument, described by
:func:`kodo.toolspecs.tool_description` (see doc/TOOLS.md §7). So what
``--system-prompt`` prints is the whole system prompt, not an excerpt of it.

``--tools AGENT`` prints that other half: the exact ``tools=[...]`` payload
the OpenAI-compatible client sends for that agent, built by
:func:`kodo.llms.llamacpp.build_openai_tools` — the same function
:class:`~kodo.llms.llamacpp.LlamaPlugin` calls to talk to ``llama-server``, so
the output matches what actually reaches the model byte for byte, not a
reimplementation of the wire shape.

Both commands resolve an agent's tools through
:func:`kodo.runtime.agent_tool_specs`, so ``--tools`` reflects the same
autonomous-mode filtering and auto-granted ``return_result`` tool that
``AgentRegistry.get`` already applies for ``--system-prompt`` — including the
per-agent expansions: one ``run_subagent_<name>`` entry per sub-agent the agent
may invoke, and a ``return_result`` bound to its own output schema.

Only the interactive-mode prompt/tool set is rendered (``autonomous=False``).
There is deliberately no ``--autonomous`` flag.

``--model LLM_ID`` (``-m LLM_ID``) selects which model the prompt/tools are
rendered for — a local registry name or a cloud ``model_id``, looked up in the
local registry first, then by ``model_id`` across every cloud vendor. When
omitted, the first *installed* entry in the local registry is used instead —
same "installed" definition as the Kōdo Settings local-LLM list
(``kodo/server/_app.py``'s ``_local_entry_installed``): a
``custom_server_url`` entry always counts, ``custom_file`` checks the file is
still on disk, everything else is looked up in the local model manager's
on-disk state. If nothing is installed and ``--model`` is omitted, that's a
resolution error, same as an unknown ``--model`` value.

Today ``--model`` only has to *resolve* for either command — the rendered
prompt is identical for every model, because no plugin appends anything
model-specific (``LlamaPlugin`` and ``AnthropicPlugin`` both send the agent's
system prompt as-is), and the OpenAI tools shape is the one ``LlamaPlugin``
builds regardless of which vendor ``LLM_ID`` resolves to. The argument is kept
because per-LLM variation is planned: when model-specific behavior is added,
this is where it will show up, and the invocation will not have to change.

``--list-skills`` and ``--install-skill TARGET`` are a second, unrelated
pair of commands living in this same entry point for convenience — Agent
Skills (doc/SKILLS.md) management, not prompt/tool diagnostics. They call the
same :mod:`kodo.skills` functions the Kōdo Settings panel's install picker
calls over the WS wire (``skills.install_scan``/``skills.install``/
``skills.install_local`` — doc/WS_PROTOCOL.md §7.6j), so a skill visible to
one is visible to the other.

``--install-skill`` accepts two shapes of ``TARGET``, told apart by whether it
resolves to an existing path on disk (:func:`_run_install_skill`):

- **A local path** (absolute or relative to the current directory) — either a
  directory holding ``SKILL.md`` directly, or a direct path to the
  ``SKILL.md`` file itself. Installs that **one** skill, no ``git`` involved
  (:func:`kodo.skills.install_local_skill`). A same-named skill already
  installed is only overwritten after an explicit ``y`` at a confirmation
  prompt, or unconditionally under ``--yes``/``-y``.
- **Anything else** is treated as a git repository URL: it is cloned, every
  valid skill it contains is listed, and the user is asked once per skill
  (``Yes``/``No``/``All``) unless ``--yes``/``-y`` is given, in which case
  every valid skill is installed with no prompting at all — including
  overwriting a same-named skill already installed, since a non-interactive
  run has no terminal to confirm an overwrite on (see
  :func:`_run_install_skill_from_repo`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import kodo.subagents as _subagents_pkg
from kodo.llms import CloudLLMEntry, get_cloud_registry, get_local_registry
from kodo.llms.llamacpp import build_openai_tools, get_local_model_manager
from kodo.project import kodo_skills_dir, kodo_user_dir
from kodo.runtime import agent_tool_specs
from kodo.skills import (
    GitNotAvailableError,
    SkillInstallError,
    SkillStore,
    install_local_skill,
    install_skills,
    scan_repository,
)
from kodo.subagents import AgentLoadError, AgentRegistry, SubAgent

_AGENTS_DIR = Path(_subagents_pkg.__file__).parent


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested diagnostic command.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 = success).
    """
    args = _parse_args(argv)
    try:
        if args.list_skills:
            code = _run_list_skills()
        elif args.install_skill is not None:
            code = _run_install_skill(args.install_skill, assume_yes=args.yes)
        elif args.system_prompt is not None:
            code = _run_system_prompt(args.model, args.agent)
        else:
            code = _run_tools(args.model, args.agent)
        # Force the write out now, inside the try. stdout is fully block-buffered
        # when piped (not a tty), so the print()s above may never issue an actual
        # write() syscall — on some platforms/buffer sizes ~100 KB of output fits
        # in the buffer untouched. Without this flush, a downstream reader closing
        # early (`| head`) wouldn't raise BrokenPipeError until the interpreter's
        # automatic shutdown flush, which happens after this function has already
        # returned — outside this except clause entirely, printing an "Exception
        # ignored in..." message instead of being handled cleanly. See the recipe
        # in the Python docs' note on SIGPIPE.
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # A downstream reader closed the pipe (`| head`, quitting a pager) — not
        # a failure worth a traceback. Point stdout at devnull so the
        # interpreter's shutdown flush cannot raise a second time and print one
        # anyway.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and evaluate the argument parser.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="python -m kodo",
        description="Prompt- and tool-definition diagnostics for kodo's LLM/agent system.",
    )
    parser.add_argument(
        "--model",
        "-m",
        metavar="LLM_ID",
        dest="model",
        default=None,
        help="Local registry name or cloud model_id to resolve the prompt/tools for. "
        "Defaults to the first installed model in the local registry (an error if "
        "none is installed).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--system-prompt",
        "-p",
        metavar="AGENT",
        dest="system_prompt",
        help="Print AGENT's fully-rendered system prompt for --model/-m.",
    )
    group.add_argument(
        "--tools",
        metavar="AGENT",
        dest="tools",
        help="Print AGENT's tools=[...] payload exactly as submitted to the "
        "OpenAI-compatible client, for --model/-m.",
    )
    group.add_argument(
        "--list-skills",
        dest="list_skills",
        action="store_true",
        help="List every installed Agent Skill (name and description) under ~/.kodo/skills.",
    )
    group.add_argument(
        "--install-skill",
        metavar="TARGET",
        dest="install_skill",
        help="Install an Agent Skill into ~/.kodo/skills. TARGET is either a local path "
        "(a directory holding SKILL.md, or a direct path to a SKILL.md file — absolute or "
        "relative) installed as-is with no git involved, or a git repository URL, which is "
        "cloned so you can interactively choose which of the skill(s) it contains to install.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        dest="yes",
        action="store_true",
        help="With --install-skill, install without prompting — every valid skill found, for "
        "a repository URL, or the one skill at a local path, overwriting any same-named skill "
        "already installed.",
    )
    parsed = parser.parse_args(argv)
    parsed.agent = parsed.system_prompt if parsed.system_prompt is not None else parsed.tools
    return parsed


def _find_cloud_entry(model_id: str) -> CloudLLMEntry | None:
    """Look up *model_id* across every cloud vendor (there is no flat cloud registry key).

    Args:
        model_id (str): The API model identifier, e.g. ``"claude-sonnet-5"``.

    Returns:
        CloudLLMEntry | None: The matching entry, or ``None`` if no vendor has it.
    """
    for models in get_cloud_registry().values():
        for entry in models:
            if entry.model_id == model_id:
                return entry
    return None


def _first_installed_local_model(kodo_dir: Path) -> str | None:
    """The first installed entry in the local registry, in registry order.

    Mirrors the "installed" definition ``kodo/server/_app.py``'s
    ``_local_entry_installed`` uses for the Kōdo Settings local-LLM list: a
    ``custom_server_url`` entry is always installed (it points at an
    already-running server, not a local file); ``custom_file`` checks the
    file still exists on disk; every other kind is looked up in the local
    model manager's on-disk state.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        str | None: The entry's registry name, or ``None`` if nothing is
        installed.
    """
    manager = get_local_model_manager(kodo_dir)
    for entry in get_local_registry(kodo_dir).values():
        if entry.kind == "custom_server_url":
            return entry.name
        elif entry.kind == "custom_file":
            if Path(entry.path).is_file():
                return entry.name
        elif manager.get_model_path(entry.name) is not None:
            return entry.name
    return None


def _resolve(
    model_arg: str | None, agent_name: str
) -> tuple[AgentRegistry | None, SubAgent | None, int]:
    """Resolve *agent_name* then *model_arg*, the shared validation both commands need.

    Args:
        model_arg (str | None): Local registry name or cloud ``model_id`` from
            ``--model``/``-m``, or ``None`` to default to the first installed
            local model (see :func:`_first_installed_local_model`). Validated
            but not otherwise used — see this module's docstring.
        agent_name (str): Agent/subagent frontmatter ``name`` (not the
            filename) — e.g. ``"guide"``, ``"problem_solver"``, ``"architect"``.

    Returns:
        tuple[AgentRegistry | None, SubAgent | None, int]: ``(registry, agent, 0)``
        on success, or ``(None, None, 2)`` after printing an error — the agent is
        checked before the model. The registry rides along because ``--tools``
        needs it to expand the per-agent ``run_subagent_<name>`` / ``return_result``
        specs (see :func:`kodo.runtime.agent_tool_specs`).
    """
    registry = AgentRegistry(_AGENTS_DIR)
    try:
        agent = registry.get(agent_name, autonomous=False)
    except AgentLoadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None, None, 2

    kodo_dir = kodo_user_dir()
    if model_arg is None:
        if _first_installed_local_model(kodo_dir) is None:
            print(
                "Error: no --model/-m given and no model is installed in the local registry",
                file=sys.stderr,
            )
            return None, None, 2
    elif (
        get_local_registry(kodo_dir).get(model_arg) is None and _find_cloud_entry(model_arg) is None
    ):
        print(
            f"Error: unknown LLM id {model_arg!r} — not in the local or cloud registry",
            file=sys.stderr,
        )
        return None, None, 2

    return registry, agent, 0


def _run_system_prompt(model_arg: str | None, agent_name: str) -> int:
    """Resolve *model_arg* + *agent_name* and print the rendered system prompt.

    Args:
        model_arg (str | None): Local registry name or cloud ``model_id`` from
            ``--model``/``-m``, or ``None`` to default to the first installed
            local model.
        agent_name (str): Agent/subagent frontmatter ``name``.

    Returns:
        int: 0 on success, 2 on a resolution error (unknown/missing model, or
        unknown agent).
    """
    registry, agent, code = _resolve(model_arg, agent_name)
    if registry is None or agent is None:
        return code

    print(agent.system_prompt)
    return 0


def _run_tools(model_arg: str | None, agent_name: str) -> int:
    """Resolve *model_arg* + *agent_name* and print the agent's OpenAI tools payload.

    Args:
        model_arg (str | None): Local registry name or cloud ``model_id`` from
            ``--model``/``-m``, or ``None`` to default to the first installed
            local model.
        agent_name (str): Agent/subagent frontmatter ``name``.

    Returns:
        int: 0 on success, 2 on a resolution error (unknown/missing model, or
        unknown agent).
    """
    registry, agent, code = _resolve(model_arg, agent_name)
    if registry is None or agent is None:
        return code

    oai_tools = build_openai_tools(agent_tool_specs(registry, agent))
    print(json.dumps(oai_tools, indent=2))
    return 0


def _run_list_skills() -> int:
    """Print every installed Agent Skill: ``name: description``, or the load error.

    Returns:
        int: Always 0 — an empty or all-broken store is not an error.
    """
    skills = SkillStore(kodo_skills_dir()).entries()
    if not skills:
        print("No skills installed.")
        return 0
    for skill in skills:
        if skill.usable:
            print(f"{skill.name}: {skill.description}")
        else:
            print(f"{skill.name}: [broken] {skill.error}")
    return 0


def _run_install_skill(target: str, *, assume_yes: bool) -> int:
    """Dispatch ``--install-skill TARGET`` by whether *target* is a local path.

    *target* resolves against the current working directory the same way
    :func:`kodo.skills.install_local_skill` will resolve it: if that resolved
    path exists on disk (a directory or a file), this is a local-path install
    (:func:`_run_install_local_skill`) — no ``git`` involved, exactly one
    skill. Otherwise *target* is treated as a git repository URL, unchanged
    from before local-path support existed (:func:`_run_install_skill_from_repo`).

    Args:
        target: The ``--install-skill`` argument — a local path or a repo URL.
        assume_yes: ``--yes``/``-y``.

    Returns:
        int: See :func:`_run_install_local_skill`/:func:`_run_install_skill_from_repo`.
    """
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.exists():
        return _run_install_local_skill(target, assume_yes=assume_yes)
    return _run_install_skill_from_repo(target, assume_yes=assume_yes)


def _run_install_local_skill(path: str, *, assume_yes: bool) -> int:
    """Install the one skill at local *path* (doc/SKILLS.md §2).

    No prompt unless installing would overwrite an already-installed
    same-named skill — the user already named this exact path, unlike the
    repo flow's picker over several candidates, so there is nothing to
    confirm except a destructive overwrite.

    Args:
        path: A local directory holding ``SKILL.md``, or a direct path to a
            ``SKILL.md`` file.
        assume_yes: ``--yes``/``-y`` — overwrite an existing same-named skill
            without prompting.

    Returns:
        int: 0 on success, 2 if *path* does not resolve to a valid skill, or
        an overwrite was needed and stdin is not interactive and *assume_yes*
        was not given.
    """
    skills_root = kodo_skills_dir()
    try:
        result = install_local_skill(path, skills_root, overwrite=False)
    except SkillInstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if result.conflicts:
        name = result.conflicts[0]
        if not assume_yes:
            try:
                prompt = f"{name!r} is already installed — overwrite it? (y/n) "
                answer = input(prompt).strip().lower()
            except EOFError:
                print(
                    "Error: no more input to read (stdin is not interactive) — pass --yes/-y to "
                    "overwrite without prompting.",
                    file=sys.stderr,
                )
                return 2
            if answer not in ("y", "yes"):
                print("Installation cancelled.")
                return 0
        try:
            result = install_local_skill(path, skills_root, overwrite=True)
        except SkillInstallError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    for name in result.installed:
        print(f"Installed: {name}")
    return 0


def _run_install_skill_from_repo(repo_url: str, *, assume_yes: bool) -> int:
    """Clone *repo_url*, then install the skills the user (or ``--yes``) selects.

    Prompts once per valid skill found — ``Install NAME: DESCRIPTION (y/n/a/q)``
    — unless *assume_yes* is set, in which case every valid skill is selected
    with no prompting. A skill already installed locally is flagged inline
    (``already installed locally — will be overwritten``) rather than with a
    second prompt: answering ``y`` (or reaching it under ``a``/``--yes``) is
    itself the overwrite confirmation (doc/SKILLS.md §2).

    Args:
        repo_url: Passed straight to ``git clone``.
        assume_yes: ``--yes``/``-y`` — select every valid skill with no
            interactive loop at all, for scripts/CI.

    Returns:
        int: 0 on success (including "nothing selected" or "nothing valid
        found" — neither is a failure), 2 if ``git`` is missing or the clone
        failed, 2 if stdin isn't interactive and *assume_yes* was not given.
    """
    try:
        found = scan_repository(repo_url)
    except (GitNotAvailableError, SkillInstallError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not found:
        print(f"No valid skills found in {repo_url}.")
        return 0

    skills_root = kodo_skills_dir()
    already_installed = {s.name for s in SkillStore(skills_root).entries()}

    selections: dict[str, bool] = {}
    install_all = assume_yes
    try:
        for skill in found:
            if install_all:
                selections[skill.name] = True
                continue
            prompt = f"Install {skill.name}: {skill.description}"
            if skill.name in already_installed:
                prompt += "  [already installed locally — will be overwritten]"
            prompt += " (y/n/a/q) "
            while True:
                answer = input(prompt).strip().lower()
                if answer in ("y", "yes"):
                    selections[skill.name] = True
                    break
                if answer in ("n", "no", ""):
                    break
                if answer in ("a", "all"):
                    selections[skill.name] = True
                    install_all = True
                    break
                if answer in ("q", "quit"):
                    print("Installation cancelled.")
                    return 0
                print("Please answer y, n, a (yes to all remaining), or q (quit).")
    except EOFError:
        print(
            "Error: no more input to read (stdin is not interactive) — pass --yes/-y to "
            "install without prompting.",
            file=sys.stderr,
        )
        return 2

    if not selections:
        print("Nothing selected to install.")
        return 0

    result = install_skills(repo_url, selections, skills_root)

    for name in result.installed:
        print(f"Installed: {name}")
    for name in result.conflicts:
        print(f"Skipped (already installed, not confirmed): {name}", file=sys.stderr)
    for name in result.missing:
        print(f"Skipped (no longer found in {repo_url}): {name}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
