"""Benchmark sessions: the server runs ``kodo-harbor`` and shows the run in a session tab.

doc/BENCHMARK_SESSIONS.md §4. A ``harbor.run`` on a brand-new session makes it
a benchmark session (:meth:`HarborJobs.start`): the session is named and locked
to input, and one :class:`HarborJob` runs ``python -m kodo.harbor run`` for it
in the background:

1. **Local model, macOS/Windows:** hold the managed llama-server
   **exclusively** for the whole run (``LLMGateway.acquire_local``, §4.6) on
   the entry's stored configuration (D10), make it serve the entry, and pass
   it to ``kodo-harbor`` as ``--llama-url``. **Linux:** hold it the same way
   but unload it, and let ``kodo-harbor`` start its own standalone server on
   the docker bridge (D3, D4). **Cloud:** fetch the key over the session
   (``api_key.request``) unless the server's environment has one, and put it
   only in the child's environment (D7).
2. **Run** ``kodo-harbor`` in its own process group (Windows: its own hidden
   console), output to ``<session dir>/harbor-output.log``.
3. **Watch** the job directory: each finished trial is pushed as
   ``harbor.trial`` and persisted; the end as ``harbor.finished``. What the
   agent does in each trial is pushed as ``harbor.activity`` while a window
   is attached (:class:`~kodo.harbor.TrialActivity`), never persisted; a
   ``hello`` gets all of it again from the job directory
   (:func:`push_trial_activity`).

**Stop** closes ``kodo-harbor``'s stdin (``--stop-on-stdin-close``), which then
interrupts its own process group — Harbor winds its trials down as on Ctrl+C —
and kills the whole tree after a grace period. One run at a time, server-wide
(D11). A run the server never saw finish — the server stopped or crashed — is
marked ``interrupted`` at the next start (:meth:`HarborJobs.recover`), and its
process tree killed if it still runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Protocol, TypeVar

import psutil  # type: ignore[import-untyped]

from kodo.common import Envelope
from kodo.harbor import (
    AGENT_BASICS_SUITE,
    TERMINUS_2,
    BenchModel,
    HarborRunError,
    JobSummary,
    ListedModel,
    SuiteCatalog,
    TrialActivity,
)
from kodo.headless import ModelSpec, resolve_vendor_api_key, vendor_credential_env_names
from kodo.llms import LLMGateway, get_local_registry
from kodo.llms.llamacpp import LlamaServer, ensure_llama_running, plan_llama_command
from kodo.state import TransientStore
from kodo.transport import (
    EVT_HARBOR_ACTIVITY,
    EVT_HARBOR_FINISHED,
    EVT_HARBOR_TRIAL,
    EVT_LLAMA_STATE,
)

from ._key_broker import KeyBroker
from ._session import Session

__all__ = [
    "HarborJob",
    "HarborJobs",
    "HarborRequest",
    "LocalLlama",
    "ManagedLlama",
    "push_trial_activity",
]

_log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Statuses of a run that has not ended.
_ACTIVE = frozenset({"waiting", "running", "stopping"})
_LOG_FILE = "harbor-output.log"
_SUMMARY_FILE = "kodo-summary.md"
_DETAIL_LINES = 40
_POLL_SECONDS = 2.0
_GRACE_SECONDS = 120.0
_STARTUP_FAILURE = 2
_KEY_PLACEHOLDER = "<from VS Code>"
_REGION_VARS = ("AWS_REGION", "AWS_DEFAULT_REGION")


class HarborRequest:
    """A ``harbor.run`` payload, type-checked: what to run, before it is checked."""

    __agent: str
    __model: str
    __suite: str
    __attempts: int
    __control: bool
    __thinking_level: str | None

    def __init__(
        self,
        agent: str,
        model: str,
        suite: str,
        attempts: int = 1,
        control: bool = False,
        thinking_level: str | None = None,
    ) -> None:
        """Bind the request.

        Args:
            agent (str): The Kōdo top-level agent to benchmark.
            model (str): ``ENTRY`` (local) or ``VENDOR/MODEL_ID`` (cloud).
            suite (str): A suite name (``kodo-harbor list-suites``).
            attempts (int): Attempts per task, at least 1.
            control (bool): Also run a ``terminus-2`` control arm.
            thinking_level (str | None): A thinking tier; ``None`` for the
                model's default.
        """
        self.__agent = agent
        self.__model = model
        self.__suite = suite
        self.__attempts = attempts
        self.__control = control
        self.__thinking_level = thinking_level

    @classmethod
    def parse(cls, payload: Mapping[str, object]) -> HarborRequest:
        """Read a ``harbor.run`` payload.

        Args:
            payload (Mapping[str, object]): ``{agent, model, suite, attempts,
                control, thinking_level}``.

        Returns:
            HarborRequest: The request.

        Raises:
            HarborRunError: A missing field or a wrong type.
        """
        texts: dict[str, str] = {}
        for key in ("agent", "model", "suite"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise HarborRunError(f"harbor.run needs a non-empty {key!r}")
            texts[key] = value.strip()
        attempts = payload.get("attempts", 1)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise HarborRunError("'attempts' must be a whole number of at least 1")
        thinking = payload.get("thinking_level")
        if thinking is not None and not isinstance(thinking, str):
            raise HarborRunError("'thinking_level' must be a string or null")
        return cls(
            texts["agent"],
            texts["model"],
            texts["suite"],
            attempts,
            payload.get("control") is True,
            thinking or None,
        )

    @property
    def agent(self) -> str:
        """The agent benchmarked."""
        return self.__agent

    @property
    def model(self) -> str:
        """The model, as ``--model`` spells it."""
        return self.__model

    @property
    def suite(self) -> str:
        """The suite run."""
        return self.__suite

    @property
    def attempts(self) -> int:
        """Attempts per task."""
        return self.__attempts

    @property
    def control(self) -> bool:
        """Whether a ``terminus-2`` control arm runs too."""
        return self.__control

    @property
    def thinking_level(self) -> str | None:
        """The thinking tier; ``None`` for the model's default."""
        return self.__thinking_level

    @property
    def kind(self) -> str:
        """``basics`` for the Basics test suite, else ``benchmark``."""
        return "basics" if self.__suite == AGENT_BASICS_SUITE else "benchmark"

    @property
    def title(self) -> str:
        """The benchmark session's name."""
        label = "Basics test" if self.kind == "basics" else "Benchmark"
        return f"{label}: {self.__agent} · {self.__model}"

    def check(self, kodo_dir: Path, env: Mapping[str, str]) -> BenchModel:
        """Make the checks ``kodo-harbor run`` makes before it creates a job.

        Blocking (it reads the registry and the suite files). A cloud model's
        key may still be in VS Code rather than in *env*, so its presence is
        not checked here; the job asks for it when it starts.

        Args:
            kodo_dir (Path): The user's ``~/.kodo``.
            env (Mapping[str, str]): The server's environment.

        Returns:
            BenchModel: The checked model.

        Raises:
            HarborRunError: The model, its thinking level or the suite fails,
            or a local model is not installed or is not one the server's
            llama-server runs.
        """
        try:
            spec = ModelSpec.parse(self.__model)
        except ValueError as exc:
            raise HarborRunError(f"model {self.__model!r}: {exc}") from exc
        checked_env = dict(env)
        if spec.vendor is not None:
            names = vendor_credential_env_names(spec.vendor)
            if names and not any(checked_env.get(name, "").strip() for name in names):
                checked_env[names[0]] = _KEY_PLACEHOLDER
        model = BenchModel.resolve(self.__model, checked_env, kodo_dir)
        if self.__thinking_level is not None:
            model.check_thinking_level(self.__thinking_level)
        SuiteCatalog.for_user().find(self.__suite)
        if model.is_local and spec.name not in {row.model for row in ListedModel.local(kodo_dir)}:
            raise HarborRunError(
                f"{spec.name!r} is not installed, or is not a model Kōdo's llama-server runs"
            )
        return model


