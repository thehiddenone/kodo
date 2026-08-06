"""``python -m kodo.validator.scenarios`` — the ``hatch run validate`` entry point.

Usage::

    hatch run validate <selector> [<selector> ...] --llm-under-test NAME --validation-llm NAME
    hatch run validate all --llm-under-test NAME --validation-llm NAME

Each *selector* is ``all``, a scenario (``tictactoe_detailed_task``), or a
sub-directory whose scenarios are all included; see
:mod:`kodo.validator.scenarios`. Scenarios are content-only (no LLM named on
the file itself, see :mod:`kodo.validator._scenario`), so every scenario this
batch resolves runs against the **same** ``--llm-under-test``/
``--validation-llm``/``--flavor``. To validate several LLMs together (each
potentially with different scenarios/flavors) and get one final comparative
summary, use a suite instead — see ``python -m kodo.validator.suites``.

The runner:

1. **resolves every selector first** into the full batch of scenarios;
2. **verifies the LUT/VLLM are already installed** in the template home
   (``~/.kodo`` by default) — a pure disk check that **fails fast and never
   downloads** (per the project decision); and only then
3. runs each scenario in its own isolated home/server, writing artifacts under
   ``--out`` (``~/.kodo-validation/runs`` by default).

Exit code 0 iff every scenario completed with no ``error``-phase turn.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from kodo.validator import missing_local_llms
from kodo.validator._scenario import Scenario, ScenarioResult, run_scenario

from . import ALL, ScenarioResolutionError, resolve_selectors, scenario_ids

_log = logging.getLogger(__name__)

_DEFAULT_OUT = Path.home() / ".kodo-validation" / "runs"


def main(argv: list[str] | None = None) -> int:
    """Resolve selectors, verify models, run the batch, and print results.

    Args:
        argv (list[str] | None): CLI args; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 = every scenario ran with no error turn;
            2 = usage/resolution/pre-flight failure; 1 = a scenario failed).
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        resolved = resolve_selectors(args.selectors)
    except ScenarioResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_available()
        return 2
    if not resolved:
        print("Nothing to run: no scenarios matched.", file=sys.stderr)
        _print_available()
        return 2

    print(
        f"Selected {len(resolved)} scenario(s) "
        f"(LUT={args.llm_under_test}, flavor={args.flavor}, VLLM={args.validation_llm}):"
    )
    for dotted_id, _scenario in resolved:
        print(f"  - {dotted_id}")

    template_home = _resolve_template_home(args.template_home)
    if template_home is None:
        print(
            "error: no template home found (~/.kodo does not exist). Pass "
            "--template-home DIR pointing at a .kodo with the required models installed.",
            file=sys.stderr,
        )
        return 2

    scenarios = [scenario for _, scenario in resolved]
    _note_missing_models(args.llm_under_test, args.validation_llm, template_home)

    out_dir = args.out.resolve()
    results = asyncio.run(
        _run_all(
            scenarios,
            out_dir,
            template_home,
            llm_under_test=args.llm_under_test,
            validation_llm=args.validation_llm,
            flavor=args.flavor,
            validation_llm_flavor=args.validation_llm_flavor,
        )
    )

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


def _note_missing_models(llm_under_test: str, validation_llm: str, template_home: Path) -> None:
    """Log which LUT/VLLM models will be downloaded during the run (no fail).

    The batch's models are checked against *template_home* with a pure disk
    read; any that are absent are **not** an error — the per-run harness
    downloads them into the global ``~/.kodo`` (through the clone's
    ``llama.cpp`` symlink) before it prompts. This just surfaces the pending
    downloads up front.

    Args:
        llm_under_test (str): The LLM every scenario in the batch runs against.
        validation_llm (str): The judge/proxy LLM.
        template_home (Path): The ``.kodo`` used as the clone template.
    """
    required = sorted({llm_under_test, validation_llm})
    missing = missing_local_llms(template_home, required)
    if missing:
        _log.info(
            "%d of %d required local model(s) not yet installed; they will be downloaded "
            "into %s during the run: %s",
            len(missing),
            len(required),
            template_home,
            missing,
        )
    else:
        _log.info("All %d required local model(s) already installed.", len(required))


