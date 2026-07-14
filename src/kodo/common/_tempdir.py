"""System temporary-directory detection.

Shared by :mod:`kodo.security` (the `run_command` workspace-escape check) and
:mod:`kodo.tools` (the file-tool path resolvers) so both agree on what counts
as "system temp" — a location it's safe for an agent to read, write, or
delete under even though it sits outside every workspace root.
"""

from __future__ import annotations

import os
import tempfile

__all__ = ["system_temp_roots"]


def system_temp_roots() -> tuple[str, ...]:
    """Candidate roots for the OS temp directory, in every spelling a caller
    might use.

    Always includes ``tempfile.gettempdir()`` — the interpreter's own
    resolution of ``TMPDIR``/``TEMP``/``TMP`` and the platform default
    (e.g. macOS's per-user ``/var/folders/.../T/``, Windows'
    ``%LOCALAPPDATA%\\Temp``). On POSIX, the literal ``/tmp`` is always
    included too, even when ``gettempdir()`` resolves elsewhere (macOS
    prefers the per-user ``TMPDIR`` over ``/tmp``) — callers name ``/tmp``
    directly and expect it to work.

    Both the literal spelling *and* its ``realpath`` are included for each
    candidate: some callers (``kodo.security._analysis``) match arguments
    purely lexically and need the literal ``/tmp`` to be present verbatim;
    others (``kodo.tools._paths``, via ``Path.resolve()``) always compare
    against the symlink-resolved form (macOS's ``/tmp`` -> ``/private/tmp``).
    Carrying both spellings keeps every caller's containment check correct
    without each having to know which one it needs.
    """
    candidates = {tempfile.gettempdir()}
    if os.name != "nt":
        candidates.add("/tmp")
    roots: set[str] = set()
    for candidate in candidates:
        roots.add(candidate)
        roots.add(os.path.realpath(candidate))
    return tuple(sorted(roots))
