"""Workflow stage definitions and transitions."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Stage"]


class Stage(StrEnum):
    """Coarse phases of the Kodo workflow (FR-WF-01).

    The stage machine progresses through these values in order.  ``*``
    suffixed names are per-component fan-outs; with one worker they run
    serially in alphabetical component order.
    """

    # Pre-workflow states
    IDLE = "IDLE"
    STOPPED = "STOPPED"
    ERROR = "ERROR"

    # Active workflow stages (in execution order)
    NARRATIVE = "NARRATIVE"
    ARCHITECTURE = "ARCHITECTURE"
    REQUIREMENTS = "REQUIREMENTS"  # per-component
    DESIGN = "DESIGN"  # per-component
    TEST_PLAN = "TEST_PLAN"  # per-component
    TEST_CODING = "TEST_CODING"  # per-component
    IMPLEMENTATION = "IMPLEMENTATION"  # per-component
    INTEGRATION_TEST = "INTEGRATION_TEST"
    E2E_TEST = "E2E_TEST"
    FINAL = "FINAL"
    DONE = "DONE"