class LocalLlama(Protocol):
    """The server's managed llama-server, as a local-model run uses it."""

    def command(self, entry: str) -> tuple[str, ...]:
        """The llama-server argv *entry* runs with on its stored configuration."""
        ...

    async def serve(self, entry: str) -> str:
        """Make the managed llama-server serve *entry*; return its base URL."""
        ...

    async def unload(self) -> None:
        """Stop the managed llama-server, if it runs."""
        ...


class ManagedLlama:
    """:class:`LocalLlama` on the server's real managed llama-server."""

    __kodo_dir: Path

    def __init__(self, kodo_dir: Path) -> None:
        """Bind the registry location.

        Args:
            kodo_dir (Path): The user's ``~/.kodo``.
        """
        self.__kodo_dir = kodo_dir

    def command(self, entry: str) -> tuple[str, ...]:
        """The argv *entry* runs with on its stored profile and knobs (D10).

        Args:
            entry (str): A local-registry entry.

        Returns:
            tuple[str, ...]: The planned llama-server command line.
        """
        return plan_llama_command(get_local_registry(self.__kodo_dir)[entry], self.__kodo_dir)

    async def serve(self, entry: str) -> str:
        """Start or reuse the managed llama-server for *entry*.

        Args:
            entry (str): A local-registry entry.

        Returns:
            str: ``http://127.0.0.1:<port>``.
        """
        server = await ensure_llama_running(
            get_local_registry(self.__kodo_dir)[entry], self.__kodo_dir
        )
        return f"http://127.0.0.1:{server.port}"

    async def unload(self) -> None:
        """Stop the managed llama-server, if it runs."""
        server = LlamaServer.get_active_llama_server()
        if server is not None and server.is_running:
            await server.stop()


