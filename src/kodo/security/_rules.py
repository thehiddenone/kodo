"""Security rule schema and matcher — future iteration.

Reserved for persistent user-defined allow/deny rules ("always allow commands
like this") layered ahead of the per-call judgement in :mod:`._layer`. Not yet
implemented; the wire protocol reserves ``security.add_rule`` for it.
"""

from __future__ import annotations

__all__: list[str] = []
