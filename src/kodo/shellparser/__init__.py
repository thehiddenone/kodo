"""Structural parser for shell command lines (POSIX and PowerShell/Windows).

A **parse-only**, dependency-free leaf package (tier T0 — imports nothing from
``kodo``).  :func:`parse_command` (bash/POSIX) and
:func:`parse_powershell_command` (PowerShell/cmd) turn a command string into
the same neutral :class:`ParsedCommand` view — pipeline segments, each with its
executable, arguments, and redirections, plus the operators joining them.  The
package deliberately makes **no judgement** about what a command does
(mutation, danger, network, …); that classification belongs to callers.  The
checkpoint hook and the security layer each apply their own checks over the
same structural parse.
"""

from ._parser import ParsedCommand, Redirection, Segment, parse_command
from ._powershell import parse_powershell_command

__all__ = [
    "ParsedCommand",
    "Redirection",
    "Segment",
    "parse_command",
    "parse_powershell_command",
]
