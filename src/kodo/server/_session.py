"""A single server-managed session — one VS Code window's working context.

Each :class:`Session` owns its own engine, transient store, channel (sink +
buffer), key broker, gate orchestrator, and workspace view.  Sessions are minted
and wired by :class:`kodo.server.SessionManager`; ownership (which live window
holds the session) is tracked by the manager, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from kodo.project import SessionWorkspace
from kodo.runtime import WorkflowEngine
from kodo.state import TransientStore
from kodo.transport import SessionChannel

__all__ = ["Session"]


@dataclass
class Session:
    """Container for one window's per-session collaborators.

    Attributes:
        id: Session identifier (a timestamp, possibly with a uniqueness suffix).
        channel: Stable per-session sink that buffers while the window is gone.
        engine: The workflow engine driving this session.
        transient: Append-only JSONL session store.
        session_workspace: The window's physical root + logical folder map.
    """

    id: str
    channel: SessionChannel
    engine: WorkflowEngine
    transient: TransientStore
    session_workspace: SessionWorkspace
