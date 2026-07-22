"""Checkpointing coordinator (shadow-git mirror, both workflow modes).

:class:`CheckpointCoordinator` owns the per-root :class:`RootMirrorManager`
and everything around it: the per-tool-call prepare/commit cycle, the
undo/redo/rollback/roll-forward operations behind the client's checkpoint
buttons, the ``checkpoint.state`` broadcasts, and the Guided-state
``new_revision`` attribution. Path resolution and the mode-aware root set
stay engine-owned and are reached through :class:`CheckpointHost`.

A call carrying ``temporary: true`` (the session-scoped scratch directory —
:func:`kodo.project.session_temp_dir`, doc/SECURITY.md) is skipped by
:meth:`CheckpointCoordinator.prepare` outright: it never earns a mirror
commit, an undo/rollback entry, or a ``new_revision`` jsonl attribution.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from kodo.common import Envelope, MessageSink
from kodo.guided_state import append_new_revision, is_tracked
from kodo.shellparser import parse_command
from kodo.state import TransientStore
from kodo.tools import PathResolver, RootPath
from kodo.transport import EVT_CHECKPOINT_STATE

from .._checkpoints import CheckpointRef, CheckpointState, RootMirrorManager, command_may_mutate
from .._session import SessionState

_log = logging.getLogger(__name__)

# File-mutating tools that earn a shadow-mirror checkpoint after each call, in
# both workflow modes.
_MUTATING_TOOLS = frozenset(
    {"filesystem", "edit_file", "create_file", "create_directory", "run_command"}
)

# Of those, the tools whose commit also earns a `new_revision` entry in a
# tracked document's .jsonl evolution log (run_command's targets are too
# coarse-grained — a whole cwd, not a specific file — to attribute cleanly).
_GUIDED_STATE_TOOLS = frozenset({"filesystem", "edit_file", "create_file", "create_directory"})


class CheckpointHost(Protocol):
    """What the checkpoint coordinator needs back from the engine."""

    _session: SessionState
    _current_project: dict[str, str] | None
    _orch_session_id: str
    _transient: TransientStore

    def _make_resolver(self, session_id: str) -> PathResolver: ...

    def _root_paths(self) -> tuple[RootPath, ...]: ...


class CheckpointCoordinator:
    """Owns the shadow-git mirrors and the per-tool-call checkpoint cycle."""

    def __init__(self, host: CheckpointHost, *, sink: MessageSink) -> None:
        self._host = host
        self._sink = sink
        # Per-root shadow-git checkpoint mirrors. Drives both workflow modes: a
        # Guided-mode filesystem/edit_file/create_file/create_directory/run_command
        # call earns a checkpoint exactly like a Problem-Solver one (see _enabled).
        self._mirrors = RootMirrorManager()

    @property
    def mirrors(self) -> RootMirrorManager:
        """The underlying mirror manager (state lookups for feed rebuilds)."""
        return self._mirrors

    def sync_roots(self) -> None:
        """Refresh the mirror manager's known-roots set from the engine."""
        self._mirrors.set_roots([Path(rp.path) for rp in self._host._root_paths()])

    def _enabled(self) -> bool:
        """Whether per-tool-call checkpointing runs for the current prompt.

        Unconditional: Guided mode now drives the same shadow-git mirror
        Problem Solver always has — there is no separate Guided checkpoint
        system to collide with anymore.
        """
        return True

    async def prepare(self, tool_name: str, tool_input: dict[str, object]) -> list[Path]:
        """Snapshot the pre-mutation baseline for a mutating tool; return its paths.

        Called *before* dispatch so each root's mirror baseline reflects the tree
        as it was before this call. Returns the affected paths (primary first) to
        hand to :meth:`commit`, or an empty list when nothing should be
        checkpointed (wrong mode, non-mutating tool, a read-only command, or a
        call scoped to the session's private ``temporary`` scratch directory —
        see :func:`kodo.project.session_temp_dir` — which never enters a
        project's mirror).
        """
        if not self._enabled() or tool_name not in _MUTATING_TOOLS or tool_input.get("temporary"):
            return []
        paths = self.mutation_paths(tool_name, tool_input)
        if paths:
            self.sync_roots()
            for path in paths:
                await self._mirrors.prepare(path)
        return paths

    async def commit(
        self, tool_name: str, tool_input: dict[str, object], paths: list[Path]
    ) -> CheckpointRef | None:
        """Commit the mirror after a mutating tool ran; return its checkpoint ref.

        Commits the root enclosing the primary path. ``run_command`` additionally
        sweeps every other already-initialised mirror (no-op when clean) so a
        command that wrote outside its cwd's root is still captured.

        Every root that actually earns a non-empty commit here — the primary
        one, plus any the ``run_command`` sweep catches — permanently locks
        that folder into the session's remembered workspace shape (see
        ``TransientStore.lock_workspace_path`` /
        ``WorkflowEngine.handle_workspace_folders``). This is the sole place
        that lock is ever set.
        """
        if not paths:
            return None
        label = self.label(tool_name, tool_input)
        ref = await self._mirrors.commit_for_path(paths[0], label)
        if ref is not None:
            self._host._transient.lock_workspace_path(ref.root)
        if tool_name == "run_command":
            swept_roots = await self._mirrors.sweep_initialized(label)
            for root in swept_roots:
                self._host._transient.lock_workspace_path(str(root))
        return ref

    def mutation_paths(self, tool_name: str, tool_input: dict[str, object]) -> list[Path]:
        """Resolve the filesystem paths a mutating tool will touch (primary first).

        Always built against the orchestrator's own resolver/scratch dir, not
        whichever sub-agent subsession is actually dispatching — checkpointing
        already tracks mirrors per *project* root, not per subsession, and a
        ``run_command`` whose ``working_dir`` lands under any session's scratch
        directory (orchestrator's or a subsession's own) resolves outside every
        known root either way, so :meth:`RootMirrorManager.prepare`/
        :meth:`~.RootMirrorManager.commit_for_path` treat it as a no-op.
        """
        resolver = self._host._make_resolver(self._host._orch_session_id)

        def _resolve(key: str) -> Path | None:
            value = tool_input.get(key)
            if not value:
                return None
            try:
                return resolver.resolve(str(value))
            except (PermissionError, ValueError):
                return None

        if tool_name in ("edit_file", "create_file", "create_directory"):
            path = _resolve("path")
            return [path] if path is not None else []
        if tool_name == "filesystem":
            # destination/path is the primary mutation; source matters for moves.
            return [p for p in (_resolve("destination"), _resolve("path"), _resolve("source")) if p]
        if tool_name == "run_command":
            command = str(tool_input.get("command", ""))
            if not command.strip() or not command_may_mutate(parse_command(command)):
                return []
            working_dir = tool_input.get("working_dir")
            try:
                cwd = resolver.resolve(str(working_dir)) if working_dir else resolver.default_cwd
            except (PermissionError, ValueError):
                cwd = resolver.default_cwd
            return [cwd]
        return []

    @staticmethod
    def label(tool_name: str, tool_input: dict[str, object]) -> str:
        """A short, human-readable commit message for a tool-call checkpoint."""
        if tool_name == "run_command":
            return f"run_command: {str(tool_input.get('command', ''))[:80]}"
        if tool_name == "filesystem":
            op = tool_input.get("operation", "")
            target = tool_input.get("path") or tool_input.get("destination", "")
            return f"filesystem {op}: {target}"
        return f"{tool_name}: {tool_input.get('path', '')}"

    async def record_guided_revision(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        checkpoint: CheckpointRef,
        agent_name: str,
    ) -> None:
        """Append a ``new_revision`` jsonl entry for a tracked document's commit.

        Fires in *both* workflow modes whenever the touched path falls under
        the bound project's ``specs``/``src``/``test`` (see
        ``kodo.guided_state``) — independent of which mirror root the
        checkpoint itself committed to. A Problem-Solver edit to a tracked
        document is recorded too, tagged ``workflow: "problem_solving"``, so
        the Guide can reconcile state once Guided mode resumes; no other
        jsonl entry type is ever written outside Guided mode, since
        ``document_feedback`` (the only producer of the other three) is never
        granted to Problem Solver.
        """
        if self._host._current_project is None:
            return
        project_root = Path(self._host._current_project["root"])
        paths = self.mutation_paths(tool_name, tool_input)
        if not paths or not is_tracked(paths[0], project_root):
            return
        await asyncio.to_thread(
            append_new_revision,
            paths[0],
            project_root,
            commit_hash=checkpoint.sha,
            author=agent_name,
            tool=tool_name,
            summary=self.label(tool_name, tool_input),
            workflow=self._host._session.effective_workflow_mode,
        )

    async def undo(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Undo checkpoint *sha* in *root*'s mirror; return the updated state.

        Restores the files that commit touched to their prior state
        (discarding later edits to those same files) as a new commit and
        flips that entry's ``undone`` flag. The conversation and agent state
        are untouched.

        Raises:
            MirrorDirtyError: The work tree has edits Kodo didn't make and
                *resolution* wasn't given — caller should ask the user how to
                proceed and retry with a resolution.
        """
        self.sync_roots()
        state = await self._mirrors.undo(root, sha, resolution)
        _log.info(
            "Checkpoint undo: root=%s sha=%s current_index=%d", root, sha[:8], state.current_index
        )
        await self.push_state(root, state)
        return state

    async def redo(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Redo checkpoint *sha* in *root*'s mirror; return the updated state.

        See :meth:`undo` — this is its inverse and shares the same dirty-tree
        handling.
        """
        self.sync_roots()
        state = await self._mirrors.redo(root, sha, resolution)
        _log.info(
            "Checkpoint redo: root=%s sha=%s current_index=%d", root, sha[:8], state.current_index
        )
        await self.push_state(root, state)
        return state

    async def rollback(self, root: str, sha: str, resolution: str | None = None) -> CheckpointState:
        """Move *root*'s current branch to checkpoint *sha*; return the updated state.

        See :meth:`undo` for the dirty-tree handling.
        """
        self.sync_roots()
        state = await self._mirrors.rollback(root, sha, resolution)
        _log.info(
            "Checkpoint rollback: root=%s sha=%s current_index=%d",
            root,
            sha[:8],
            state.current_index,
        )
        await self.push_state(root, state)
        return state

    async def roll_forward(
        self, root: str, sha: str, resolution: str | None = None
    ) -> CheckpointState:
        """Move *root*'s current branch forward to checkpoint *sha*.

        Same underlying operation as :meth:`rollback` — see
        :meth:`kodo.runtime._checkpoints.RootMirrorManager.roll_forward`.
        """
        self.sync_roots()
        state = await self._mirrors.roll_forward(root, sha, resolution)
        _log.info(
            "Checkpoint roll-forward: root=%s sha=%s current_index=%d",
            root,
            sha[:8],
            state.current_index,
        )
        await self.push_state(root, state)
        return state

    async def state_for(self, root: str) -> CheckpointState:
        """The persisted :class:`CheckpointState` for *root* (UI hydration)."""
        self.sync_roots()
        return await self._mirrors.state_for(root)

    async def push_state(self, root: str, state: CheckpointState) -> None:
        """Broadcast *root*'s updated state so every checkpoint button can refresh."""
        await self._sink.send(
            Envelope.make_event(
                EVT_CHECKPOINT_STATE,
                {
                    "root": root,
                    "current_index": state.current_index,
                    "entries": [{"sha": e.sha, "undone": e.undone} for e in state.entries],
                },
            )
        )
