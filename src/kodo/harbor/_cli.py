"""``kodo-harbor`` — run a benchmark on Kodo through Harbor, and report it.

::

    kodo-harbor run --model (VENDOR/MODEL_ID | ENTRY) [--agent kodo_problem_solver]
        (--dataset NAME[@VER] | --task ORG/NAME[@REF] | --path DIR | --suite NAME|FILE)...
        [--include GLOB]... [--exclude GLOB]... [--n-tasks N]
        [--control terminus-2]... [--attempts K] [--concurrency N] [--max-retries N]
        [--thinking-level TIER] [--timeout SEC] [--platform OS/ARCH]
        [--kodo-wheel PATH | --kodo-version VERSION]
        [--jobs-dir jobs] [--job-name NAME]
        [--llama-port 8090] [--llama-bind ADDR] [--keep-llama | --llama-url URL]
        [--dry-run] [-- HARBOR_RUN_ARGS...]
    kodo-harbor summarize JOB_DIR [--json]
    kodo-harbor list-suites [--json]
    kodo-harbor list-tasks (--suite NAME|FILE | --dataset NAME[@VER])... [--json]
    kodo-harbor list-models [local|cloud] [--json]

``run`` checks everything it can before Docker starts (the model, its
credential, the tasks, Docker itself), starts the host llama-server for a
local model (or, with ``--llama-url``, checks and attaches to one already
running on this host), writes ``<job>/kodo-job.json``, runs Harbor on it, and writes
``kodo-summary.md`` / ``kodo-summary.json`` next to Harbor's own results.
Exit code: Harbor's, or ``2`` when the run could not start (doc/HARBOR.md).
``list-models`` prints the ``--model`` values ``run`` can run: installed local
models, and the cloud catalog marked with whether this shell has each
vendor's credential. ``list-tasks`` prints every task each suite or dataset
holds (the names ``--include`` matches), downloading each dataset into a
temporary directory it deletes afterwards.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from kodo.project import kodo_agents_dir, kodo_user_dir

from ._errors import HarborRunError
from ._job import TERMINUS_2, JobPlan, KodoInstall, platform_overlay
from ._llama import HostLlama, LlamaAccess
from ._model import BenchModel, ListedModel
from ._runner import HarborInvocation, KodoSource, check_docker, find_uv
from ._selection import Selection
from ._suites import SuiteCatalog
from ._summary import JobSummary
from ._tasks import TaskGroup, TaskLister

__all__ = ["main"]

_JOB_CONFIG = "kodo-job.json"
_COMPOSE_OVERLAY = "kodo-compose.yaml"
_PLATFORM_OVERLAY = "kodo-platform.yaml"
_SUMMARY_MD = "kodo-summary.md"
_SUMMARY_JSON = "kodo-summary.json"
_BUILTIN_PREFIX = "kodo_"
_STARTUP_FAILURE = 2
_INTERRUPTED = 130
_DEFAULT_LLAMA_PORT = 8090


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kodo-harbor", description="Benchmark Kodo agents with Harbor (and Docker)."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run a benchmark and summarize it.")
    run.add_argument(
        "--model", required=True, help="VENDOR/MODEL_ID (cloud) or ENTRY (local registry)."
    )
    run.add_argument("--agent", default="kodo_problem_solver", help="Kodo top-level agent.")
    what = run.add_argument_group("what to run (repeatable, combinable)")
    what.add_argument("--dataset", action="append", default=[], help="NAME[@VERSION].")
    what.add_argument("--task", action="append", default=[], help="ORG/NAME[@REF].")
    what.add_argument("--path", action="append", default=[], type=Path, help="Local task/dataset.")
    what.add_argument("--suite", action="append", default=[], help="Suite name or JSON file.")
    what.add_argument("--include", action="append", default=[], help="Task-name glob to keep.")
    what.add_argument("--exclude", action="append", default=[], help="Task-name glob to drop.")
    what.add_argument("--n-tasks", type=int, help="At most N tasks per dataset.")
    how = run.add_argument_group("how")
    how.add_argument(
        "--control", action="append", default=[], help=f"Control-arm agent, e.g. {TERMINUS_2}."
    )
    how.add_argument("--attempts", type=int, default=1, help="Attempts per task and agent.")
    how.add_argument(
        "--concurrency", type=int, help="Trials at once (default: 4 cloud, 1 local model)."
    )
    how.add_argument("--max-retries", type=int, default=0, help="Retries for errored trials.")
    how.add_argument(
        "--thinking-level",
        help="Kodo thinking tier for the model, e.g. unlimited for a local Qwen-family "
        "model (no thinking cap); default: the model's own default tier.",
    )
    how.add_argument("--timeout", type=float, help="Kodo-side turn bound in seconds.")
    how.add_argument(
        "--platform",
        help="Docker platform for the task images, e.g. linux/amd64 (default: Docker's own).",
    )
    source = how.add_mutually_exclusive_group()
    source.add_argument("--kodo-wheel", type=Path, help="py-kodo wheel for the containers.")
    source.add_argument("--kodo-version", help="py-kodo release (PyPI) for the containers.")
    out = run.add_argument_group("output")
    out.add_argument("--jobs-dir", type=Path, default=Path("jobs"), help="Harbor jobs directory.")
    out.add_argument("--job-name", help="Job name (default: derived and timestamped).")
    local = run.add_argument_group("local model")
    local.add_argument(
        "--llama-port", type=int, help=f"Host llama-server port (default {_DEFAULT_LLAMA_PORT})."
    )
    local.add_argument("--llama-bind", help="Host address the llama-server listens on.")
    local.add_argument("--keep-llama", action="store_true", help="Leave it running afterwards.")
    local.add_argument(
        "--llama-url",
        help="Attach to a llama-server already running on this host (http://HOST:PORT); "
        "never started or stopped. Excludes --llama-port/--llama-bind/--keep-llama.",
    )
    run.add_argument("--dry-run", action="store_true", help="Write the job config; run nothing.")
    # For the Kōdo server, which runs this in its own process group (or hidden
    # console) and stops it by closing stdin (doc/BENCHMARK_SESSIONS.md §4.4).
    run.add_argument("--stop-on-stdin-close", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("harbor_args", nargs=argparse.REMAINDER, help="-- then harbor run args.")

    summarize = commands.add_parser("summarize", help="Summarize a Harbor job directory.")
    summarize.add_argument("job_dir", type=Path)
    summarize.add_argument("--json", action="store_true", help="Print JSON instead.")

    suites = commands.add_parser("list-suites", help="List the benchmark suites.")
    suites.add_argument("--json", action="store_true", help="Machine-readable output.")

    tasks = commands.add_parser(
        "list-tasks", help="List every task a suite or dataset holds (downloads, then deletes)."
    )
    tasks.add_argument("--suite", action="append", default=[], help="Suite name or JSON file.")
    tasks.add_argument("--dataset", action="append", default=[], help="NAME[@VERSION].")
    tasks.add_argument("--json", action="store_true", help="Machine-readable output.")

    models = commands.add_parser("list-models", help="List the models --model accepts.")
    models.add_argument(
        "kind",
        nargs="?",
        choices=[ListedModel.LOCAL, ListedModel.CLOUD],
        help="Only installed local models, or only cloud ones (default: both).",
    )
    models.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the ``kodo-harbor`` CLI.

    Args:
        argv (list[str] | None): Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: The exit code.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.llama_url is not None:
        given = [
            flag
            for flag, value in (
                ("--llama-port", args.llama_port is not None),
                ("--llama-bind", args.llama_bind is not None),
                ("--keep-llama", args.keep_llama),
            )
            if value
        ]
        if given:
            parser.error(f"--llama-url cannot be combined with {', '.join(given)}")
    catalog = SuiteCatalog.for_user()
    try:
        if args.command == "list-suites":
            return _list_suites(catalog, args.json)
        if args.command == "list-tasks":
            return _list_tasks(args, catalog)
        if args.command == "list-models":
            return _list_models(args.kind, args.json)
        if args.command == "summarize":
            return _summarize(args.job_dir, args.json)
        _interrupt_on_break()
        return _run(args, catalog)
    except HarborRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _STARTUP_FAILURE


def _interrupt_on_break() -> None:
    # Windows has no SIGINT for one process group: a parent (the Kodo
    # server's Stop, doc/BENCHMARK_SESSIONS.md §4.4) sends CTRL_BREAK_EVENT to
    # this run's own group, and it must wind down exactly as Ctrl+C does.
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, _raise_interrupt)


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _interrupt_on_stdin_close() -> None:
    # Blocks until the parent closes our stdin, then interrupts this whole
    # process group — this run, uv and Harbor — exactly as Ctrl+C would. The
    # raw descriptor, not sys.stdin: a daemon thread blocked inside the
    # buffered reader holds its lock, and Python aborts at exit over it.
    try:
        while os.read(sys.stdin.fileno(), 4096):
            pass
    except (OSError, ValueError):
        pass
    if sys.platform == "win32":
        # Every process on this (hidden) console; each maps it to Ctrl+C.
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, 0)
    else:
        os.killpg(os.getpgrp(), signal.SIGINT)


def _list_suites(catalog: SuiteCatalog, as_json: bool) -> int:
    suites = catalog.all()
    if as_json:
        rows = [{"name": s.name, "description": s.description, "path": str(s.path)} for s in suites]
        print(json.dumps(rows, indent=2))
        return 0
    for suite in suites:
        print(f"{suite.name}\t{suite.description}")
    return 0


def _list_tasks(args: argparse.Namespace, catalog: SuiteCatalog) -> int:
    if not args.suite and not args.dataset:
        raise HarborRunError("list-tasks needs at least one --suite or --dataset")
    selection = Selection()
    for name in args.suite:
        suite = catalog.find(name)
        selection.add_entries(suite.datasets, suite.tasks, f"suite {suite.name}")
    for reference in args.dataset:
        selection.add_dataset(reference)
    lister = TaskLister(
        find_uv(kodo_user_dir()),
        lambda reference: print(f"kodo-harbor: downloading {reference}", file=sys.stderr),
    )
    try:
        groups = lister.collect(selection)
    except KeyboardInterrupt:
        return _INTERRUPTED
    if args.json:
        print(json.dumps([group.to_dict() for group in groups], indent=2))
        return 0
    for index, group in enumerate(groups):
        if index:
            print()
        _print_group(group)
    return 0


def _print_group(group: TaskGroup) -> None:
    names = group.names
    print(f"{group.source} ({len(names)} task{'' if len(names) == 1 else 's'})")
    for name in names:
        print(f"  {name}")


def _list_models(kind: str | None, as_json: bool) -> int:
    rows: list[ListedModel] = []
    if kind != ListedModel.CLOUD:
        rows.extend(ListedModel.local(kodo_user_dir()))
    if kind != ListedModel.LOCAL:
        rows.extend(ListedModel.cloud(os.environ))
    if as_json:
        print(json.dumps([row.to_dict() for row in rows], indent=2))
        return 0
    if kind != ListedModel.CLOUD and not any(row.kind == ListedModel.LOCAL for row in rows):
        print("(no local model is installed; install one in the Kodo extension)", file=sys.stderr)
    width = max((len(row.model) for row in rows), default=0)
    for row in rows:
        status = {None: "installed", True: "key set", False: "no key"}[row.credential]
        print(f"{row.model:<{width}}  {status:<9}  {row.description}")
    return 0


def _summarize(job_dir: Path, as_json: bool) -> int:
    if not job_dir.is_dir():
        raise HarborRunError(f"No job directory at {job_dir}")
    summary = JobSummary.load(job_dir)
    _write_summary(job_dir, summary)
    print(json.dumps(summary.to_dict(), indent=2) if as_json else summary.to_markdown())
    return 0


def _run(args: argparse.Namespace, catalog: SuiteCatalog) -> int:
    if args.stop_on_stdin_close:
        threading.Thread(target=_interrupt_on_stdin_close, daemon=True).start()
    kodo_dir = kodo_user_dir()
    model = BenchModel.resolve(args.model, os.environ, kodo_dir)
    if args.thinking_level is not None:
        model.check_thinking_level(args.thinking_level)
    selection = _selection(args, catalog)
    harbor_args = [a for a in args.harbor_args if a != "--"]
    job_name = args.job_name or _job_name(args.agent, model)
    jobs_dir = args.jobs_dir.expanduser().resolve()
    job_dir = jobs_dir / job_name
    if (job_dir / "config.json").exists():
        raise HarborRunError(f"Job {job_dir} already exists; pass another --job-name")
    attached: LlamaAccess | None = None
    if args.llama_url is not None:
        if not model.is_local:
            raise HarborRunError(f"--llama-url is for a local model, not {model.spec.label!r}")
        attached = LlamaAccess.attach(model.spec.name, args.llama_url, kodo_dir)
    platform = platform_overlay(args.platform) if args.platform is not None else None
    uv = find_uv(kodo_dir)
    if not args.dry_run:
        _put_on_path(check_docker())
    job_dir.mkdir(parents=True, exist_ok=True)
    overlays: list[Path] = []
    if platform is not None:
        overlay = job_dir / _PLATFORM_OVERLAY
        overlay.write_text(platform, encoding="utf-8")
        overlays.append(overlay)

    source = KodoSource.detect()
    install, harbor_kodo = _install_source(args, source, uv, job_dir)
    agents_dir = kodo_agents_dir() if not args.agent.startswith(_BUILTIN_PREFIX) else None
    if agents_dir is not None and not agents_dir.is_dir():
        raise HarborRunError(f"{args.agent!r} is not built in, and {agents_dir} does not exist")

    llama: HostLlama | None = None
    access = attached
    registry: Path | None = None
    port = args.llama_port or _DEFAULT_LLAMA_PORT
    bind = str(args.llama_bind or HostLlama.default_bind())
    if model.is_local:
        if attached is None:
            llama = HostLlama(model.spec.name, port, bind)
        registry_path = kodo_dir / "etc" / "local-llm-registry.json"
        registry = registry_path if registry_path.is_file() else None
        text = HostLlama.compose_overlay()
        if text is not None:
            overlay = job_dir / _COMPOSE_OVERLAY
            overlay.write_text(text, encoding="utf-8")
            overlays.append(overlay)
    try:
        if llama is not None:
            # A dry run starts nothing, so the served context is not known yet.
            access = (
                LlamaAccess(model.spec.name, f"http://{bind}:{port}", 0)
                if args.dry_run
                else llama.start()
            )
        plan = JobPlan(
            job_name=job_name,
            jobs_dir=jobs_dir,
            model=model,
            agent=args.agent,
            selection=selection,
            controls=args.control,
            attempts=args.attempts,
            concurrency=args.concurrency or (1 if model.is_local else 4),
            max_retries=args.max_retries,
            thinking_level=args.thinking_level,
            turn_timeout=args.timeout,
            install=install,
            agents_dir=agents_dir,
            llama=access,
            registry_file=registry,
            compose_overlays=overlays,
        )
        config_path = job_dir / _JOB_CONFIG
        config_path.write_text(json.dumps(plan.to_config(), indent=2), encoding="utf-8")
        invocation = HarborInvocation(uv, config_path, harbor_kodo, harbor_args)
        thinking = args.thinking_level or model.default_thinking_tier
        tier = f" · thinking {thinking}" if thinking else ""
        print(
            f"kodo-harbor: {model.spec.label} · {args.agent}{tier} · {', '.join(selection.labels)}"
        )
        print(f"kodo-harbor: job config {config_path}")
        if args.dry_run:
            print(" ".join(invocation.command()))
            return 0
        try:
            code = invocation.run()
        except KeyboardInterrupt:
            # Harbor got the same Ctrl+C and wound its trials down; still stop
            # the llama-server and report what finished.
            code = _INTERRUPTED
    finally:
        if llama is not None and not args.keep_llama:
            llama.stop()
    summary = JobSummary.load(job_dir)
    _write_summary(job_dir, summary)
    print(summary.to_markdown())
    print(f"kodo-harbor: summary {job_dir / _SUMMARY_MD}")
    return code


def _put_on_path(docker: Path) -> None:
    """Let Harbor, which runs ``docker`` (and Docker its credential helpers,
    which sit beside it) by name, find a docker :func:`check_docker` found off
    ``PATH``."""
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    if str(docker.parent) not in entries:
        os.environ["PATH"] = os.pathsep.join([str(docker.parent), *entries])


def _selection(args: argparse.Namespace, catalog: SuiteCatalog) -> Selection:
    selection = Selection()
    for reference in args.dataset:
        selection.add_dataset(reference)
    for reference in args.task:
        selection.add_task(reference)
    for path in args.path:
        selection.add_path(path)
    for name in args.suite:
        suite = catalog.find(name)
        selection.add_entries(suite.datasets, suite.tasks, f"suite {suite.name}")
    selection.apply_filters(args.include, args.exclude, args.n_tasks)
    return selection


def _install_source(
    args: argparse.Namespace, source: KodoSource, uv: Path, job_dir: Path
) -> tuple[KodoInstall, str]:
    """Where the containers get py-kodo, and what Harbor's own process gets."""
    if args.kodo_wheel is not None:
        wheel = args.kodo_wheel.expanduser().resolve()
        if not wheel.is_file():
            raise HarborRunError(f"--kodo-wheel {wheel}: no such file")
        return KodoInstall(wheel=wheel), str(wheel)
    if args.kodo_version is not None:
        return KodoInstall(version=args.kodo_version), f"py-kodo=={args.kodo_version}"
    if source.editable_root is not None:
        print(f"kodo-harbor: building a py-kodo wheel from {source.editable_root}")
        wheel = source.build_wheel(uv, job_dir)
        return KodoInstall(wheel=wheel), str(wheel)
    return KodoInstall(), f"py-kodo=={source.version}"


def _job_name(agent: str, model: BenchModel) -> str:
    slug = re.sub(r"[^a-zA-Z0-9.-]+", "-", model.spec.label).strip("-")
    stamp = datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
    return f"{agent}__{slug}__{stamp}"


def _write_summary(job_dir: Path, summary: JobSummary) -> None:
    (job_dir / _SUMMARY_JSON).write_text(json.dumps(summary.to_dict(), indent=2), "utf-8")
    (job_dir / _SUMMARY_MD).write_text(summary.to_markdown(), "utf-8")
