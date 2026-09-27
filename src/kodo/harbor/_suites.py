"""Named benchmark suites — a reusable, versioned list of Harbor datasets and tasks.

A suite is one JSON file (doc/HARBOR.md §3)::

    {
      "name": "terminal-bench-sample",
      "description": "The 10-task Terminal-Bench 2.0 sample.",
      "datasets": [{"name": "terminal-bench-sample", "version": "2.0"}],
      "tasks": [{"name": "org/task", "ref": "latest"}, {"path": "tasks/mine"}]
    }

``datasets`` / ``tasks`` entries are Harbor's own ``DatasetConfig`` /
``TaskConfig`` objects, restricted to the keys below; a relative ``path`` is
resolved against the suite file's directory. Suites ship in the package
(``kodo/harbor/suites/``) and are read from ``~/.kodo/harbor/suites/``; a
user suite with a built-in's name shadows it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from ._errors import HarborRunError

__all__ = ["BUILTIN_SUITES_DIR", "Suite", "SuiteCatalog"]

#: Where the suites that ship with kodo live.
BUILTIN_SUITES_DIR = Path(__file__).parent / "suites"

_DATASET_KEYS = frozenset(
    {"name", "version", "ref", "path", "task_names", "exclude_task_names", "n_tasks"}
)
_TASK_KEYS = frozenset({"name", "ref", "path"})
_SUITE_KEYS = frozenset({"name", "description", "datasets", "tasks"})


class Suite:
    """One suite: a name, a description, and Harbor entries."""

    __name: str
    __description: str
    __datasets: tuple[dict[str, object], ...]
    __tasks: tuple[dict[str, object], ...]
    __path: Path

    def __init__(
        self,
        name: str,
        description: str,
        datasets: list[dict[str, object]],
        tasks: list[dict[str, object]],
        path: Path,
    ) -> None:
        """Bind a parsed suite.

        Args:
            name (str): The suite's name.
            description (str): One line on what it measures.
            datasets (list[dict[str, object]]): Harbor ``DatasetConfig`` objects.
            tasks (list[dict[str, object]]): Harbor ``TaskConfig`` objects.
            path (Path): The file it was read from.
        """
        self.__name = name
        self.__description = description
        self.__datasets = tuple(datasets)
        self.__tasks = tuple(tasks)
        self.__path = path

    @classmethod
    def load(cls, path: Path) -> Suite:
        """Read and validate a suite file.

        Args:
            path (Path): The suite's JSON file.

        Returns:
            Suite: The suite.

        Raises:
            HarborRunError: The file is unreadable or not a valid suite.
        """
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarborRunError(f"Cannot read suite {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise HarborRunError(f"Suite {path}: expected a JSON object")
        data = cast(dict[str, object], raw)
        unknown = set(data) - _SUITE_KEYS
        if unknown:
            raise HarborRunError(f"Suite {path}: unknown keys {sorted(unknown)}")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise HarborRunError(f"Suite {path}: 'name' must be a non-empty string")
        datasets = _entries(path, data.get("datasets"), "datasets", _DATASET_KEYS)
        tasks = _entries(path, data.get("tasks"), "tasks", _TASK_KEYS)
        if not datasets and not tasks:
            raise HarborRunError(f"Suite {path}: lists no datasets and no tasks")
        return cls(name, str(data.get("description", "")), datasets, tasks, path)

    @property
    def name(self) -> str:
        """The suite's name."""
        return self.__name

    @property
    def description(self) -> str:
        """One line on what the suite measures."""
        return self.__description

    @property
    def datasets(self) -> list[dict[str, object]]:
        """Harbor ``datasets`` entries (copies)."""
        return [dict(entry) for entry in self.__datasets]

    @property
    def tasks(self) -> list[dict[str, object]]:
        """Harbor ``tasks`` entries (copies)."""
        return [dict(entry) for entry in self.__tasks]

    @property
    def path(self) -> Path:
        """The file the suite was read from."""
        return self.__path


class SuiteCatalog:
    """The built-in suites plus the user's, by name."""

    __dirs: tuple[Path, ...]

    def __init__(self, user_dir: Path, builtin_dir: Path = BUILTIN_SUITES_DIR) -> None:
        """Bind the directories suites are read from.

        Args:
            user_dir (Path): ``~/.kodo/harbor/suites`` (may not exist).
            builtin_dir (Path): The package's suite directory.
        """
        self.__dirs = (builtin_dir, user_dir)

    def all(self) -> list[Suite]:
        """Every suite, a user suite shadowing a built-in of the same name.

        Returns:
            list[Suite]: Suites sorted by name.

        Raises:
            HarborRunError: A suite file is invalid.
        """
        by_name: dict[str, Suite] = {}
        for directory in self.__dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                suite = Suite.load(path)
                by_name[suite.name] = suite
        return [by_name[name] for name in sorted(by_name)]

    def find(self, name_or_path: str) -> Suite:
        """Resolve ``--suite``: a suite file path, or a suite name.

        Args:
            name_or_path (str): A path to a ``.json`` file, or a suite name.

        Returns:
            Suite: The suite.

        Raises:
            HarborRunError: No such suite, or the file is invalid.
        """
        candidate = Path(name_or_path).expanduser()
        if candidate.suffix == ".json" or candidate.is_file():
            return Suite.load(candidate)
        suites = self.all()
        for suite in suites:
            if suite.name == name_or_path:
                return suite
        names = ", ".join(s.name for s in suites) or "none"
        raise HarborRunError(f"No suite named {name_or_path!r}; available: {names}")


def _entries(path: Path, raw: object, key: str, allowed: frozenset[str]) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HarborRunError(f"Suite {path}: '{key}' must be a list")
    entries: list[dict[str, object]] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise HarborRunError(f"Suite {path}: every '{key}' entry must be an object")
        entry = dict(cast(dict[str, object], item))
        unknown = set(entry) - allowed
        if unknown:
            raise HarborRunError(f"Suite {path}: unknown '{key}' keys {sorted(unknown)}")
        if ("name" in entry) == ("path" in entry):
            raise HarborRunError(f"Suite {path}: each '{key}' entry needs 'name' or 'path'")
        local = entry.get("path")
        if isinstance(local, str):
            entry["path"] = str((path.parent / local).resolve())
        entries.append(entry)
    return entries
