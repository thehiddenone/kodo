"""Which tasks a benchmark run covers, in Harbor's own job-config vocabulary.

A selection is a list of Harbor ``datasets`` and ``tasks`` entries — the same
objects ``harbor run -c job.json`` reads (``DatasetConfig`` / ``TaskConfig``),
kept as plain JSON so this package never imports Harbor:

- ``--dataset NAME[@VERSION]`` → a registry dataset (``terminal-bench@2.0``);
  ``ORG/NAME[@REF]`` is a Harbor package dataset.
- ``--task ORG/NAME[@REF]`` → one package task.
- ``--path DIR`` → a local task directory (it holds ``task.toml``) or a local
  dataset directory (it holds task directories).
- ``--suite NAME|FILE`` → a :class:`~._suites.Suite`, itself a named list of
  the above.

Filters (``--include`` / ``--exclude`` globs, ``--n-tasks``) apply to every
dataset entry; they map onto ``task_names`` / ``exclude_task_names`` /
``n_tasks``, which Harbor applies when it expands the dataset.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ._errors import HarborRunError

__all__ = ["Selection"]

_TASK_FILE = "task.toml"


class Selection:
    """An ordered set of Harbor ``datasets`` and ``tasks`` entries."""

    __datasets: list[dict[str, object]]
    __tasks: list[dict[str, object]]
    __labels: list[str]

    def __init__(self) -> None:
        """Start an empty selection."""
        self.__datasets = []
        self.__tasks = []
        self.__labels = []

    @property
    def datasets(self) -> list[dict[str, object]]:
        """Harbor ``datasets`` entries (copies)."""
        return [dict(entry) for entry in self.__datasets]

    @property
    def tasks(self) -> list[dict[str, object]]:
        """Harbor ``tasks`` entries (copies)."""
        return [dict(entry) for entry in self.__tasks]

    @property
    def labels(self) -> list[str]:
        """One human-readable label per source added, in order."""
        return list(self.__labels)

    @property
    def is_empty(self) -> bool:
        """Whether nothing has been selected."""
        return not self.__datasets and not self.__tasks

    def add_dataset(self, reference: str) -> None:
        """Add a registry or package dataset.

        Args:
            reference (str): ``NAME[@VERSION]`` or ``ORG/NAME[@REF]``.

        Raises:
            HarborRunError: *reference* is empty.
        """
        name, version = _split_at(reference)
        entry: dict[str, object] = {"name": name}
        if version is not None:
            entry["ref" if "/" in name else "version"] = version
        self.__datasets.append(entry)
        self.__labels.append(f"dataset {reference}")

    def add_task(self, reference: str) -> None:
        """Add one package task.

        Args:
            reference (str): ``ORG/NAME[@REF]``.

        Raises:
            HarborRunError: *reference* is not ``ORG/NAME``.
        """
        name, ref = _split_at(reference)
        if "/" not in name:
            raise HarborRunError(
                f"--task {reference!r}: a Harbor task is ORG/NAME[@REF]; "
                "use --path for a local task directory"
            )
        entry: dict[str, object] = {"name": name}
        if ref is not None:
            entry["ref"] = ref
        self.__tasks.append(entry)
        self.__labels.append(f"task {reference}")

    def add_path(self, path: Path) -> None:
        """Add a local task directory or a local dataset directory.

        Args:
            path (Path): The directory.

        Raises:
            HarborRunError: *path* is not a directory.
        """
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise HarborRunError(f"--path {path}: not a directory")
        if (resolved / _TASK_FILE).is_file():
            self.__tasks.append({"path": str(resolved)})
            self.__labels.append(f"task {resolved}")
        else:
            self.__datasets.append({"path": str(resolved)})
            self.__labels.append(f"dataset {resolved}")

    def add_entries(
        self, datasets: list[dict[str, object]], tasks: list[dict[str, object]], label: str
    ) -> None:
        """Add ready-made Harbor entries (a suite's).

        Args:
            datasets (list[dict[str, object]]): ``DatasetConfig`` objects.
            tasks (list[dict[str, object]]): ``TaskConfig`` objects.
            label (str): What they came from, for the run banner.
        """
        self.__datasets.extend(dict(entry) for entry in datasets)
        self.__tasks.extend(dict(entry) for entry in tasks)
        self.__labels.append(label)

    def apply_filters(self, include: list[str], exclude: list[str], n_tasks: int | None) -> None:
        """Narrow every dataset entry.

        ``include`` replaces a dataset's own ``task_names``; ``exclude`` is
        added to its ``exclude_task_names``; ``n_tasks`` replaces its cap.

        Args:
            include (list[str]): Task-name globs to keep (empty: no change).
            exclude (list[str]): Task-name globs to drop.
            n_tasks (int | None): Keep at most this many tasks per dataset.
        """
        for entry in self.__datasets:
            if include:
                entry["task_names"] = list(include)
            if exclude:
                existing = entry.get("exclude_task_names")
                merged = cast(list[str], existing) if isinstance(existing, list) else []
                entry["exclude_task_names"] = [*merged, *exclude]
            if n_tasks is not None:
                entry["n_tasks"] = n_tasks


def _split_at(reference: str) -> tuple[str, str | None]:
    name, sep, version = reference.strip().partition("@")
    if not name:
        raise HarborRunError(f"{reference!r} names no dataset or task")
    return name, (version or None) if sep else None