class _Stopped(Exception):
    """A Stop arrived before ``kodo-harbor`` started."""


class HarborJob:
    """One benchmark session's run of ``kodo-harbor``."""

    __session: Session
    __request: HarborRequest
    __model: BenchModel
    __gateway: LLMGateway
    __llama: LocalLlama
    __jobs_dir: Path
    __command: tuple[str, ...]
    __platform: str
    __get_settings: Callable[[], dict[str, object]]
    __poll_seconds: float
    __grace_seconds: float
    __record: dict[str, object]
    __task: asyncio.Task[None] | None
    __process: asyncio.subprocess.Process | None
    __cancel_preparing: Callable[[], bool] | None
    __stop_requested: bool
    __kill_timer: asyncio.TimerHandle | None
    __seen: set[str]
    __activity: TrialActivity

    def __init__(
        self,
        session: Session,
        request: HarborRequest,
        model: BenchModel,
        *,
        gateway: LLMGateway,
        llama: LocalLlama,
        jobs_dir: Path,
        command: Sequence[str],
        platform: str,
        get_settings: Callable[[], dict[str, object]],
        poll_seconds: float = _POLL_SECONDS,
        grace_seconds: float = _GRACE_SECONDS,
    ) -> None:
        """Bind everything one run needs; nothing starts until :meth:`begin`.

        Args:
            session (Session): The brand-new session the run shows in.
            request (HarborRequest): What to run.
            model (BenchModel): The checked model (:meth:`HarborRequest.check`).
            gateway (LLMGateway): Holds the managed llama-server's lease.
            llama (LocalLlama): The managed llama-server.
            jobs_dir (Path): Where Harbor writes job directories.
            command (Sequence[str]): How ``kodo-harbor`` is run, before ``run``.
            platform (str): ``sys.platform`` (Linux runs its own llama-server).
            get_settings (Callable[[], dict[str, object]]): Live settings
                (the Bedrock region).
            poll_seconds (float): How often the job directory is read.
            grace_seconds (float): How long a Stop waits before killing.
        """
        self.__session = session
        self.__request = request
        self.__model = model
        self.__gateway = gateway
        self.__llama = llama
        self.__jobs_dir = jobs_dir
        self.__command = tuple(command)
        self.__platform = platform
        self.__get_settings = get_settings
        self.__poll_seconds = poll_seconds
        self.__grace_seconds = grace_seconds
        job_name = f"{request.agent}-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        self.__record = {
            "status": "waiting",
            "kind": request.kind,
            "agent": request.agent,
            "model": request.model,
            "suite": request.suite,
            "attempts": request.attempts,
            "control": request.control,
            "thinking_level": request.thinking_level,
            "job_dir": str(jobs_dir / job_name),
            "log_path": str(session.transient.session_dir / _LOG_FILE),
            "started_at": datetime.now(tz=UTC).isoformat(),
        }
        self.__task = None
        self.__process = None
        self.__cancel_preparing = None
        self.__stop_requested = False
        self.__kill_timer = None
        self.__seen = set()
        self.__activity = TrialActivity(jobs_dir / job_name)

    @property
    def session_id(self) -> str:
        """The benchmark session's id."""
        return self.__session.id

    @property
    def title(self) -> str:
        """The benchmark session's name."""
        return self.__session.transient.session_name

    @property
    def status(self) -> str:
        """``waiting`` · ``running`` · ``stopping`` · ``completed`` · ``failed`` · ``stopped``."""
        return str(self.__record["status"])

    @property
    def active(self) -> bool:
        """Whether the run has not ended."""
        return self.status in _ACTIVE

    async def begin(self) -> str | None:
        """Make the session a benchmark session and start the run in the background.

        Returns:
            str | None: ``None`` once started, else why the session can't
            take it (it is not brand new). Nothing changes on a refusal.
        """
        refusal = await self.__session.engine.handle_job_start(
            dict(self.__record), self.__request.title
        )
        if refusal is not None:
            return refusal
        header = {key: value for key, value in self.__record.items() if key != "status"}
        await self.__session.engine.record_job_event("", "harbor_job", header)
        self.__task = asyncio.create_task(self.__run(), name=f"harbor-{self.session_id}")
        return None

    def stop(self) -> None:
        """Ask the run to stop as Ctrl+C would; the trials that finished are kept.

        Before ``kodo-harbor`` starts this abandons the wait (for the lease,
        the key or the model). Once it runs, its stdin is closed, which
        interrupts its process group; the whole tree is killed if it has not
        exited after the grace period. No-op once the run ended.
        """
        if not self.active or self.__stop_requested:
            return
        self.__stop_requested = True
        if self.__process is None:
            if self.__cancel_preparing is not None:
                self.__cancel_preparing()
            return
        stdin = self.__process.stdin
        if stdin is not None:
            stdin.close()
        self.__kill_timer = asyncio.get_running_loop().call_later(
            self.__grace_seconds, self.__kill_tree
        )
        asyncio.create_task(self.__set_status("stopping"))

    async def wait(self) -> None:
        """Wait until the run has ended (and its record is written)."""
        if self.__task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self.__task)

    async def kill(self) -> None:
        """End the run at once — its session is being deleted — and wait for it."""
        self.__stop_requested = True
        self.__kill_tree()
        if self.__cancel_preparing is not None:
            self.__cancel_preparing()
        await self.wait()

    async def __run(self) -> None:
        holder = f"harbor:{self.session_id}"
        exit_code: int | None = None
        detail = ""
        try:
            env, llama_url = await self.__prepare(holder)
            exit_code = await self.__execute(env, llama_url)
        except _Stopped:
            pass
        except Exception as exc:  # noqa: BLE001 — any failure ends the run as failed
            _log.exception("Benchmark %s failed to run", self.session_id)
            detail = str(exc) or type(exc).__name__
        finally:
            self.__gateway.release_local(holder)
            if self.__kill_timer is not None:
                self.__kill_timer.cancel()
        await self.__scan()
        await self.__finish(exit_code, detail)

    async def __prepare(self, holder: str) -> tuple[dict[str, str], str | None]:
        env = dict(os.environ)
        vendor = self.__model.spec.vendor
        if vendor is not None:
            if resolve_vendor_api_key(vendor, env) is None:
                env.update(await self.__cloud_credential(vendor, env))
            return env, None
        entry = self.__model.spec.name
        command = self.__llama.command(entry)
        await self.__guard(
            lambda: self.__gateway.acquire_local(holder, command, exclusive=True, label=self.title)
        )
        if self.__platform == "linux":
            # Containers can't reach the managed server's loopback; kodo-harbor
            # starts its own on the bridge, so this one must not hold the GPU (D4).
            await self.__guard(self.__llama.unload)
            await self.__llama_state({"running": False, "model": None})
            return env, None
        # The sidebar mirrors llama-server from these, as a chat turn's load does.
        await self.__llama_state({"running": False, "starting": True})
        try:
            url = await self.__guard(lambda: self.__llama.serve(entry))
        except Exception as exc:
            await self.__llama_state({"running": False, "model": None, "error": str(exc)})
            raise
        port = urllib.parse.urlsplit(url).port
        await self.__llama_state({"running": True, "model": entry, "port": port})
        return env, url

    async def __llama_state(self, payload: dict[str, object]) -> None:
        await self.__session.channel.send(Envelope.make_event(EVT_LLAMA_STATE, payload))

    async def __cloud_credential(self, vendor: str, env: Mapping[str, str]) -> dict[str, str]:
        names = vendor_credential_env_names(vendor)
        if not names:
            raise HarborRunError(f"{vendor} has no credential variable to pass a key in")
        key = await self.__guard(lambda: KeyBroker(self.__session.channel).get_key(vendor))
        if key.error or not key.api_key:
            raise HarborRunError(f"No {vendor} API key: {key.error or 'none was entered'}")
        found = {names[0]: key.api_key}
        region = str(self.__get_settings().get("bedrock_region", "") or "")
        if vendor == "bedrock" and region and not any(env.get(n) for n in _REGION_VARS):
            found["AWS_REGION"] = region
        return found

    async def __guard(self, step: Callable[[], Awaitable[_T]]) -> _T:
        """Run one preparation step that a Stop abandons (never started after a Stop)."""
        if self.__stop_requested:
            raise _Stopped
        future = asyncio.ensure_future(step())
        self.__cancel_preparing = future.cancel
        try:
            return await future
        except asyncio.CancelledError:
            if self.__stop_requested and future.cancelled():
                raise _Stopped from None
            raise
        finally:
            self.__cancel_preparing = None

    async def __execute(self, env: dict[str, str], llama_url: str | None) -> int:
        argv = [*self.__command, "run", *self.__arguments(llama_url)]
        log_path = Path(str(self.__record["log_path"]))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            process = await self.__spawn(argv, env, log)
            self.__process = process
            self.__record["process"] = _process_identity(process.pid)
            await self.__set_status("running")
            if self.__stop_requested:
                # Stop landed while the process was starting.
                self.__stop_requested = False
                self.stop()
            while True:
                try:
                    return await asyncio.wait_for(process.wait(), self.__poll_seconds)
                except TimeoutError:
                    await self.__scan()

    def __arguments(self, llama_url: str | None) -> list[str]:
        request = self.__request
        job_dir = Path(str(self.__record["job_dir"]))
        arguments = [
            "--agent", request.agent,
            "--model", request.model,
            "--suite", request.suite,
            "--attempts", str(request.attempts),
            "--jobs-dir", str(job_dir.parent),
            "--job-name", job_dir.name,
            "--stop-on-stdin-close",
        ]  # fmt: skip
        if request.control:
            arguments += ["--control", TERMINUS_2]
        if request.thinking_level is not None:
            arguments += ["--thinking-level", request.thinking_level]
        if llama_url is not None:
            arguments += ["--llama-url", llama_url]
        return arguments

    @staticmethod
    async def __spawn(
        argv: list[str], env: dict[str, str], log: IO[bytes]
    ) -> asyncio.subprocess.Process:
        if sys.platform == "win32":
            # Its own hidden console: kodo-harbor interrupts every process on
            # it with CTRL_BREAK_EVENT, which never reaches this server.
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(Path.home()),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startupinfo,
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(Path.home()),
            start_new_session=True,
        )

    async def __scan(self) -> None:
        job_dir = Path(str(self.__record["job_dir"]))
        # Before the trial rows, so a trial's last events land before its row.
        chunks = await asyncio.to_thread(self.__activity.read)
        if self.__session.channel.connection is not None:
            # Detached, nothing is sent (it would pile up in the outbox for
            # hours); the next hello reads it all again (push_trial_activity).
            for chunk in chunks:
                await self.__session.channel.send(Envelope.make_event(EVT_HARBOR_ACTIVITY, chunk))
        rows, total = await asyncio.to_thread(_read_progress, job_dir)
        for row in rows:
            name = str(row["trial"])
            if name in self.__seen:
                continue
            self.__seen.add(name)
            payload = {**row, "finished": len(self.__seen), "total": total}
            await self.__session.engine.record_job_event(EVT_HARBOR_TRIAL, "harbor_trial", payload)

    async def __finish(self, exit_code: int | None, detail: str) -> None:
        log_path = Path(str(self.__record["log_path"]))
        summary = await asyncio.to_thread(
            _read_text, Path(str(self.__record["job_dir"])) / _SUMMARY_FILE
        )
        if self.__stop_requested:
            outcome = "stopped"
        elif summary and exit_code != _STARTUP_FAILURE and not detail:
            outcome = "completed"
        else:
            outcome = "failed"
        if outcome == "failed" and not detail:
            detail = await asyncio.to_thread(_log_tail, log_path)
        self.__record.pop("process", None)
        await self.__set_status(outcome)
        await self.__session.engine.record_job_event(
            EVT_HARBOR_FINISHED,
            "harbor_finished",
            {
                "outcome": outcome,
                "exit_code": exit_code,
                "summary_markdown": summary,
                "detail": detail,
                "job_dir": self.__record["job_dir"],
                "log_path": str(log_path),
            },
        )
        _log.info("Benchmark %s ended: %s (exit %s)", self.session_id, outcome, exit_code)

    async def __set_status(self, status: str) -> None:
        if not self.active and status in _ACTIVE:
            return
        self.__record["status"] = status
        await self.__session.engine.update_job(dict(self.__record))

    def __kill_tree(self) -> None:
        process = self.__process
        if process is not None and process.returncode is None:
            _kill_tree(process.pid)


