"""The Harbor job a benchmark run becomes — one ``JobConfig`` JSON document.

``kodo-harbor run`` never drives Harbor through its Python API: it writes this
document and hands it to ``harbor run -c`` (:class:`~._runner.HarborInvocation`),
so the whole run is reproducible from the file alone (``harbor run -c
<job>/kodo-job.json``). Every agent row runs the same tasks, attempts and
sandbox:

- **Kodo** — ``kodo.harbor.agent:KodoAgent`` with the chosen top-level agent
  and model. Cloud: the vendor's credential variables as ``${NAME}`` templates
  (resolved by Harbor from the host environment at trial start) and the
  vendor API host on the agent-phase allowlist. Local: the host llama-server
  URL, the host's registry mounted read-only, and ``host.docker.internal`` on
  the allowlist.
- **Control arms** (``--control``) — any Harbor agent on the same model.
  ``terminus-2`` runs on the host through LiteLLM, so it also serves a local
  model (the llama-server's OpenAI endpoint); other agents take a cloud model
  by the same ``VENDOR/MODEL_ID`` and credentials.

Compose overlays (``environment.extra_docker_compose``) adjust the task's
``main`` service: Linux maps ``host.docker.internal`` for a local model, and
``--platform`` pins the platform the image is built or pulled for
(:func:`platform_overlay`) — SWE-bench images are ``linux/amd64`` only, which
an arm64 Docker cannot build natively.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._errors import HarborRunError
from ._llama import CONTAINER_HOST, LlamaAccess
from ._model import BenchModel
from ._selection import Selection

__all__ = [
    "CONTAINER_REGISTRY_PATH",
    "KODO_AGENT_IMPORT_PATH",
    "TERMINUS_2",
    "JobPlan",
    "KodoInstall",
    "platform_overlay",
]

#: How Harbor loads the adapter.
KODO_AGENT_IMPORT_PATH = "kodo.harbor.agent:KodoAgent"
#: Harbor's reference terminal agent — the default control arm.
TERMINUS_2 = "terminus-2"
#: Where the host's local-LLM registry is mounted in a task container.
CONTAINER_REGISTRY_PATH = "/kodo-host/local-llm-registry.json"

_LOCAL_API_KEY = "sk-no-key-required"

# Docker's OS/ARCH[/VARIANT] spelling, e.g. linux/amd64 or linux/arm/v7.
_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9_]+(/[a-z0-9]+)?")


def platform_overlay(platform: str) -> str:
    """The compose overlay that builds and runs a task's ``main`` service on *platform*.

    Args:
        platform (str): Docker's ``OS/ARCH[/VARIANT]``, e.g. ``linux/amd64``.

    Returns:
        str: The overlay YAML.

    Raises:
        HarborRunError: *platform* is not spelled ``OS/ARCH[/VARIANT]``.
    """
    if _PLATFORM.fullmatch(platform) is None:
        raise HarborRunError(f"--platform {platform!r}: expected OS/ARCH, e.g. linux/amd64")
    return f'services:\n  main:\n    platform: "{platform}"\n'


class KodoInstall:
    """Which py-kodo a trial container installs: a PyPI version or a wheel."""

    __version: str | None
    __wheel: Path | None

    def __init__(self, version: str | None = None, wheel: Path | None = None) -> None:
        """Choose the install source.

        Args:
            version (str | None): A PyPI version; ``None`` uses the adapter's own.
            wheel (Path | None): A local wheel, uploaded into each container.
        """
        self.__version = version
        self.__wheel = wheel

    @property
    def version(self) -> str | None:
        """The PyPI version, if pinned explicitly."""
        return self.__version

    @property
    def wheel(self) -> Path | None:
        """The local wheel, if any."""
        return self.__wheel


class JobPlan:
    """Everything one benchmark run asks of Harbor."""

    __job_name: str
    __jobs_dir: Path
    __model: BenchModel
    __agent: str
    __selection: Selection
    __controls: tuple[str, ...]
    __attempts: int
    __concurrency: int
    __max_retries: int
    __thinking_level: str | None
    __turn_timeout: float | None
    __install: KodoInstall
    __agents_dir: Path | None
    __llama: LlamaAccess | None
    __registry_file: Path | None
    __compose_overlays: tuple[Path, ...]

    def __init__(
        self,
        *,
        job_name: str,
        jobs_dir: Path,
        model: BenchModel,
        agent: str,
        selection: Selection,
        controls: list[str],
        attempts: int = 1,
        concurrency: int = 1,
        max_retries: int = 0,
        thinking_level: str | None = None,
        turn_timeout: float | None = None,
        install: KodoInstall | None = None,
        agents_dir: Path | None = None,
        llama: LlamaAccess | None = None,
        registry_file: Path | None = None,
        compose_overlays: list[Path] | None = None,
    ) -> None:
        """Bind and check the run's parameters.

        Args:
            job_name (str): Harbor's job name (the job directory's name).
            jobs_dir (Path): Where Harbor writes jobs.
            model (BenchModel): The model every arm runs.
            agent (str): Kodo's top-level agent.
            selection (Selection): The tasks.
            controls (list[str]): Harbor agent names run as control arms.
            attempts (int): Attempts per task and agent (``n_attempts``).
            concurrency (int): Trials in flight at once.
            max_retries (int): Harbor retries for a trial that errored.
            thinking_level (str | None): Kodo's thinking tier.
            turn_timeout (float | None): Kodo-side turn bound (seconds).
            install (KodoInstall | None): Where containers get py-kodo.
            agents_dir (Path | None): User agents to install in containers.
            llama (LlamaAccess | None): The host llama-server (local model).
            registry_file (Path | None): The host registry to mount (local model).
            compose_overlays (list[Path] | None): Compose overlays for the task's
                ``main`` service (Linux local-model host mapping, ``--platform``).

        Raises:
            HarborRunError: The combination cannot run.
        """
        if selection.is_empty:
            raise HarborRunError("Nothing to run: give --dataset, --task, --path or --suite")
        if model.is_local and llama is None:
            raise HarborRunError("A local model needs its host llama-server")
        if model.is_local:
            others = sorted(c for c in controls if c != TERMINUS_2)
            if others:
                raise HarborRunError(
                    f"Control arm(s) {', '.join(others)} cannot use a local model; "
                    f"only {TERMINUS_2} (which runs on the host) can"
                )
        if attempts < 1 or concurrency < 1 or max_retries < 0:
            raise HarborRunError("--attempts and --concurrency must be ≥ 1, --max-retries ≥ 0")
        self.__job_name = job_name
        self.__jobs_dir = jobs_dir
        self.__model = model
        self.__agent = agent
        self.__selection = selection
        self.__controls = tuple(dict.fromkeys(controls))
        self.__attempts = attempts
        self.__concurrency = concurrency
        self.__max_retries = max_retries
        self.__thinking_level = thinking_level
        self.__turn_timeout = turn_timeout
        self.__install = install or KodoInstall()
        self.__agents_dir = agents_dir
        self.__llama = llama
        self.__registry_file = registry_file
        self.__compose_overlays = tuple(compose_overlays or ())

    @property
    def job_dir(self) -> Path:
        """The directory Harbor writes this job to."""
        return self.__jobs_dir / self.__job_name

    def to_config(self) -> dict[str, object]:
        """The Harbor ``JobConfig`` document.

        Returns:
            dict[str, object]: JSON-ready; no secret values, only templates.
        """
        environment: dict[str, object] = {"type": "docker"}
        if self.__registry_file is not None:
            environment["mounts"] = [
                {
                    "type": "bind",
                    "source": str(self.__registry_file),
                    "target": CONTAINER_REGISTRY_PATH,
                    "read_only": True,
                }
            ]
        if self.__compose_overlays:
            environment["extra_docker_compose"] = [str(p) for p in self.__compose_overlays]
        config: dict[str, object] = {
            "job_name": self.__job_name,
            "jobs_dir": str(self.__jobs_dir),
            "n_attempts": self.__attempts,
            "n_concurrent_trials": self.__concurrency,
            "retry": {"max_retries": self.__max_retries},
            "environment": environment,
            "agents": [self.__kodo_agent(), *(self.__control(c) for c in self.__controls)],
            "datasets": self.__selection.datasets,
            "tasks": self.__selection.tasks,
        }
        return config

    def __kodo_agent(self) -> dict[str, object]:
        kwargs: dict[str, object] = {"agent": self.__agent}
        hosts = self.__model.allowed_hosts()
        if self.__llama is not None:
            kwargs["llama_url"] = self.__llama.container_url
            hosts = [CONTAINER_HOST]
            if self.__registry_file is not None:
                kwargs["registry_file"] = CONTAINER_REGISTRY_PATH
        if self.__thinking_level is not None:
            kwargs["thinking_level"] = self.__thinking_level
        if self.__turn_timeout is not None:
            kwargs["turn_timeout_sec"] = self.__turn_timeout
        if self.__install.wheel is not None:
            kwargs["kodo_wheel"] = str(self.__install.wheel)
        if self.__install.version is not None:
            kwargs["version"] = self.__install.version
        if self.__agents_dir is not None:
            kwargs["agents_dir"] = str(self.__agents_dir)
        row: dict[str, object] = {
            "import_path": KODO_AGENT_IMPORT_PATH,
            "model_name": self.__model.harbor_model_name,
            "kwargs": kwargs,
        }
        env = self.__model.kodo_env()
        if env:
            row["env"] = env
        if hosts:
            row["extra_allowed_hosts"] = hosts
        return row

    def __control(self, name: str) -> dict[str, object]:
        llama = self.__llama
        if name == TERMINUS_2 and llama is not None:
            return {
                "name": name,
                "model_name": f"openai/{llama.entry}",
                "kwargs": {
                    "api_base": f"{llama.host_url}/v1",
                    "model_info": {
                        "max_input_tokens": llama.context_per_slot,
                        "max_output_tokens": llama.context_per_slot,
                        "input_cost_per_token": 0.0,
                        "output_cost_per_token": 0.0,
                    },
                },
                "env": {"OPENAI_API_KEY": _LOCAL_API_KEY},
            }
        if name == TERMINUS_2:
            model_name, env = self.__model.litellm_model()
            return {"name": name, "model_name": model_name, "env": env}
        return {
            "name": name,
            "model_name": self.__model.harbor_model_name,
            "env": self.__model.kodo_env(),
            "extra_allowed_hosts": self.__model.allowed_hosts(),
        }
