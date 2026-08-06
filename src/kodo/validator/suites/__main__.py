"""``python -m kodo.validator.suites`` — the ``hatch run validate-suite`` entry point.

Usage::

    hatch run validate-suite <selector> [<selector> ...]
    hatch run validate-suite all

Each *selector* is ``all``, a suite (e.g. ``full_regression``), or a
sub-directory whose suites are all included; see :mod:`kodo.validator.suites`.
Unlike ``hatch run validate`` (a single scenario/batch against one
externally-named LLM, see :mod:`kodo.validator.scenarios.__main__`), a suite
already carries every LLM-under-test (+ flavor) and judge it needs — no
``--llm-under-test``/``--validation-llm`` flags here.

The runner:

1. **resolves every selector first** into the full batch of suites;
2. **verifies every suite's LUTs + judge are already installed** in the
   template home (``~/.kodo`` by default) — a pure disk check that **fails
   fast and never downloads** (per the project decision, same as the
   scenario runner); and only then
3. runs each suite in turn (:func:`~kodo.validator.run_suite`) — every entry
   in its own isolated home/server, then one final judge round comparing
   every entry — writing artifacts under ``--out``
   (``~/.kodo-validation/runs`` by default).

Exit code 0 iff every entry of every suite completed with no ``error``-phase
turn.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from kodo.validator import missing_local_llms
from kodo.validator._suite import SuiteResult, ValidationSuite, run_suite

from . import ALL, SuiteResolutionError, resolve_selectors, suite_ids

_log = logging.getLogger(__name__)

_DEFAULT_OUT = Path.home() / ".kodo-validation" / "runs"


def main(argv: list[str] | None = None) -> int:
    """Resolve selectors, verify models, run the batch, and print results.

    Args:
        argv (list[str] | None): CLI args; defaults to ``sys.argv[1:]``.

    Returns:
        int: Process exit code (0 = every entry ran with no error turn;
            2 = usage/resolution/pre-flight failure; 1 = a suite failed).
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        resolved = resolve_selectors(args.selectors)
    except SuiteResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_available()
        return 2
    if not resolved:
        print("Nothing to run: no suites matched.", file=sys.stderr)
        _print_available()
        return 2

    print(f"Selected {len(resolved)} suite(s):")
    for dotted_id, suite in resolved:
        noun = "entry" if len(suite.entries) == 1 else "entries"
        print(f"  - {dotted_id}  ({len(suite.entries)} {noun}, judge={suite.judge_llm})")

    template_home = _resolve_template_home(args.template_home)
    if template_home is None:
        print(
            "error: no template home found (~/.kodo does not exist). Pass "
            "--template-home DIR pointing at a .kodo with the required models installed.",
            file=sys.stderr,
        )
        return 2

    suites = [suite for _, suite in resolved]
    _note_missing_models(suites, template_home)

    out_dir = args.out.resolve()
    results = asyncio.run(_run_all(suites, out_dir, template_home))

    failed = 0
    for result in results:
        entry_ok = all(
            bool(e.result.turns) and all(t.final_phase != "error" for t in e.result.turns)
            for e in result.entries
        )
        failed += 0 if entry_ok else 1
        print(
            f"[{'ok' if entry_ok else 'FAILED'}] {result.suite.name}: "
            f"entries={len(result.entries)} artifacts={result.run_dir}"
        )
        for e in result.entries:
            print(
                f"    - {e.llm_under_test.llm} (flavor: {e.llm_under_test.flavor}) "
                f"/ {e.result.scenario.name}: score={e.result.score}"
            )
    return 1 if failed else 0


def _note_missing_models(suites: list[ValidationSuite], template_home: Path) -> None:
    """Log which LUT/judge models will be downloaded during the run (no fail).

    Args:
        suites (list[ValidationSuite]): The resolved batch.
        template_home (Path): The ``.kodo`` used as the clone template.
    """
    required = sorted(
        {
            name
            for suite in suites
            for name in (suite.judge_llm, *(e.llm_under_test.llm for e in suite.entries))
        }
    )
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
    suites: list[ValidationSuite], out_dir: Path, template_home: Path | None
) -> list[SuiteResult]:
    """Run suites sequentially.

    Args:
        suites (list[ValidationSuite]): Suites to execute, in order.
        out_dir (Path): Parent artifact directory.
        template_home (Path | None): ``.kodo`` template to clone per entry.

    Returns:
        list[SuiteResult]: One result per suite.
    """
    results: list[SuiteResult] = []
    for suite in suites:
        _log.info("Running suite %s", suite.name)
        results.append(await run_suite(suite, out_dir, template_home=template_home))
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
    ids = suite_ids()
    print(f"\nAvailable suites ('{ALL}' selects them all):", file=sys.stderr)
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
        prog="hatch run validate-suite",
        description="Run named kodo validation suites (see kodo.validator.suites).",
    )
    parser.add_argument(
        "selectors",
        nargs="*",
        metavar="SELECTOR",
        help="Suite id (e.g. full_regression), a submodule (all suites under "
        "it), or 'all'.",
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
        help="List available suites and exit.",
    )
    args = parser.parse_args(argv)
    if args.list:
        _print_available()
        raise SystemExit(0)
    if not args.selectors:
        parser.error("give at least one SELECTOR (or 'all'); use --list to see them")
    return args


if __name__ == "__main__":
    sys.exit(main())
