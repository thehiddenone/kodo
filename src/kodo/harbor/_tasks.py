"""Which tasks a suite or dataset holds — ``kodo-harbor list-tasks``.

Harbor resolves a dataset's task names only when it expands the dataset at
run time, so the names ``--include`` / ``--exclude`` (and a suite's
``task_names``) match are not known up front. :class:`TaskLister` downloads
each registry or package dataset with the pinned Harbor::

    uv tool run --from harbor==<HARBOR_VERSION> harbor datasets download REF --export -o TMP

into a throwaway directory, reads the task directories
(``TMP/<dataset>/<task>/task.toml``), and deletes the download before it
returns, whether the listing succeeded or not. Export mode writes only under
``-o``; it does not fill Harbor's ``~/.cache/harbor/tasks``. A local ``path``
dataset is read in place, and a selection's single ``tasks`` entries are
listed by name, without downloading anything.

The listing is **every** task a dataset holds: an entry's ``task_names``,
``exclude_task_names`` and ``n_tasks`` are ignored.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ._errors import HarborRunError
from ._runner import HARBOR_VERSION
from ._selection import Selection

__all__ = ["TaskGroup", "TaskLister"]

_TASK_FILE = "task.toml"
_SINGLE_TASKS = "tasks"
_ERROR_TAIL_LINES = 20


class TaskGroup:
    """The task names one source holds: a dataset, or a selection's single tasks."""

    __source: str
    __names: tuple[str, ...]

    def __init__(self, source: str, names: list[str]) -> None:
        """Bind a listed source.

        Args:
            source (str): The dataset reference (``NAME[@VERSION]``,
                ``ORG/NAME[@REF]`` or a path), or ``tasks`` for single tasks.
            names (list[str]): The task names it holds.
        """
        self.__source = source
        self.__names = tuple(names)

    @property
    def source(self) -> str:
        """The dataset reference, or ``tasks`` for single tasks."""
        return self.__source

    @property
    def names(self) -> list[str]:
        """The task names, sorted."""
        return list(self.__names)

    def to_dict(self) -> dict[str, object]:
        """The group as JSON.

        Returns:
            dict[str, object]: ``{"source": ..., "tasks": [...]}``.
        """
        return {"source": self.__source, "tasks": list(self.__names)}


class TaskLister:
    """Lists the tasks of Harbor datasets, downloading each into a throwaway directory."""

    __uv: Path
    __announce: Callable[[str], None]

    def __init__(self, uv: Path, announce: Callable[[str], None]) -> None:
        """Bind how to run Harbor.

        Args:
            uv (Path): The uv executable.
            announce (Callable[[str], None]): Told the reference of each
                dataset before its download starts.
        """
        self.__uv = uv
        self.__announce = announce

    def download_command(self, reference: str, out_dir: Path) -> list[str]:
        """The command that downloads one dataset.

        Args:
            reference (str): ``NAME[@VERSION]`` or ``ORG/NAME[@REF]``.
            out_dir (Path): Where Harbor writes ``<dataset>/<task>/``.

        Returns:
            list[str]: argv.
        """
        return [
            str(self.__uv),
            "tool",
            "run",
            "--from",
            f"harbor=={HARBOR_VERSION}",
            "harbor",
            "datasets",
            "download",
            reference,
            "--export",
            "-o",
            str(out_dir),
        ]

    def collect(self, selection: Selection) -> list[TaskGroup]:
        """Every task each dataset in *selection* holds, plus its single tasks.

        Args:
            selection (Selection): The datasets and tasks to list.

        Returns:
            list[TaskGroup]: One group per dataset, in order, then one for the
            single tasks (if any).

        Raises:
            HarborRunError: A download failed, or wrote no task directories.
        """
        groups: list[TaskGroup] = []
        with tempfile.TemporaryDirectory(prefix="kodo-harbor-tasks-") as scratch:
            for index, entry in enumerate(selection.datasets):
                local = entry.get("path")
                if isinstance(local, str):
                    groups.append(TaskGroup(local, _task_names(Path(local), "*")))
                    continue
                reference = _reference(entry)
                out_dir = Path(scratch) / str(index)
                self.__download(reference, out_dir)
                names = _task_names(out_dir, "*/*")
                if not names:
                    raise HarborRunError(f"Harbor downloaded no tasks for dataset {reference}")
                groups.append(TaskGroup(reference, names))
        singles = [_single_task_name(entry) for entry in selection.tasks]
        if singles:
            groups.append(TaskGroup(_SINGLE_TASKS, sorted(singles)))
        return groups

    def __download(self, reference: str, out_dir: Path) -> None:
        self.__announce(reference)
        completed = subprocess.run(
            self.download_command(reference, out_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            output = f"{completed.stdout}\n{completed.stderr}".strip().splitlines()
            tail = "\n".join(output[-_ERROR_TAIL_LINES:])
            raise HarborRunError(f"Downloading dataset {reference} failed:\n{tail}")


def _reference(entry: dict[str, object]) -> str:
    name = str(entry["name"])
    version = entry.get("version", entry.get("ref"))
    return f"{name}@{version}" if version is not None else name


def _task_names(root: Path, depth: str) -> list[str]:
    return sorted({task_file.parent.name for task_file in root.glob(f"{depth}/{_TASK_FILE}")})


def _single_task_name(entry: dict[str, object]) -> str:
    local = entry.get("path")
    if isinstance(local, str):
        return Path(local).name
    return str(entry["name"])