class HarborJobs:
    """Every benchmark run of this server: at most one active at a time (D11)."""

    __gateway: LLMGateway
    __kodo_dir: Path
    __jobs_dir: Path
    __llama: LocalLlama
    __command: tuple[str, ...]
    __platform: str
    __get_settings: Callable[[], dict[str, object]]
    __poll_seconds: float
    __grace_seconds: float
    __jobs: dict[str, HarborJob]
    __lock: asyncio.Lock

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        kodo_dir: Path,
        get_settings: Callable[[], dict[str, object]],
        llama: LocalLlama | None = None,
        command: Sequence[str] | None = None,
        platform: str = sys.platform,
        poll_seconds: float = _POLL_SECONDS,
        grace_seconds: float = _GRACE_SECONDS,
    ) -> None:
        """Bind what every run shares.

        Args:
            gateway (LLMGateway): The server's LLM gateway (the llama lease).
            kodo_dir (Path): The user's ``~/.kodo``; jobs go to
                ``<kodo_dir>/harbor/jobs``.
            get_settings (Callable[[], dict[str, object]]): Live settings.
            llama (LocalLlama | None): The managed llama-server; ``None`` for
                the real one.
            command (Sequence[str] | None): How ``kodo-harbor`` is run, before
                ``run``; ``None`` for ``python -m kodo.harbor``.
            platform (str): ``sys.platform``.
            poll_seconds (float): How often a run's job directory is read.
            grace_seconds (float): How long a Stop waits before killing.
        """
        self.__gateway = gateway
        self.__kodo_dir = kodo_dir
        self.__jobs_dir = kodo_dir / "harbor" / "jobs"
        self.__llama = llama if llama is not None else ManagedLlama(kodo_dir)
        self.__command = tuple(command or (sys.executable, "-m", "kodo.harbor"))
        self.__platform = platform
        self.__get_settings = get_settings
        self.__poll_seconds = poll_seconds
        self.__grace_seconds = grace_seconds
        self.__jobs = {}
        self.__lock = asyncio.Lock()

    @property
    def running(self) -> HarborJob | None:
        """The run that has not ended, if any."""
        return next((job for job in self.__jobs.values() if job.active), None)

    def for_session(self, session_id: str) -> HarborJob | None:
        """The run of *session_id*, ended or not.

        Args:
            session_id (str): A session id.

        Returns:
            HarborJob | None: Its run; ``None`` for a session this server
            process started no run in.
        """
        return self.__jobs.get(session_id)

    async def start(self, session: Session, payload: Mapping[str, object]) -> str | None:
        """Start a ``harbor.run`` on *session*.

        Args:
            session (Session): A brand-new session.
            payload (Mapping[str, object]): The ``harbor.run`` payload.

        Returns:
            str | None: ``None`` once the run started, else why it was
            refused. Nothing changes on a refusal.
        """
        async with self.__lock:
            running = self.running
            if running is not None:
                return f"Another benchmark is running in {running.title!r}; one runs at a time"
            try:
                request = HarborRequest.parse(payload)
                model = await asyncio.to_thread(request.check, self.__kodo_dir, dict(os.environ))
            except HarborRunError as exc:
                return str(exc)
            job = HarborJob(
                session,
                request,
                model,
                gateway=self.__gateway,
                llama=self.__llama,
                jobs_dir=self.__jobs_dir,
                command=self.__command,
                platform=self.__platform,
                get_settings=self.__get_settings,
                poll_seconds=self.__poll_seconds,
                grace_seconds=self.__grace_seconds,
            )
            refusal = await job.begin()
            if refusal is not None:
                return refusal
            self.__jobs[session.id] = job
            _log.info("Benchmark started in %s: %s", session.id, request.title)
            return None

    async def stop_all(self) -> None:
        """Stop every active run as Stop does and wait for them (server shutdown, D5)."""
        jobs = [job for job in self.__jobs.values() if job.active]
        for job in jobs:
            job.stop()
        for job in jobs:
            await job.wait()

    async def discard(self, session_id: str) -> None:
        """Kill *session_id*'s run, if any, and forget it (its session is being deleted).

        Args:
            session_id (str): A session id.
        """
        job = self.__jobs.pop(session_id, None)
        if job is not None:
            await job.kill()

    def recover(self, sessions_dir: Path) -> list[str]:
        """Mark every run a previous server process never saw end ``interrupted``.

        Called once at startup, before any session loads. A run whose
        ``kodo-harbor`` still runs (the same pid and start time) has its
        process tree killed first. Nothing is resumed (D9).

        Args:
            sessions_dir (Path): ``~/.kodo/sessions``.

        Returns:
            list[str]: The ids of the sessions marked interrupted.
        """
        marked: list[str] = []
        if not sessions_dir.is_dir():
            return marked
        for session_dir in sorted(sessions_dir.iterdir()):
            if not (session_dir / "transient.json").is_file():
                continue
            store = TransientStore(self.__kodo_dir)
            store.attach_session(session_dir.name, True)
            record = store.harbor_job
            if record is None or record.get("status") not in _ACTIVE:
                continue
            identity = record.pop("process", None)
            if isinstance(identity, dict):
                _kill_recorded(identity)
            record["status"] = "interrupted"
            store.update(harbor_job=record)
            job_dir = Path(str(record.get("job_dir", "")))
            store.append_marker(
                {
                    "type": "harbor_finished",
                    "outcome": "interrupted",
                    "exit_code": None,
                    "summary_markdown": _read_text(job_dir / _SUMMARY_FILE),
                    "detail": "The Kōdo server stopped while the run was in progress.",
                    "job_dir": str(job_dir),
                    "log_path": str(record.get("log_path", "")),
                }
            )
            marked.append(session_dir.name)
            _log.info("Benchmark in %s marked interrupted", session_dir.name)
        return marked


