"""Entry point for ``python -m kodo.validator`` — run validation scenarios.

Two ways to describe what to run:

* **Scenario file** — ``--scenario path/to/file.py``: a plain Python file
  defining ``SCENARIO`` (a :class:`~kodo.validator.Scenario`) or
  ``SCENARIOS`` (a list of them). This is the intended form once real
  validation suites exist, since it can carry scripted users and seeds.
* **Inline flags** — ``--prompt``/``--root``/``--workflow``/… for a quick
  ad-hoc run without writing a file.

Every run gets an isolated kodo home cloned from ``--template-home``
(``bin/`` and ``llama.cpp/`` symlinked, per-run state fresh, the rest
copied), its own workspace directories, and a ``transcript.jsonl`` +
``summary.json`` under ``--out``. Scoring is phase 2: results print with
``score=None`` for now.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import cast

from ._harness import Modes
from ._scenario import RootSpec, Scenario, ScenarioResult, run_scenario

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the requested scenarios, and print results.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 = every scenario ran to completion).
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    scenarios = _resolve_scenarios(args)
    if not scenarios:
        print("Nothing to run: give --scenario FILE or at least one --prompt.", file=sys.stderr)
        return 2

    template_home = _resolve_template_home(args.template_home)
    out_dir = Path(args.out).resolve()

    results = asyncio.run(_run_all(scenarios, out_dir, template_home))
    failed = 0
    for result in results:
        phases = [t.final_phase for t in result.turns]
        ok = bool(result.turns) and all(p != "error" for p in phases)
        failed += 0 if ok else 1
        print(
            f"[{'ok' if ok else 'FAILED'}] {result.scenario.name}: "
            f"turns={phases} score={result.score} artifacts={result.run_dir}"
        )
    return 1 if failed else 0


async def _run_all(
    scenarios: list[Scenario], out_dir: Path, template_home: Path | None
) -> list[ScenarioResult]:
    """Run scenarios sequentially (each with its own server + home).

    Args:
        scenarios (list[Scenario]): Scenarios to execute, in order.
        out_dir (Path): Parent artifact directory.
        template_home (Path | None): ``.kodo`` template to clone per run.

    Returns:
        list[ScenarioResult]: One result per scenario.
    """
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        _log.info("Running scenario %s", scenario.name)
        results.append(await run_scenario(scenario, out_dir, template_home=template_home))
    return results


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and evaluate the argument parser.

    Args:
        argv (list[str] | None): CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="kodo-validator",
        description="Run automated validations of kodo's agentic workflows.",
    )
    parser.add_argument(
        "--scenario",
        type=Path,
        default=None,
        metavar="FILE",
        help="Python file defining SCENARIO (or SCENARIOS) to run.",
    )
    parser.add_argument(
        "--template-home",
        type=Path,
        default=None,
        metavar="DIR",
        help="Kodo home (.kodo directory) to clone for each run; "
        "defaults to ~/.kodo when it exists, else an empty home.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("kodo-validator-runs"),
        metavar="DIR",
        help="Parent directory for run artifacts (default: ./kodo-validator-runs).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Validator log level (default: INFO).",
    )
    # Inline (ad-hoc) scenario flags:
    parser.add_argument("--name", default="adhoc", help="Inline scenario name.")
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        metavar="TEXT",
        help="Prompt to submit (repeatable; one turn each).",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="NAME[=SEED_PATH]",
        help="Simulated workspace folder, optionally seeded from a path "
        "(repeatable; several roots = multi-root workspace).",
    )
    parser.add_argument(
        "--workflow",
        default="problem_solving",
        choices=["guided", "problem_solving"],
        help="Workflow mode (default: problem_solving).",
    )
    parser.add_argument(
        "--autonomous", action="store_true", help="Run in Autonomous mode (default: Interactive)."
    )
    parser.add_argument(
        "--edit-control",
        default="smart",
        choices=["review_all", "allow_all", "smart"],
        help="Edit Control posture (default: smart).",
    )
    parser.add_argument(
        "--command-control",
        default="smart",
        choices=["defensive", "permissive", "smart"],
        help="Command Control posture (default: smart).",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        metavar="NAME",
        help="Root to bind as the Guided-mode project (guided workflow only).",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=900.0,
        metavar="SECONDS",
        help="Per-prompt turn timeout (default: 900).",
    )
    return parser.parse_args(argv)


def _resolve_scenarios(args: argparse.Namespace) -> list[Scenario]:
    """Assemble the scenario list from a file and/or inline flags.

    Args:
        args (argparse.Namespace): Parsed CLI arguments.

    Returns:
        list[Scenario]: Scenarios to run (may be empty).
    """
    if args.scenario is not None:
        return _load_scenario_file(Path(args.scenario))
    if not args.prompt:
        return []
    roots = [_parse_root(spec) for spec in cast(list[str], args.root)]
    modes = Modes(
        autonomous=bool(args.autonomous),
        workflow=args.workflow,
        edit_control=args.edit_control,
        command_control=args.command_control,
    )
    return [
        Scenario(
            name=str(args.name),
            prompts=list(args.prompt),
            roots=roots,
            modes=modes,
            project_root=args.project_root,
            turn_timeout=float(args.turn_timeout),
        )
    ]


def _parse_root(spec: str) -> RootSpec:
    """Parse one ``--root NAME[=SEED_PATH]`` flag.

    Args:
        spec (str): The raw flag value.

    Returns:
        RootSpec: The parsed root spec.
    """
    name, sep, seed = spec.partition("=")
    return RootSpec(name=name, seed_from=Path(seed).resolve() if sep else None)


def _load_scenario_file(path: Path) -> list[Scenario]:
    """Import a scenario file and collect its SCENARIO/SCENARIOS.

    Args:
        path (Path): Python file defining ``SCENARIO`` or ``SCENARIOS``.

    Returns:
        list[Scenario]: The declared scenarios.

    Raises:
        SystemExit: If the file cannot be imported or declares neither name.
    """
    spec = importlib.util.spec_from_file_location(f"kodo_validator_scenario_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot import scenario file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "SCENARIOS"):
        return list(cast(list[Scenario], module.SCENARIOS))
    if hasattr(module, "SCENARIO"):
        return [cast(Scenario, module.SCENARIO)]
    raise SystemExit(f"{path} defines neither SCENARIO nor SCENARIOS")


def _resolve_template_home(explicit: Path | None) -> Path | None:
    """Pick the template home: explicit flag, else ``~/.kodo`` when present.

    Args:
        explicit (Path | None): The ``--template-home`` value, if given.

    Returns:
        Path | None: The template ``.kodo`` directory, or None for an empty home.
    """
    if explicit is not None:
        return explicit
    default = Path.home() / ".kodo"
    return default if default.is_dir() else None


if __name__ == "__main__":
    sys.exit(main())
