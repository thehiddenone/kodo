"""Generic shadow-git checkpoint mirror (tier T0 — imports nothing from ``kodo``).

A truly low-level capability: :class:`ShadowMirror` drives a git repository
whose *git directory* lives apart from its *work tree*, so it can version a
directory in place without a second copy of the files and without a ``.git``
inside the tracked tree.  It knows nothing about Kōdo's ``.kodo`` layout or
``kodo.md`` — the caller supplies the work-tree path, the git-dir path, and the
ignore patterns.  Higher layers (the per-root checkpoint coordinator) wrap it
with project conventions.

``undo`` is append-only — it restores touched files as a *new* forward commit,
so undoing an undo (a "redo") stays possible. ``rollback`` instead moves the
current branch's ref directly to a target commit (forward or backward),
preserving whatever tip it orphans under a ``rollback_<unix-ts>`` branch so no
commit is ever left unreachable — it never detaches HEAD.
"""

from ._mirror import CommitInfo, ShadowMirror, ShadowMirrorError

__all__ = [
    "CommitInfo",
    "ShadowMirror",
    "ShadowMirrorError",
]
