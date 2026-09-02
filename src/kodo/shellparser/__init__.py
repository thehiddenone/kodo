"""Structural parser for shell command lines (POSIX and PowerShell/Windows).

A **parse-only**, dependency-free leaf package (tier T0 — imports nothing from
``kodo``).  :func:`parse_command` (bash/POSIX) and
:func:`parse_powershell_command` (PowerShell/cmd) turn a command string into
the same neutral :class:`ParsedCommand` view — pipeline segments, each with its
executable, arguments, and redirections, plus the operators joining them.

:func:`flatten_command` goes one step further (POSIX dialect): it walks the
control-flow structure — branches, loops, ``case`` arms, function definitions
and their call sites — and returns the same :class:`ParsedCommand` view
populated with the primitive commands the script *may* execute, rather than
only the ones a flat separator split happens to expose.  A construct it cannot
reduce to commands at all becomes an *opaque* segment (:func:`opaque_reason`)
that callers must treat as unanalyzable, never as empty.

The package deliberately makes **no judgement** about what a command does
(mutation, danger, network, …); that classification belongs to callers.  The
checkpoint hook and the security layer each apply their own checks over the
same structural parse.
"""

from ._flatten import OPAQUE_PREFIX, flatten_command, opaque_reason
from ._parser import (
    ParsedCommand,
    Redirection,
    Segment,
    is_fd_merge_target,
    parse_command,
    redirection_writes_file,
)
from ._powershell import parse_powershell_command

__all__ = [
    "OPAQUE_PREFIX",
    "ParsedCommand",
    "Redirection",
    "Segment",
    "flatten_command",
    "is_fd_merge_target",
    "parse_command",
    "parse_powershell_command",
    "opaque_reason",
    "redirection_writes_file",
]
