"""Core orchestrator: workflow lifecycle, concurrency, and decision management."""

from __future__ import annotations

import asyncio
import multiprocessing
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._models import DecisionState, WorkflowRecord
from ._worker import worker_main


def _supports_subinterpreters() -> bool:
    return sys.version_info >= (3, 14)


def run_mode() -> str:
    """Return the active worker execution mode.

    Returns:
        str: ``"interpreter"`` on Python 3.14+ (stub), ``"subprocess"`` otherwise.
    """
    return "interpreter" if _supports_subinterpreters() else "subprocess"


class Orchestrator:
    """Manages workflow creation, execution, cancellation, and decision handling.

    All workflow state is in-memory; nothing survives a server restart.
    """

    __max: int
    __timeout: float
    __base_url: str
    __workflows: dict[str, WorkflowRecord]
    __lock: asyncio.Lock

    def __init__(
        self,
        max_workflows: int,
        decision_timeout: float,
        base_url: str,
    ) -> None:
        """Initialise the orchestrator.

        Args:
            max_workflows (int): Maximum number of concurrently active workflows.
            decision_timeout (float): Seconds before a pending decision is auto-resolved.
            base_url (str): Base URL used by worker subprocesses to reach the server.
        """
        self.__max = max_workflows
        self.__timeout = decision_timeout
        self.__base_url = base_url
        self.__workflows = {}
        self.__lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        """Number of workflows currently in ``PENDING`` or ``RUNNING`` state."""
        return sum(1 for w in self.__workflows.values() if w.is_active())

    async def create_workflow(
        self,
        module: str,
        intake_path: str,
        parent_id: Optional[str] = None,
    ) -> WorkflowRecord:
        """Create and schedule a new workflow.

        Args:
            module (str): Dotted import path of the workflow module.
            intake_path (str): Path to the plain-text intake file.
            parent_id (str | None): UUID of the parent workflow for child creation.

        Returns:
            WorkflowRecord: The newly created record in ``PENDING`` state.

        Raises:
            RuntimeError: If the concurrency limit has been reached.
            ValueError: If ``parent_id`` is unknown or refers to a child workflow.
        """
        async with self.__lock:
            if self.active_count >= self.__max:
                raise RuntimeError(
                    f"Concurrency limit of {self.__max} active workflows reached"
                )
            if parent_id is not None:
                parent = self.__workflows.get(parent_id)
                if parent is None:
                    raise ValueError(f"Parent workflow {parent_id!r} not found")
                if parent.is_child:
                    raise ValueError(
                        f"Workflow {parent_id!r} is a child and cannot spawn children"
                    )

            wid = str(uuid.uuid4())
            workdir = Path(tempfile.mkdtemp(prefix=f"kodo-{wid[:8]}-"))
            record = WorkflowRecord(
                workflow_id=wid,
                parent_id=parent_id,
                workdir=workdir,
                started_at=datetime.now(tz=timezone.utc),
                is_child=parent_id is not None,
            )
            if parent_id:
                self.__workflows[parent_id].add_child(wid)
            self.__workflows[wid] = record

        asyncio.create_task(self.__start_worker(wid, module, intake_path))
        return record

    async def cancel(self, wid: str) -> None:
        """Cancel a workflow and all its children.

        Args:
            wid (str): UUID of the workflow to cancel.

        Raises:
            KeyError: If no workflow with the given UUID exists.
        """
        record = self.__workflows.get(wid)
        if record is None:
            raise KeyError(wid)
        for child_id in reversed(record.children):
            child = self.__workflows.get(child_id)
            if child and child.is_active():
                await self.__cancel_one(child_id)
        await self.__cancel_one(wid)

    async def cancel_all(self) -> None:
        """Cancel all currently active workflows.

        Iterates top-level (parent) workflows only; cancellation cascades to
        their children automatically via :meth:`cancel`.
        """
        for wid, record in list(self.__workflows.items()):
            if record.is_active() and record.parent_id is None:
                await self.cancel(wid)

    def get(self, wid: str) -> Optional[WorkflowRecord]:
        """Return the record for a given UUID, or ``None``.

        Args:
            wid (str): Workflow UUID.

        Returns:
            WorkflowRecord | None: The record, or ``None`` if not found.
        """
        return self.__workflows.get(wid)

    def list_all(self) -> list[WorkflowRecord]:
        """Return a snapshot of all known workflow records.

        Returns:
            list[WorkflowRecord]: All records regardless of state.
        """
        return list(self.__workflows.values())

    async def register_decision(
        self,
        wid: str,
        prompt: str,
        options: list[str],
        default: str,
    ) -> None:
        """Attach a pending decision to a running workflow.

        Args:
            wid (str): Workflow UUID.
            prompt (str): Question to present to the user.
            options (list[str]): Valid response values.
            default (str): Value applied automatically on timeout.

        Raises:
            KeyError: If the workflow UUID is not found.
        """
        record = self.__workflows.get(wid)
        if record is None:
            raise KeyError(wid)
        record.set_decision(DecisionState(prompt=prompt, options=options, default=default))
        asyncio.create_task(self.__decision_timeout_task(wid))

    async def submit_decision(
        self,
        wid: str,
        choice: str,
        message: Optional[str] = None,
    ) -> None:
        """Submit a human answer for a pending decision.

        Args:
            wid (str): Workflow UUID.
            choice (str): One of ``accepted``, ``rejected``, or ``feedback``.
            message (str | None): Required when ``choice`` is ``feedback``.

        Raises:
            KeyError: If the workflow UUID is not found.
            ValueError: If there is no pending decision.
        """
        record = self.__workflows.get(wid)
        if record is None:
            raise KeyError(wid)
        decision = record.decision
        if decision is None or not decision.is_pending():
            raise ValueError("No pending decision for this workflow")
        decision.submit(choice, message)

    def get_decision_answer(self, wid: str) -> dict:
        """Return the current decision status for a worker polling for its answer.

        Args:
            wid (str): Workflow UUID.

        Returns:
            dict: ``{"status": ...}`` with an optional ``"data"`` key.
        """
        record = self.__workflows.get(wid)
        if record is None or record.state == "CANCELLED":
            return {"status": "cancelled"}
        decision = record.decision
        if decision is None:
            return {"status": "pending"}
        if decision.status == "answered":
            return {"status": "answered", "data": {"answer": decision.answer}}
        return {"status": decision.status}

    async def __start_worker(self, wid: str, module: str, intake_path: str) -> None:
        record = self.__workflows[wid]

        if _supports_subinterpreters():
            # TODO: implement sub-interpreter execution (Python 3.14+)
            pass

        proc = multiprocessing.Process(
            target=worker_main,
            args=(module, intake_path, wid, str(record.workdir), self.__base_url),
            daemon=True,
        )
        record.start(proc)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, record.join)

        if record.state != "CANCELLED":
            await self.__on_worker_done(wid)

    async def __on_worker_done(self, wid: str) -> None:
        record = self.__workflows[wid]
        if (record.workdir / "SUCCESS").exists():
            record.succeed()
            # TODO: invoke mirror-git-commit step before cleanup
        else:
            record.fail()
        await self.__cleanup(wid)

    async def __cancel_one(self, wid: str) -> None:
        record = self.__workflows[wid]
        record.cancel()
        await self.__cleanup(wid)

    async def __cleanup(self, wid: str) -> None:
        record = self.__workflows.get(wid)
        if record and record.workdir.exists():
            shutil.rmtree(record.workdir, ignore_errors=True)

    async def __decision_timeout_task(self, wid: str) -> None:
        await asyncio.sleep(self.__timeout)
        record = self.__workflows.get(wid)
        if record and record.decision:
            record.decision.timeout()