async def push_trial_activity(session: Session) -> None:
    """Send a benchmark session's whole agent activity, read from its job directory.

    For a (re)attached window, after ``session.history``: the activity is
    never persisted, and a running job only pushes it while a window is
    attached. A no-op for a session that is no benchmark session.

    Args:
        session (Session): The session a window just attached to.
    """
    job = session.engine.session.job
    job_dir = job.get("job_dir") if job is not None else None
    if not isinstance(job_dir, str) or not job_dir:
        return
    chunks = await asyncio.to_thread(TrialActivity(Path(job_dir)).read)
    for chunk in chunks:
        await session.channel.send(Envelope.make_event(EVT_HARBOR_ACTIVITY, chunk))


def _read_progress(job_dir: Path) -> tuple[list[dict[str, object]], int | None]:
    return JobSummary.load(job_dir).trial_rows(), JobSummary.total_trials(job_dir)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _log_tail(path: Path) -> str:
    lines = _read_text(path).splitlines()
    return "\n".join(lines[-_DETAIL_LINES:])


def _process_identity(pid: int) -> dict[str, object]:
    try:
        created = float(psutil.Process(pid).create_time())
    except psutil.Error:
        created = 0.0
    return {"pid": pid, "created": created}


def _kill_recorded(identity: dict[str, object]) -> None:
    # The pid alone could name an unrelated process by now; its start time
    # must match what was recorded when the run started.
    pid = identity.get("pid")
    created = identity.get("created")
    if not isinstance(pid, int) or not isinstance(created, (int, float)):
        return
    try:
        if abs(float(psutil.Process(pid).create_time()) - float(created)) > 1.0:
            return
    except psutil.Error:
        return
    _kill_tree(pid)


def _kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        processes = [*root.children(recursive=True), root]
    except psutil.Error:
        return
    for process in processes:
        with contextlib.suppress(psutil.Error):
            process.kill()
