"""Stage specification — the data-driven workflow definition.

A :class:`StageSpec` describes one stage: which agents run, which artifact
is expected, and how the gate is labelled.  The engine iterates over
:data:`PROJECT_STAGES` and calls a single generic ``__run_stage`` for each,
so adding a new stage is just adding a new spec entry — no engine code needed.

``critic=None`` means "skip the Author/Critic loop — run the Author once
(or until the artifact is written) and go straight to the gate."  This
covers stages like Test Coding where the output is verified by running the
tests, not by a reviewer agent.

:func:`build_component_stages` generates per-component StageSpecs from a list
of component names; the engine calls it after the Architecture stage completes
and prepends the result to its pending stage queue.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._stages import Stage

__all__ = ["PROJECT_STAGES", "StageSpec", "build_component_stages"]


@dataclass(frozen=True)
class StageSpec:
    """Declarative description of one workflow stage.

    Attributes:
        stage: The :class:`Stage` enum value for this stage.
        author: Name of the agent that produces the artifact.
        critic: Name of the reviewing agent, or ``None`` to skip review.
        artifact: Artifact path the author must write, relative to project
            root and in POSIX notation (e.g. ``'src/narrative.kd'``).
        gate_type: Gate-type label used in ``approval.request`` events and
            mirror commit messages (e.g. ``'narrative'``).
        component: Component name for per-component stages; ``None`` for
            project-level stages.
    """

    stage: Stage
    author: str
    critic: str | None
    artifact: str
    gate_type: str
    component: str | None

    def build_task_message(self, context: dict[str, str]) -> str:
        """Return the initial user-turn message for this stage.

        Args:
            context: Accumulated stage outputs keyed by artifact path, plus
                ``'prompt'`` for the original developer prompt.

        Returns:
            str: Full content of the first user message sent to the author.
        """
        if self.stage == Stage.NARRATIVE:
            prompt = context.get("prompt", "")
            return (
                "## Task\n\n"
                "Write a project narrative based on the prompt below. "
                "Use the `fileio_write_file` tool to write the narrative "
                "to `src/narrative.kd`.\n\n"
                "## Project Prompt\n\n"
                f"{prompt}"
            )

        if self.stage == Stage.ARCHITECTURE:
            narrative = context.get("src/narrative.kd", "(narrative not available)")
            return (
                "## Task\n\n"
                "Design the software architecture for this project.\n"
                "1. Write component responsibilities to `src/responsibilities.kd`.\n"
                "2. Write the component dependency graph to "
                "`src/responsibilities.dag.json`.\n"
                "Use `fileio_write_file` for both files.\n\n"
                "## Project Narrative\n\n"
                f"{narrative}"
            )

        if self.stage == Stage.REQUIREMENTS:
            component = self.component or ""
            narrative = context.get("src/narrative.kd", "(narrative not available)")
            responsibilities = context.get(
                "src/responsibilities.kd", "(responsibilities not available)"
            )
            return (
                "## Task\n\n"
                f"Write the functional requirements for the `{component}` component.\n"
                f"Use the `fileio_write_file` tool to write them to "
                f"`src/{component}/requirements.kd`.\n\n"
                "## Project Narrative\n\n"
                f"{narrative}\n\n"
                "## Responsibilities\n\n"
                f"{responsibilities}"
            )

        if self.stage == Stage.DESIGN:
            component = self.component or ""
            requirements = context.get(
                f"src/{component}/requirements.kd", "(requirements not available)"
            )
            return (
                "## Task\n\n"
                f"Write the functional design for the `{component}` component.\n"
                f"Use the `fileio_write_file` tool to write it to "
                f"`src/{component}/design.kd`.\n\n"
                "## Requirements\n\n"
                f"{requirements}"
            )

        if self.stage == Stage.TEST_PLAN:
            component = self.component or ""
            requirements = context.get(
                f"src/{component}/requirements.kd", "(requirements not available)"
            )
            design = context.get(f"src/{component}/design.kd", "(design not available)")
            return (
                "## Task\n\n"
                f"Write the test plan for the `{component}` component.\n"
                f"Use the `fileio_write_file` tool to write it to "
                f"`src/{component}/test_plan.kd`.\n\n"
                "## Requirements\n\n"
                f"{requirements}\n\n"
                "## Design\n\n"
                f"{design}"
            )

        if self.stage == Stage.TEST_CODING:
            component = self.component or ""
            test_plan = context.get(f"src/{component}/test_plan.kd", "(test plan not available)")
            design = context.get(f"src/{component}/design.kd", "(design not available)")
            return (
                "## Task\n\n"
                f"Write the test code for the `{component}` component.\n\n"
                f"Write the main test file to `gen/{component}/tests/test_{component}.py`.\n"
                f"Also create `gen/{component}/tests/__init__.py` (empty) and "
                f"`gen/{component}/src/__init__.py` (empty) and "
                f"`gen/{component}/src/{component}.py` (stub with comment "
                f"'# Implementation placeholder') so imports resolve.\n\n"
                "All tests MUST be expected-to-fail until the implementation is written.\n\n"
                "## Test Plan\n\n"
                f"{test_plan}\n\n"
                "## Design\n\n"
                f"{design}"
            )

        if self.stage == Stage.IMPLEMENTATION:
            component = self.component or ""
            design = context.get(f"src/{component}/design.kd", "(design not available)")
            requirements = context.get(
                f"src/{component}/requirements.kd", "(requirements not available)"
            )
            return (
                "## Task\n\n"
                f"Implement the `{component}` component until all its tests pass.\n\n"
                f"Tests are in `gen/{component}/tests/`. "
                f"Write the implementation to `gen/{component}/src/{component}.py` "
                f"(and sub-modules if the design requires it).\n\n"
                f"Run tests with: `python -m pytest gen/{component}/tests/ -v`\n\n"
                "## Design\n\n"
                f"{design}\n\n"
                "## Requirements\n\n"
                f"{requirements}"
            )

        # Fallback: pass the raw prompt (future stages will add their own branch)
        return context.get("prompt", "")


# ---------------------------------------------------------------------------
# M3 workflow definition
# ---------------------------------------------------------------------------
# Adding a new stage = adding one entry here.  Per-component stages for M4+
# are generated dynamically by the engine after architecture completes.

PROJECT_STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        stage=Stage.NARRATIVE,
        author="narrative_author",
        critic="critic_stub",
        artifact="src/narrative.kd",
        gate_type="narrative",
        component=None,
    ),
    StageSpec(
        stage=Stage.ARCHITECTURE,
        author="architect",
        critic="critic_stub",
        artifact="src/responsibilities.kd",
        gate_type="responsibilities",
        component=None,
    ),
)


# ---------------------------------------------------------------------------
# M4 per-component stage factory
# ---------------------------------------------------------------------------


def build_component_stages(components: list[str]) -> tuple[StageSpec, ...]:
    """Build per-component StageSpecs for the given component names.

    Each component gets three consecutive specs: REQUIREMENTS → DESIGN →
    TEST_PLAN.  Components are processed in alphabetical order (FR-WF-03).

    Args:
        components: Component names (unsorted is fine; sorted internally).

    Returns:
        Tuple of :class:`StageSpec` entries — three per component.
    """
    specs: list[StageSpec] = []
    for name in sorted(components):
        specs.extend(
            [
                StageSpec(
                    stage=Stage.REQUIREMENTS,
                    author="requirements_author",
                    critic="requirements_reviewer",
                    artifact=f"src/{name}/requirements.kd",
                    gate_type="requirements",
                    component=name,
                ),
                StageSpec(
                    stage=Stage.DESIGN,
                    author="functional_designer",
                    critic="functional_design_critic",
                    artifact=f"src/{name}/design.kd",
                    gate_type="design",
                    component=name,
                ),
                StageSpec(
                    stage=Stage.TEST_PLAN,
                    author="test_designer",
                    critic="test_design_critic",
                    artifact=f"src/{name}/test_plan.kd",
                    gate_type="test_plan",
                    component=name,
                ),
                StageSpec(
                    stage=Stage.TEST_CODING,
                    author="test_coder",
                    critic=None,
                    artifact=f"gen/{name}/tests/test_{name}.py",
                    gate_type="test_coding",
                    component=name,
                ),
                StageSpec(
                    stage=Stage.IMPLEMENTATION,
                    author="coder",
                    critic="code_reviewer",
                    artifact=f"gen/{name}/src/{name}.py",
                    gate_type="implementation",
                    component=name,
                ),
            ]
        )
    return tuple(specs)