async def _run_all(
    scenarios: list[Scenario],
    out_dir: Path,
    template_home: Path | None,
    *,
    llm_under_test: str,
    validation_llm: str,
    flavor: str,
    validation_llm_flavor: str | None,
) -> list[ScenarioResult]:
    """Run scenarios sequentially, each in its own isolated home/server.

    Args:
        scenarios (list[Scenario]): Scenarios to execute, in order.
        out_dir (Path): Parent artifact directory.
        template_home (Path | None): ``.kodo`` template to clone per run.
        llm_under_test (str): Local registry name every scenario runs against.
        validation_llm (str): Local registry name of the judge/proxy LLM.
        flavor (str): ``llm_under_test`` flavor id.
        validation_llm_flavor (str | None): ``validation_llm`` flavor id.

    Returns:
        list[ScenarioResult]: One result per scenario.
    """
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        _log.info("Running scenario %s", scenario.name)
        results.append(
            await run_scenario(
                scenario,
                out_dir,
                llm_under_test=llm_under_test,
                validation_llm=validation_llm,
                flavor=flavor,
                validation_llm_flavor=validation_llm_flavor,
                template_home=template_home,
            )
        )
    return results


def _resolve_template_home(explicit: Path | None) -> Path | None:
    """Pick the template home: explicit flag, else ``~/.kodo`` when present.

    Args:
        explicit (Path | None): The ``--template-home`` value, if given.

    Returns:
        Path | None: The template ``.kodo`` directory, or None when absent.
    """
    if explicit is not None:
        return explicit
    default = Path.home() / ".kodo"
    return default if default.is_dir() else None


def _print_available() -> None:
    """Print the available selectors to stderr (for error messages / --list)."""
    ids = scenario_ids()
    print(f"\nAvailable scenarios ('{ALL}' selects them all):", file=sys.stderr)
    for dotted_id in ids:
        print(f"  - {dotted_id}", file=sys.stderr)
    if not ids:
        print("  (none found)", file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and evaluate the argument parser.

    Args:
        argv (list[str] | None): CLI args; defaults to ``sys.argv[1:]``.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="hatch run validate",
        description="Run named kodo validation scenarios (see kodo.validator.scenarios).",
    )
    parser.add_argument(
        "selectors",
        nargs="*",
        metavar="SELECTOR",
        help="Scenario id (e.g. tictactoe_detailed_task), a submodule "
        "(all scenarios under a sub-directory), or 'all'.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        metavar="DIR",
        help=f"Parent directory for run artifacts (default: {_DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--template-home",
        type=Path,
        default=None,
        metavar="DIR",
        help="Kodo home (.kodo) to clone for each run; defaults to ~/.kodo.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Runner log level (default: INFO).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenarios and exit.",
    )
    parser.add_argument(
        "--llm-under-test",
        default=None,
        metavar="NAME",
        help="Local registry name every selected scenario runs against (mandatory).",
    )
    parser.add_argument(
        "--validation-llm",
        default=None,
        metavar="NAME",
        help="Local registry name of the fixed validation/judge LLM (mandatory).",
    )
    parser.add_argument(
        "--flavor",
        default="default",
        metavar="FLAVOR_ID",
        help="llm_under_test flavor id to pin before the first prompt (default: 'default').",
    )
    parser.add_argument(
        "--validation-llm-flavor",
        default=None,
        metavar="FLAVOR_ID",
        help="validation_llm flavor id to pin the same way (default: leave as-is).",
    )
    args = parser.parse_args(argv)
    if args.list:
        _print_available()
        raise SystemExit(0)
    if not args.selectors:
        parser.error("give at least one SELECTOR (or 'all'); use --list to see them")
    if not args.llm_under_test or not args.validation_llm:
        parser.error("--llm-under-test and --validation-llm are required")
    return args


if __name__ == "__main__":
    sys.exit(main())
