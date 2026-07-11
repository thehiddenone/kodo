"""Named validation scenarios and the selector resolver for ``hatch run validate``.

**One scenario == one ``.py`` file** under this package that defines a
module-level ``SCENARIO`` (a :class:`kodo.validator.Scenario`), with its PUT /
UPP / RVP inlined as triple-quoted strings so each file is self-contained. A
file may also define ``SCENARIOS`` (a list) if it carries several. Files can be
nested in sub-directories to group them — e.g. by the model under test:
``qwen35-9b/tictactoe_console.py``. Sub-directory names need not be valid Python
identifiers (they are loaded by file path, not imported), so ``qwen35-9b`` is
fine.

A command-line **selector** is a dotted path under this package:

* ``qwen35-9b.tictactoe_console`` → the one scenario in
  ``qwen35-9b/tictactoe_console.py``;
* ``qwen35-9b`` → every scenario file under ``qwen35-9b/`` (a "submodule");
* ``all`` → every scenario file anywhere under this package.

:func:`resolve_selectors` turns a list of selectors into an ordered,
de-duplicated list of ``(dotted_id, Scenario)`` pairs. The runner
(:mod:`kodo.validator.scenarios.__main__`) resolves **all** selectors first,
then verifies every model is installed, then runs — nothing starts until the
whole batch is known and its models are present.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from kodo.validator._scenario import Scenario

__all__ = ["ALL", "ScenarioResolutionError", "resolve_selectors", "scenario_ids"]

# The selector that expands to every scenario in the package.
ALL = "all"

# The directory this package lives in — the root of the scenario tree.
_SCENARIOS_DIR = Path(__file__).resolve().parent


class ScenarioResolutionError(ValueError):
    """A selector matched no scenario file/sub-module, or a file was malformed."""


def _is_scenario_file(path: Path) -> bool:
    """True if *path* is a scenario module (a public ``.py``, not dunder/private).

    Args:
        path (Path): Candidate file.

    Returns:
        bool: Whether it should be treated as a scenario file.
    """
    return (
        path.suffix == ".py"
        and not path.name.startswith("_")
        and path.name not in {"__init__.py", "__main__.py"}
    )


def _dotted_id(path: Path) -> str:
    """The selector that names *path* (its package-relative path, dotted).

    Args:
        path (Path): A scenario file under this package.

    Returns:
        str: e.g. ``qwen35-9b.tictactoe_console`` for
        ``qwen35-9b/tictactoe_console.py``.
    """
    return ".".join(path.relative_to(_SCENARIOS_DIR).with_suffix("").parts)


def _all_scenario_files() -> list[Path]:
    """Every scenario file anywhere under the package, sorted for stable order."""
    return sorted(p for p in _SCENARIOS_DIR.rglob("*.py") if _is_scenario_file(p))


def _files_for_selector(selector: str) -> list[Path]:
    """Resolve one selector to the scenario files it names.

    Args:
        selector (str): ``all``, a dotted scenario path, or a dotted
            sub-directory ("submodule").

    Returns:
        list[Path]: Matching scenario files (a single file, or every file under
            a directory), sorted.

    Raises:
        ScenarioResolutionError: If *selector* names neither a file nor a
            directory under the package.
    """
    if selector == ALL:
        return _all_scenario_files()
    parts = selector.split(".")
    base = _SCENARIOS_DIR.joinpath(*parts)
    as_file = base.with_suffix(".py")
    if as_file.is_file():
        return [as_file]
    if base.is_dir():
        return sorted(p for p in base.rglob("*.py") if _is_scenario_file(p))
    raise ScenarioResolutionError(
        f"Unknown scenario or submodule: {selector!r} "
        f"(looked for {as_file.relative_to(_SCENARIOS_DIR)} or a "
        f"{'/'.join(parts)}/ directory under the scenarios package)"
    )


def _load_scenarios(path: Path) -> list[Scenario]:
    """Import a scenario file and collect its ``SCENARIO``/``SCENARIOS``.

    Args:
        path (Path): The scenario file to load.

    Returns:
        list[Scenario]: The scenarios it declares.

    Raises:
        ScenarioResolutionError: If it cannot be imported or declares neither.
    """
    mod_name = f"kodo_validator_scenario_{_dotted_id(path).replace('.', '_').replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ScenarioResolutionError(f"Cannot import scenario file: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surface any load error as a resolution error
        raise ScenarioResolutionError(f"Failed to load scenario {path}: {exc}") from exc
    if hasattr(module, "SCENARIOS"):
        return list(cast("list[Scenario]", module.SCENARIOS))
    if hasattr(module, "SCENARIO"):
        return [cast(Scenario, module.SCENARIO)]
    raise ScenarioResolutionError(f"{path} defines neither SCENARIO nor SCENARIOS")


def resolve_selectors(selectors: Sequence[str]) -> list[tuple[str, Scenario]]:
    """Resolve selectors to an ordered, de-duplicated ``(id, Scenario)`` list.

    A scenario file selected by more than one selector (e.g. both ``all`` and
    its own name) runs once; the first selector to reach it wins its position.

    Args:
        selectors (Sequence[str]): CLI selectors (``all`` / scenario / submodule).

    Returns:
        list[tuple[str, Scenario]]: Each scenario's dotted id and object.

    Raises:
        ScenarioResolutionError: If any selector matches nothing.
    """
    resolved: list[tuple[str, Scenario]] = []
    seen: set[Path] = set()
    for selector in selectors:
        files = _files_for_selector(selector)
        if not files:
            raise ScenarioResolutionError(f"No scenarios match selector {selector!r}")
        for path in files:
            if path in seen:
                continue
            seen.add(path)
            for scenario in _load_scenarios(path):
                resolved.append((_dotted_id(path), scenario))
    return resolved


def scenario_ids() -> list[str]:
    """Every available scenario's dotted id (for help / listing)."""
    return [_dotted_id(p) for p in _all_scenario_files()]
