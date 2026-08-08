"""Named validation suites and the selector resolver for ``hatch run validate-suite``.

**One suite == one ``.py`` file** under this package that defines a
module-level ``SUITE`` (a :class:`kodo.validator.ValidationSuite`); a file may
also define ``SUITES`` (a list) if it carries several. A suite file is the
*wiring* — which :class:`~kodo.validator.LLMUnderTest` (LLM + knobs) pairs
with which :class:`~kodo.validator.Scenario`, and which model judges the
batch — built by importing scenario content from
:mod:`kodo.validator.scenarios` (typically via
:func:`kodo.validator.scenarios.resolve_selectors`, since scenario files are
loaded by path rather than import — see that package's docstring for why).

A command-line **selector** is a dotted path under this package, resolved the
same way :mod:`kodo.validator.scenarios` resolves scenario selectors:

* ``full_regression`` → the one suite in ``full_regression.py``;
* a bare sub-directory name → every suite file under it;
* ``all`` → every suite file anywhere under this package.

:func:`resolve_selectors` turns a list of selectors into an ordered,
de-duplicated list of ``(dotted_id, ValidationSuite)`` pairs. The runner
(:mod:`kodo.validator.suites.__main__`) resolves **all** selectors first,
then runs each suite in turn.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from kodo.validator._suite import ValidationSuite

__all__ = ["ALL", "SuiteResolutionError", "resolve_selectors", "suite_ids"]

# The selector that expands to every suite in the package.
ALL = "all"

# The directory this package lives in — the root of the suite tree.
_SUITES_DIR = Path(__file__).resolve().parent


class SuiteResolutionError(ValueError):
    """A selector matched no suite file/sub-module, or a file was malformed."""


def _is_suite_file(path: Path) -> bool:
    """True if *path* is a suite module (a public ``.py``, not dunder/private).

    Args:
        path (Path): Candidate file.

    Returns:
        bool: Whether it should be treated as a suite file.
    """
    return (
        path.suffix == ".py"
        and not path.name.startswith("_")
        and path.name not in {"__init__.py", "__main__.py"}
    )


def _dotted_id(path: Path) -> str:
    """The selector that names *path* (its package-relative path, dotted).

    Args:
        path (Path): A suite file under this package.

    Returns:
        str: e.g. ``full_regression`` for ``full_regression.py``.
    """
    return ".".join(path.relative_to(_SUITES_DIR).with_suffix("").parts)


def _all_suite_files() -> list[Path]:
    """Every suite file anywhere under the package, sorted for stable order."""
    return sorted(p for p in _SUITES_DIR.rglob("*.py") if _is_suite_file(p))


def _files_for_selector(selector: str) -> list[Path]:
    """Resolve one selector to the suite files it names.

    Args:
        selector (str): ``all``, a dotted suite path, or a dotted
            sub-directory ("submodule").

    Returns:
        list[Path]: Matching suite files (a single file, or every file under
            a directory), sorted.

    Raises:
        SuiteResolutionError: If *selector* names neither a file nor a
            directory under the package.
    """
    if selector == ALL:
        return _all_suite_files()
    parts = selector.split(".")
    base = _SUITES_DIR.joinpath(*parts)
    as_file = base.with_suffix(".py")
    if as_file.is_file():
        return [as_file]
    if base.is_dir():
        return sorted(p for p in base.rglob("*.py") if _is_suite_file(p))
    raise SuiteResolutionError(
        f"Unknown suite or submodule: {selector!r} "
        f"(looked for {as_file.relative_to(_SUITES_DIR)} or a "
        f"{'/'.join(parts)}/ directory under the suites package)"
    )


def _load_suites(path: Path) -> list[ValidationSuite]:
    """Import a suite file and collect its ``SUITE``/``SUITES``.

    Args:
        path (Path): The suite file to load.

    Returns:
        list[ValidationSuite]: The suites it declares.

    Raises:
        SuiteResolutionError: If it cannot be imported or declares neither.
    """
    mod_name = f"kodo_validator_suite_{_dotted_id(path).replace('.', '_').replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise SuiteResolutionError(f"Cannot import suite file: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface any load error as a resolution error
        raise SuiteResolutionError(f"Failed to load suite {path}: {exc}") from exc
    if hasattr(module, "SUITES"):
        return list(cast("list[ValidationSuite]", module.SUITES))
    if hasattr(module, "SUITE"):
        return [cast(ValidationSuite, module.SUITE)]
    raise SuiteResolutionError(f"{path} defines neither SUITE nor SUITES")


def resolve_selectors(selectors: Sequence[str]) -> list[tuple[str, ValidationSuite]]:
    """Resolve selectors to an ordered, de-duplicated ``(id, ValidationSuite)`` list.

    A suite file selected by more than one selector (e.g. both ``all`` and its
    own name) runs once; the first selector to reach it wins its position.

    Args:
        selectors (Sequence[str]): CLI selectors (``all`` / suite / submodule).

    Returns:
        list[tuple[str, ValidationSuite]]: Each suite's dotted id and object.

    Raises:
        SuiteResolutionError: If any selector matches nothing.
    """
    resolved: list[tuple[str, ValidationSuite]] = []
    seen: set[Path] = set()
    for selector in selectors:
        files = _files_for_selector(selector)
        if not files:
            raise SuiteResolutionError(f"No suites match selector {selector!r}")
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            for suite in _load_suites(path):
                resolved.append((_dotted_id(path), suite))
    return resolved


def suite_ids() -> list[str]:
    """Every available suite's dotted id (for help / listing)."""
    return [_dotted_id(p) for p in _all_suite_files()]
