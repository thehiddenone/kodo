"""Domain model classes for workflow and decision state."""

from __future__ import annotations

from datetime import datetime
from multiprocessing import Process
from pathlib import Path


class DecisionState:
    """Holds the state of a pending user decision attached to a workflow."""

    __prompt: str
    __options: list[str]
    __default: str
    __answer: str | None
    __status: str

    def __init__(self, prompt: str, options: list[str], default: str) -> None:
        """Initialise a new pending decision.

        Args:
            prompt (str): Question to present to the user.
            options (list[str]): Valid response values.
            default (str): Value applied automatically on timeout.
        """
        self.__prompt = prompt
        self.__options = options
        self.__default = default
        self.__answer = None
        self.__status = "pending"

    @property
    def prompt(self) -> str:
        """Question presented to the user."""
        return self.__prompt

    @property
    def options(self) -> list[str]:
        """Valid response values."""
        return self.__options

    @property
    def default(self) -> str:
        """Value applied automatically on timeout."""
        return self.__default

    @property
    def answer(self) -> str | None:
        """The recorded answer, or ``None`` if not yet answered."""
        return self.__answer

    @property
    def status(self) -> str:
        """Current status: ``pending``, ``answered``, ``timeout``, or ``cancelled``."""
        return self.__status

    def is_pending(self) -> bool:
        """Return ``True`` if no answer has been recorded yet.

        Returns:
            bool: Whether the decision is still awaiting input.
        """
        return self.__status == "pending"

    def submit(self, choice: str, message: str | None = None) -> None:
        """Record a human answer.

        Args:
            choice (str): One of ``accepted``, ``rejected``, or ``feedback``.
            message (str | None): Required when ``choice`` is ``feedback``.

        Raises:
            ValueError: If the decision is not in the pending state.
        """
        if not self.is_pending():
            raise ValueError("Decision is not pending")
        self.__answer = message if choice == "feedback" else choice
        self.__status = "answered"

    def timeout(self) -> None:
        """Apply the default answer because the decision window elapsed."""
        if self.is_pending():
            self.__status = "timeout"

    def cancel(self) -> None:
        """Mark the decision as cancelled."""
        if self.is_pending():
            self.__status = "cancelled"


class WorkflowRecord:
    """Tracks the full lifecycle of a single workflow instance."""

    __id: str
    __state: str
    __parent_id: str | None
    __workdir: Path
    __started_at: datetime
    __is_child: bool
    __children: list[str]
    __process: Process | None
    __decision: DecisionState | None

    def __init__(
        self,
        workflow_id: str,
        parent_id: str | None,
        workdir: Path,
        started_at: datetime,
        is_child: bool,
    ) -> None:
        """Initialise a record in the ``PENDING`` state.

        Args:
            workflow_id (str): Runtime UUID.
            parent_id (str | None): UUID of the parent workflow, if any.
            workdir (Path): Isolated working directory.
            started_at (datetime): When the record was created.
            is_child (bool): Whether this is a child workflow.
        """
        self.__id = workflow_id
        self.__state = "PENDING"
        self.__parent_id = parent_id
        self.__workdir = workdir
        self.__started_at = started_at
        self.__is_child = is_child
        self.__children = []
        self.__process = None
        self.__decision = None

    @property
    def id(self) -> str:
        """Runtime UUID."""
        return self.__id

    @property
    def state(self) -> str:
        """Current state: ``PENDING``, ``RUNNING``, ``SUCCESS``, ``FAILED``, or ``CANCELLED``."""
        return self.__state

    @property
    def parent_id(self) -> str | None:
        """UUID of the parent workflow, or ``None`` for top-level workflows."""
        return self.__parent_id

    @property
    def workdir(self) -> Path:
        """Isolated working directory."""
        return self.__workdir

    @property
    def started_at(self) -> datetime:
        """Timestamp when the record was created."""
        return self.__started_at

    @property
    def is_child(self) -> bool:
        """Whether this is a child workflow."""
        return self.__is_child

    @property
    def children(self) -> list[str]:
        """Snapshot of child workflow UUIDs."""
        return list(self.__children)

    @property
    def decision(self) -> DecisionState | None:
        """The current pending decision, or ``None``."""
        return self.__decision

    def is_active(self) -> bool:
        """Return ``True`` if the workflow has not yet reached a terminal state.

        Returns:
            bool: Whether state is ``PENDING`` or ``RUNNING``.
        """
        return self.__state in ("PENDING", "RUNNING")

    def start(self, process: Process) -> None:
        """Transition to ``RUNNING`` and attach the worker process.

        Args:
            process (Process): The subprocess executing the workflow.
        """
        self.__state = "RUNNING"
        self.__process = process

    def succeed(self) -> None:
        """Transition to ``SUCCESS``."""
        self.__state = "SUCCESS"

    def fail(self) -> None:
        """Transition to ``FAILED``."""
        self.__state = "FAILED"

    def cancel(self) -> None:
        """Transition to ``CANCELLED``, cancel any pending decision, and terminate the process."""
        self.__state = "CANCELLED"
        if self.__decision is not None:
            self.__decision.cancel()
        if self.__process is not None and self.__process.is_alive():
            self.__process.terminate()

    def add_child(self, child_id: str) -> None:
        """Register a child workflow UUID.

        Args:
            child_id (str): UUID of the child workflow.
        """
        self.__children.append(child_id)

    def set_decision(self, decision: DecisionState) -> None:
        """Attach a pending decision to this workflow.

        Args:
            decision (DecisionState): The decision awaiting human input.
        """
        self.__decision = decision

    def join(self) -> None:
        """Block until the worker process terminates.

        Intended for use with ``loop.run_in_executor`` so the event loop
        is not blocked.
        """
        if self.__process is not None:
            self.__process.join()
