"""Kodo runtime engine — decomposed into mixins + collaborators.

The public surface is unchanged: :class:`WorkflowEngine`. Internally the
former 4000-line module is split along its concern seams:

Mixins (share the engine instance's state via :class:`~._proto.EngineHost`):

- :mod:`._llm` — plugin/model resolution, silent LLM turns, security judge
- :mod:`._worker` — the single queue-driven worker coroutine
- :mod:`._turns` — entry-agent runs + the generic LLM turn/tool loop
- :mod:`._subagents` — gated spawns, subsessions, Author/Critic rounds
- :mod:`._resume` — Stop folding + cold-restart resume of dangling turns

Collaborators (own their state; reach back via narrow host protocols):

- :mod:`._events` — every client event emitter + cumulative cost
- :mod:`._compaction` — context gauge + in-place compaction
- :mod:`._titling` — silent session titling
- :mod:`._checkpointing` — shadow-git mirrors + undo/redo/rollback
- :mod:`._history` — session.jsonl read-back (feed rebuild, context rehydration)

:mod:`._core` wires it all together; :mod:`._shared` holds the shared
constants; :mod:`._services` adapts the engine to the tools' EngineServices
protocol.
"""

from ._core import WorkflowEngine
from ._shared import _slugify_project_name, _unique_child_dir

__all__ = [
    "WorkflowEngine",
    "_slugify_project_name",
    "_unique_child_dir",
]
