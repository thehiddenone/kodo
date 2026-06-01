"""Data models for the virtual workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ArtifactType(StrEnum):
    """Known artifact types produced and consumed by Kodo agents."""

    NARRATIVE = "narrative"
    ARCHITECTURE = "architecture"
    REQUIREMENTS = "requirements"
    PLAN = "plan"
    FUNCTIONAL_DESIGN = "functional-design"
    DESIGN_PLAN = "design-plan"
    TECH_STACK = "tech-stack"
    CODE = "code"
    TEST_PLAN = "test-plan"
    TEST = "test"
    FEEDBACK = "feedback"


class Verdict(StrEnum):
    """Review outcome recorded on a feedback artifact."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class Concern:
    """A single structured issue raised by a critic in a feedback artifact."""

    kind: str
    description: str
    first_line: int | None = None
    last_line: int | None = None
    excerpt: str | None = None


@dataclass
class Artifact:
    """A named piece of content published into the workspace.

    ``content`` and ``concerns`` are only populated when loaded with
    ``include_content=True`` via :meth:`~kodo.workspace.Workspace.read`.
    When retrieved as metadata-only, ``content`` is ``None`` and
    ``concerns`` is an empty list.
    """

    id: str
    type: ArtifactType
    author: str
    project_code: str
    responsibility_code: str
    created_at: datetime
    content: str | None
    requirement_ids: list[str] = field(default_factory=list)
    filename_hint: str | None = None
    supersedes: list[str] = field(default_factory=list)
    reviewed_artifact_id: str | None = None
    verdict: Verdict | None = None
    concerns: list[Concern] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
