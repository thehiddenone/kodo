"""Entry point for ``python -m kodo.llms`` — prompt-generation diagnostics.

``--system-prompt LLM_ID AGENT`` prints the exact system prompt kodo would
send for that ``(model, agent)`` pair, by calling the real runtime code
rather than reimplementing it: :meth:`~kodo.subagents.AgentRegistry.get`
renders the agent's body — preambles, bases, sub-agent roster, task contract —
exactly as ``kodo/server/_app.py`` does for a live session.

An agent's granted tools are deliberately **not** part of that prompt; they
reach the model through the LLM tool-definition ``tools`` argument, described by
:func:`kodo.toolspecs.tool_description` (see doc/TOOLS.md §7). So what this
prints is the whole system prompt, not an excerpt of it.

Only the interactive-mode prompt is rendered (``autonomous=False``). There is
deliberately no ``--autonomous`` flag.

``LLM_ID`` is looked up in the local registry first, then by ``model_id``
across every cloud vendor. Today it only has to *resolve* — the rendered prompt
is identical for every model, because no plugin appends anything model-specific
(``LlamaPlugin`` and ``AnthropicPlugin`` both send the agent's system prompt
as-is). The argument is kept because per-LLM prompt variation is planned: when
model-specific text is added to work around individual models' quirks, this is
where it will show up, and the invocation will not have to change.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import kodo.subagents as _subagents_pkg
from kodo.llms import CloudLLMEntry, get_cloud_registry, get_local_registry
from kodo.project import kodo_user_dir
from kodo.subagents import AgentLoadError, AgentRegistry

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
        return _run_system_prompt(args.llm_id, args.agent)
    except BrokenPipeError:
        # A downstream reader closed the pipe (`| head`, quitting a pager). The
        # prompts this prints run to ~100 KB, so piping is the normal way to use
        # the command — not a failure worth a traceback. Point stdout at devnull
        # so the interpreter's shutdown flush cannot raise a second time and
        # print one anyway (the recipe from the Python docs' note on SIGPIPE).
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
        prog="python -m kodo.llms",
        description="Prompt-generation diagnostics for kodo's LLM/agent system.",
    )
    parser.add_argument(
        "--system-prompt",
        nargs=2,
        metavar=("LLM_ID", "AGENT"),
        dest="system_prompt",
        required=True,
        help="Print AGENT's fully-rendered system prompt for LLM_ID — a local "
        "registry name or a cloud model_id. Both are accepted and must resolve; "
        "the prompt itself is currently the same for every model.",
    )
    parsed = parser.parse_args(argv)
    parsed.llm_id, parsed.agent = parsed.system_prompt
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


def _run_system_prompt(llm_id: str, agent_name: str) -> int:
    """Resolve *llm_id* + *agent_name* and print the rendered system prompt.

    Args:
        llm_id (str): Local registry name or cloud ``model_id``. Validated but
            not otherwise used — see this module's docstring.
        agent_name (str): Agent/subagent frontmatter ``name`` (not the
            filename) — e.g. ``"guide"``, ``"problem_solver"``, ``"architect"``.

    Returns:
        int: 0 on success, 2 on a resolution error (unknown LLM/agent).
    """
    registry = AgentRegistry(_AGENTS_DIR)
    try:
        agent = registry.get(agent_name, autonomous=False)
    except AgentLoadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    kodo_dir = kodo_user_dir()
    if get_local_registry(kodo_dir).get(llm_id) is None and _find_cloud_entry(llm_id) is None:
        print(
            f"Error: unknown LLM id {llm_id!r} — not in the local or cloud registry",
            file=sys.stderr,
        )
        return 2

    print(agent.system_prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
