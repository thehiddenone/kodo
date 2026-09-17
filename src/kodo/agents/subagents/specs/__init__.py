"""Sub-agent specifications — the typed input/output contract of each sub-agent.

This package holds **one ``<name>.json`` file per sub-agent**, each describing a
single :class:`~kodo.agents.SubAgentSpec`, plus the code that turns them into
objects. Nothing in the catalog is hardcoded in Python: :data:`ALL_SUBAGENTS` is
built at import time by globbing this directory, so adding a sub-agent's
contract is adding a file and nothing else — no import to write, no ``__all__``
entry, no tuple to append to.

Every sub-agent **except** the user-facing top-level agents (``guide``,
``problem_solver``, ``judge``) has a spec here. The registry cross-references a
spec to its ``subagent_<name>.md`` by ``name`` and fails fast if either side is
missing; ``name`` must equal the filename stem, so the JSON, the prompt and the
file name cannot drift apart.

The three pieces
================

- :mod:`._loader` turns one JSON file into a spec, and documents the file
  format. Schemas are written as a **shape** — the name of a builder plus its
  arguments — so the shared envelopes stay defined once in Python; ``{"shape":
  "raw", "schema": {…}}`` declares a literal schema for an agent whose contract
  is its own.
- :mod:`._shapes` holds those builders (``pipeline_input``/``author_output``/
  ``critic_output``), declarative schema constructors with no runtime logic.
- :mod:`._order` derives the catalog's order from the ``produces``/``consumes``
  graph, replacing the hand-maintained tuple that used to encode it.

Why files rather than literals
==============================

The catalog was 23 Python modules, each exporting one constant, listed by hand
in a tuple here. Every one of those layers was a chance for the list and the
modules to disagree, and none of it was reachable by anyone who was not editing
the package: a contract could only be added by writing Python inside an
installed distribution. The specs are **data** — two JSON Schemas, a role
mapping and a list of needs — and are now stored as data. This is phase 1, which
reads only this package's own directory; user-authored spec directories build on
the same loader.

The per-agent constants (``CODER``, ``PLANNER``, …) are gone with the modules
that defined them. Nothing outside this package ever imported one — the registry
and the tests consume :data:`ALL_SUBAGENTS` — and a name bound at import time is
exactly what a runtime-built catalog cannot provide. Look one up with
:data:`~kodo.agents.SUBAGENT_SPECS_BY_NAME`.
"""

from __future__ import annotations

from pathlib import Path

from .._subagentspec import SubAgentSpec
from ._loader import SPEC_SUFFIX, SpecLoadError, load_spec, load_specs
from ._order import SpecOrderError, pipeline_order

__all__ = [
    "ALL_SUBAGENTS",
    "SPECS_DIR",
    "SPEC_SUFFIX",
    "SpecLoadError",
    "SpecOrderError",
    "SubAgentSpec",
    "load_spec",
    "load_specs",
    "pipeline_order",
]

#: The directory the built-in specs are read from — this package's own.
#: Ships inside the wheel alongside the ``.py`` files (see ``pyproject.toml``'s
#: ``tool.hatch.build.targets.wheel.include``).
SPECS_DIR: Path = Path(__file__).parent

# Every sub-agent spec in the catalog, in declared-dependency order. Consumed by
# kodo.agents._registry to auto-grant `return_result` (bound to each spec's
# output_schema) and the Input Parameters note, to mint each caller's
# `run_subagent_<name>` tools, and to validate spec <-> subagent_<name>.md
# correspondence. Built from the JSON files at import time: a malformed file or
# a cycle in the declared graph raises here, at startup, rather than at first
# spawn.
ALL_SUBAGENTS: tuple[SubAgentSpec, ...] = pipeline_order(load_specs(SPECS_DIR))
