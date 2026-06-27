"""Structural parser for POSIX-ish shell command lines.

A **parse-only**, dependency-free leaf package (tier T0 — imports nothing from
``kodo``).  :func:`parse_command` turns a command string into a neutral
:class:`ParsedCommand` view — pipeline segments, each with its executable,
arguments, and redirections, plus the operators joining them.  It deliberately
makes **no judgement** about what a command does (mutation, danger, network,
…); that classification belongs to callers.  The checkpoint hook and the
security layer each apply their own checks over the same structural parse.
"""

from ._parser import ParsedCommand, Redirection, Segment, parse_command

__all__ = [
    "ParsedCommand",
    "Redirection",
    "Segment",
    "parse_command",
]
