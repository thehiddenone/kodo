"""Entry point for ``python -m kodo.llms`` — prompt-generation diagnostics.

``--system-prompt LLM_ID AGENT`` prints the exact system prompt kodo would
send for that ``(model, agent)`` pair, by calling the real runtime code
rather than reimplementing it:

* :meth:`~kodo.subagents.AgentRegistry.get` renders the agent's body —
  preambles, tool/subagent placeholders, task contract — exactly as
  ``kodo/server/_app.py`` does for a live session.
* For a local (llama.cpp) ``LLM_ID``, :func:`~kodo.llms.llamacpp.resolve_chat_template`
  resolves the model's chat template (from the sidecar-backed cache, see
  doc/LOCAL_INFERENCE.md §6.7) and
  :func:`~kodo.llms.toolformat.render_tool_call_examples` appends the
  ``## Tool Call Format``/``## Tool Arguments`` section — the same two calls
  ``LlamaPlugin.__with_tool_call_examples`` makes per turn.
* For a cloud (Anthropic) ``LLM_ID``, nothing is appended — the real
  ``AnthropicPlugin`` path sends the agent's system prompt as-is, with no
  local-only section.

``LLM_ID`` is looked up in the local registry first, then by ``model_id``
across every cloud vendor.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import kodo.subagents as _subagents_pkg
from kodo.llms import CloudLLMEntry, get_cloud_registry, get_local_registry
from kodo.llms.llamacpp import resolve_chat_template
from kodo.llms.toolformat import render_tool_call_examples
from kodo.project import kodo_user_dir
from kodo.subagents import AgentLoadError, AgentRegistry
from kodo.toolspecs import ALL_TOOLS, ToolSpec

_AGENTS_DIR = Path(_subagents_pkg.__file__).parent
_TOOL_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested diagnostic command.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 = success).
    """
    args = _parse_args(argv)
    return _run_system_prompt(args.llm_id, args.agent)


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
        help="Print AGENT's fully-rendered system prompt for LLM_ID — local registry "
        "name or cloud model_id — including the local-only tool-call-format section "
        "when LLM_ID is a llama.cpp model.",
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
        llm_id (str): Local registry name or cloud ``model_id``.
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
    local_entry = get_local_registry(kodo_dir).get(llm_id)
    if local_entry is None and _find_cloud_entry(llm_id) is None:
        print(
            f"Error: unknown LLM id {llm_id!r} — not in the local or cloud registry",
            file=sys.stderr,
        )
        return 2

    system_prompt = agent.system_prompt
    if local_entry is not None:
        tools = [_TOOL_SPECS_BY_NAME[name] for name in sorted(agent.tools)]
        template = asyncio.run(resolve_chat_template(local_entry, kodo_dir))
        section = render_tool_call_examples(tools, chat_template=template)
        if section:
            system_prompt = f"{system_prompt}\n\n{section}"
    # A cloud LLM_ID gets no section appended — AnthropicPlugin sends the
    # agent's system prompt unmodified (see this module's docstring).

    print(system_prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
