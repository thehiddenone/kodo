"""Running Harbor itself: pinned, isolated, and with *this* Kodo alongside.

Harbor is not a dependency of ``py-kodo``. ``kodo-harbor`` runs a pinned
Harbor through ``uv tool run`` in a throwaway environment that also holds the
same Kodo it was started from, so Harbor's process can import
``kodo.harbor.agent``::

    uv tool run --from harbor==<HARBOR_VERSION> --with <this kodo> harbor run -c <job.json>

"This Kodo" (:class:`KodoSource`) is the installed ``py-kodo`` release, or —
when running from an editable source checkout — a wheel built from that
checkout, because PyPI's release of the same version number is not the code
being measured. The same wheel is uploaded into every trial container, so the
host, Harbor's process and the containers all run identical Kodo.
"""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

from kodo.binutils import find_util

from ._errors import HarborRunError

__all__ = ["HARBOR_VERSION", "HarborInvocation", "KodoSource", "check_docker", "find_uv"]

#: The Harbor release kodo-harbor runs (and the adapter is tested against).
#: Keep in lockstep with the ``harbor==`` dev dependency in pyproject.toml.
HARBOR_VERSION = "0.23.0"

_DISTRIBUTION = "py-kodo"


class KodoSource:
    """The py-kodo this process runs: a release version, or an editable checkout."""

    __version: str
    __editable_root: Path | None

    def __init__(self, version: str, editable_root: Path | None = None) -> None:
        """Bind the source.

        Args:
            version (str): The installed py-kodo version.
            editable_root (Path | None): The checkout of an editable install.
        """
        self.__version = version
        self.__editable_root = editable_root

    @classmethod
    def detect(cls) -> KodoSource:
        """Describe the py-kodo installation this process imports.

        Returns:
            KodoSource: Its version, and its source root if editable.

        Raises:
            HarborRunError: py-kodo is not installed as a distribution.
        """
        try:
            dist = importlib.metadata.distribution(_DISTRIBUTION)
        except importlib.metadata.PackageNotFoundError as exc:
            raise HarborRunError("py-kodo is not installed as a package") from exc
        root: Path | None = None
        raw = dist.read_text("direct_url.json")
        if raw:
            try:
                direct: object = json.loads(raw)
            except ValueError:
                direct = {}
            if isinstance(direct, dict):
                data = cast(dict[str, object], direct)
                info = data.get("dir_info")
                url = data.get("url")
                editable = isinstance(info, dict) and cast(dict[str, object], info).get("editable")
                if editable and isinstance(url, str) and url.startswith("file://"):
                    root = Path(unquote(urlparse(url).path))
        return cls(dist.version, root)

    @property
    def version(self) -> str:
        """The installed py-kodo version."""
        return self.__version

    @property
    def editable_root(self) -> Path | None:
        """The source checkout, for an editable install."""
        return self.__editable_root

    def build_wheel(self, uv: Path, out_dir: Path) -> Path:
        """Build a wheel from the editable checkout.

        Args:
            uv (Path): The uv executable.
            out_dir (Path): Where to write the wheel.

        Returns:
            Path: The built wheel.

        Raises:
            HarborRunError: Not an editable install, or the build failed.
        """
        root = self.__editable_root
        if root is None:
            raise HarborRunError("Only an editable py-kodo install can be built into a wheel")
        out_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [str(uv), "build", "--wheel", "--out-dir", str(out_dir), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise HarborRunError(f"Building a py-kodo wheel failed:\n{completed.stderr.strip()}")
        wheels = sorted(out_dir.glob("py_kodo-*.whl"), key=lambda p: p.stat().st_mtime)
        if not wheels:
            raise HarborRunError(f"uv build wrote no py-kodo wheel to {out_dir}")
        return wheels[-1]


class HarborInvocation:
    """One ``harbor run -c`` through ``uv tool run``."""

    __uv: Path
    __config: Path
    __kodo: str
    __extra: tuple[str, ...]

    def __init__(
        self, uv: Path, config_path: Path, kodo_requirement: str, extra_args: list[str]
    ) -> None:
        """Bind what to run.

        Args:
            uv (Path): The uv executable.
            config_path (Path): The job config.
            kodo_requirement (str): The py-kodo Harbor's process gets: a wheel
                path or ``py-kodo==VERSION``.
            extra_args (list[str]): More ``harbor run`` arguments, verbatim.
        """
        self.__uv = uv
        self.__config = config_path
        self.__kodo = kodo_requirement
        self.__extra = tuple(extra_args)

    def command(self) -> list[str]:
        """The full command line.

        Returns:
            list[str]: argv.
        """
        return [
            str(self.__uv),
            "tool",
            "run",
            "--from",
            f"harbor=={HARBOR_VERSION}",
            "--with",
            self.__kodo,
            "harbor",
            "run",
            "-c",
            str(self.__config),
            *self.__extra,
        ]

    def run(self) -> int:
        """Run Harbor in the foreground, sharing this terminal.

        Returns:
            int: Harbor's exit code.
        """
        return subprocess.run(self.command(), check=False).returncode


def find_uv(kodo_dir: Path) -> Path:
    """The uv executable: on PATH, else the one kodo bundles.

    Args:
        kodo_dir (Path): The host's ``~/.kodo``.

    Returns:
        Path: uv.

    Raises:
        HarborRunError: No uv anywhere.
    """
    on_path = shutil.which("uv")
    if on_path:
        return Path(on_path)
    bundled = find_util(kodo_dir, "uv")
    if bundled is not None:
        return bundled.path
    raise HarborRunError("uv was not found; install it (https://docs.astral.sh/uv/)")


def check_docker() -> None:
    """Fail fast when Docker is not usable.

    Raises:
        HarborRunError: No docker CLI, or the daemon does not answer.
    """
    docker = shutil.which("docker")
    if docker is None:
        raise HarborRunError("Docker is required: the docker CLI is not on PATH")
    completed = subprocess.run(
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise HarborRunError(f"The Docker daemon is not reachable: {completed.stderr.strip()}")
