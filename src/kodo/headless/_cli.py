"""``kodo-headless`` — run one prompt through kodo with no user present.

::

    kodo-headless (--prompt TEXT | --prompt-file PATH) --model ENTRY
      [--agent kodo_problem_solver] [--llama-url URL | --llama-port N] [--cwd DIR]
      [--registry-file PATH] [--thinking-level TIER] [--timeout SEC]
      [--format jsonl|text] [--stream-deltas] [--result PATH]
      [--home DIR] [--keep-home] [--keep-kodo-dir] [--log-level INFO]

The working directory (``--cwd``, default: where it was launched) is the
sandbox: the run may change files only inside it, and may only *read* git.
Everything the run does is written to stdout; the process exit code says how
it ended (doc/HEADLESS.md).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ._events import OUTPUT_FORMATS, EventSink
from ._result import RunOutcome
from ._run import HeadlessOptions, HeadlessRun, install_signal_handlers

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kodo-headless",
        description="Run one prompt through kodo autonomously, sandboxed to the working directory.",
    )
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="The instruction for the agent.")
    prompt.add_argument("--prompt-file", type=Path, help="Read the instruction from this file.")
    parser.add_argument(
        "--model",
        required=True,
        help="Local-registry entry name (LLM + quant); required even with --llama-url.",
    )
    parser.add_argument("--agent", default="kodo_problem_solver", help="Top-level agent.")
    llama = parser.add_mutually_exclusive_group()
    llama.add_argument(
        "--llama-url", help="Attach to this running llama-server (http://host:port)."
    )
    llama.add_argument("--llama-port", type=int, help="Port for a spawned llama-server.")
    parser.add_argument("--cwd", type=Path, default=None, help="Sandbox root (default: cwd).")
    parser.add_argument(
        "--registry-file", type=Path, help="Local-LLM registry to use (e.g. a mounted host copy)."
    )
    parser.add_argument("--thinking-level", help="Thinking tier for the model's family.")
    parser.add_argument("--timeout", type=float, default=3000.0, help="Turn timeout (seconds).")
    parser.add_argument("--format", choices=OUTPUT_FORMATS, default="jsonl", help="stdout format.")
    parser.add_argument(
        "--stream-deltas", action="store_true", help="Emit every thinking/text chunk."
    )
    parser.add_argument("--result", type=Path, help="Also write the result JSON here.")
    parser.add_argument("--home", type=Path, help="Build the isolated kodo home here (kept).")
    parser.add_argument("--keep-home", action="store_true", help="Keep a temporary home.")
    parser.add_argument(
        "--keep-kodo-dir", action="store_true", help="Keep <cwd>/.kodo if this run created it."
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the ``kodo-headless`` CLI.

    Args:
        argv (list[str] | None): Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: The run's exit code (``RunOutcome.exit_code``).
    """
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if args.prompt_file is not None:
        try:
            prompt = args.prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --prompt-file: {exc}", file=sys.stderr)
            return RunOutcome.STARTUP_ERROR.exit_code
    else:
        prompt = str(args.prompt)
    if not prompt.strip():
        print("error: the prompt is empty", file=sys.stderr)
        return RunOutcome.STARTUP_ERROR.exit_code
    cwd = (args.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return RunOutcome.STARTUP_ERROR.exit_code

    options = HeadlessOptions(
        prompt=prompt,
        model=args.model,
        cwd=cwd,
        agent=args.agent,
        llama_url=args.llama_url.rstrip("/") if args.llama_url else None,
        llama_port=args.llama_port,
        registry_file=args.registry_file,
        thinking_level=args.thinking_level,
        timeout=args.timeout,
        home=args.home,
        keep_home=args.keep_home,
        keep_kodo_dir=args.keep_kodo_dir,
        result_path=args.result,
        log_level=args.log_level,
    )
    sink = EventSink(args.format)

    async def _run() -> int:
        run = HeadlessRun(options, sink, stream_deltas=args.stream_deltas)
        install_signal_handlers(run)
        return RunOutcome((await run.run()).outcome).exit_code

    return asyncio.run(_run())
