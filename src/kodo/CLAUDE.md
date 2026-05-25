# This file contains general coding guidelines for Claude Code

## Class definition

Always model a class by splitting it into private and public members. Public members can be properties and methods, private members can be variables and methods.

Always name-mangle private members of a class. If necessary, provide access to private member variables through read-only properties. You MUST avoid implementation of member variables that need to be modified outside of the class as this is indicative of bad design/architecture.

Never use name-mangled symbols from outside the class.

Add Google Style docstrings to `__init__()`, public methods, and the class itself.

Example:

```python
class Example:
    """This is an example of class definition."""
    __var1: int
    __var2: str

    def __init__(self, var1: int) -> None:
        """Docstring for constructor

        Args:
            var1 (int): bla bla bla
        """
        self.__var1 = var1
        self.__var2 = str(var1 + 1)

    @property
    def var1(self) -> int:
        return self.__var1

    def method(self, arg: str) -> str:
        """Docstring for method

        Args:
            var1 (int): bla bla bla

        Returns:
            str: bla bla bla

        """
        return self.__var2 + arg

    def __private_method(self) -> None:
        pass

```

## Module definition

Always create __init__.py and py.typed in every python module. Always use relative imports from files within the module. Never use relative imports when importing from other packages.

Always prepend underscore to python files inside a package.

Always define `__all__` in `__init__.py`

Always put ```from __future__ import annotations``` at the top of each python source code file except for `__init__.py` files.

Always provide a Google Style docstring for a module.

Never use star import: ```from somewhere import *  # Never do that!```

Example:

```python
"""This is an example module"""

from ._local_file import Something
from another_package.subpackage import Other

__all__ = [
    'Something',
    'Other',
]
```

## Imports

You MUST NOT use star imports. This is not allowed: `from module import *`.

You MUST NOT import from python files. You MUST ALWAYS import from modules. This is not allowed: `from module.submodule._file import MyClass`.

## Type hints

You MUST NOT use `Optional`, `Any`, `Dict`, `List`, `TYPE_CHECKING` etc. No loose type safety, no outdated classes.

You MUST use `object` instead of `Any`, and use `cast` or `isinstance` where necessary.

One exception for the "no Any" rule: you are ALLOWED to use `Any` in definitions of kwargs, as this is the only way to make it work.

You MUST use `typename | None` instead of `Optional`.

## Test implementation

Design tests and classes such that unit tests validate behavior, not implementation details. It should not matter how many times a mock was called if the behavior is correct.

Never build tests that rely on private methods or variables.

Test private methods through behavoir of public methods.

## Prompts

When user asks you to review, edit, or generate an LLM prompt, you MUST follow these rules.

### Drafting style

You MUST generate the prompt in a way that minimizes hallucinations. Require the sub-agent to quote source material when it cites it, and to reference identifiers (requirement IDs, codenames, test IDs, file paths with line numbers) by their exact value rather than paraphrasing.

You MUST be specific. You MUST NOT use vague qualifiers such as "almost", "possibly", "maybe", "appropriate", "reasonable", "as needed", "may", "could", or "should consider". Replace each with a definite statement of the rule or with a measurable threshold.

You MUST state the sub-agent's purpose in a single sentence at the top of the prompt, immediately after the agent's name. The prompt MUST keep the sub-agent on that single purpose. Any behavior that falls outside the stated purpose is to be explicitly excluded under "What to Avoid".

### Inputs, outputs, and reporting contract

Every sub-agent MUST have three things defined in its prompt:

1. __The prompt__ — the system prompt itself, including purpose, inputs, workflow, output structure, and exclusions.
2. __The task__ — the per-invocation user message the engine sends, carrying the workspace artifact IDs and inline content the agent reads as input.
3. __The reporting tools__ — the workspace tools (`publish_artifact`, `read_artifact`) and, where applicable, the user-dialog and escalation tools. Tool names are named in the prompt; their JSON schemas live in code. The prompt MUST NOT restate any schema, so the prompt and the schema cannot drift.

Authoritative schema locations:

- `publish_artifact`, `read_artifact` — [schemas/publish_artifact.json](../../schemas/publish_artifact.json), [schemas/read_artifact.json](../../schemas/read_artifact.json), served by the workspace MCP server in [src/kodo/tools/workspace/](src/kodo/tools/workspace/).
- `escalate_to_user`, `narrative_ask_user_question`, `narrative_present_for_acceptance`, `narrative_report_completed` — [src/kodo/tools/_report_tools.py](src/kodo/tools/_report_tools.py).

The agent's frontmatter `tools:` list MUST include every tool the agent calls.

### Workspace-mediated artifact contract

Sub-agents MUST NOT touch the filesystem directly. There is no `fileio_read_file` or `fileio_write_file` on any sub-agent's frontmatter. All artifact production, reading, revision, and cross-agent routing flows through the workspace.

- __Production__ — authors call `publish_artifact` with the artifact `type` (drawn from the workspace's type enum: `narrative`, `architecture`, `requirements`, `functional-design`, `design-plan`, `tech-stack`, `code`, `test-plan`, `test`, `feedback`), the `project_code` and `responsibility_code` assigned by Architect, and the `content`. There is no separate "I'm done" tool call — the agent simply publishes its best work. The workspace owns directory placement based on (project_code, responsibility_code); `filename_hint` is optional and names only the leaf file.
- __Completion__ — what counts as "this author's contribution is done" is determined by the orchestration, not by the agent. The agent never reasons about completion. Two rules apply at the orchestration level:
  - For an author with a critic, the contribution is complete when the critic publishes a `feedback` artifact with `verdict: "accepted"` targeting the author's published artifact. The author may publish many superseding revisions before that happens.
  - For an author without a critic, the published artifact is the contribution; the orchestration advances the workflow on publish.
  - Narrative Author is a special case: it has no critic but does have user dialog, so its run ends with an explicit `narrative_report_completed` call once both phases are user-accepted.
- __Reading inputs__ — when the engine has not already injected an input artifact's content into the task message, agents call `read_artifact` with filters (`artifact_id`, `responsibility_code`, `type`, `requirement_id`, etc.). At least one filter is required.
- __Revisions__ — when an author addresses critic feedback or user feedback, the new artifact is published with `supersedes: [<old_id>, ...]`. The workspace retires the old artifact; it remains on disk for audit.
- __Critic verdicts__ — critics publish a `feedback` artifact with `reviewed_artifact_id` pointing at the artifact under review, `verdict: "accepted"` or `"rejected"`, and (when rejected) a non-empty `concerns` array. Each concern uses a `kind` from the critic's defined vocabulary; the shared base vocabulary lives in the `publish_artifact` schema description and each critic prompt may extend it.
- __Cross-agent routings__ — when one author challenges another author's artifact (e.g. Coder challenges a `test` artifact owned by Test Coder, or a `functional-design` artifact owned by Functional Designer), the challenger publishes a `feedback` artifact whose `reviewed_artifact_id` points at the artifact under challenge. The engine routes the feedback to the artifact's author. There is no separate "routing" tool — routing is an emergent property of `reviewed_artifact_id` pointing at the right artifact.

### No free-form output

A sub-agent MUST NOT use free-form text for any output the harness has to interpret or that is delivered to the user. Specifically:

- __Verdicts__ — every verdict is a `publish_artifact(type="feedback", verdict=...)` call. Critics MUST NOT signal accept/revise via prose or via convention substrings.
- __Completion__ — every completion is a `publish_artifact` call for the produced artifact (or the final `narrative_report_completed` call for Narrative Author). There is no separate completion tool.
- __Cross-agent routings__ — every routing is a `publish_artifact(type="feedback", reviewed_artifact_id=...)` call. Routings MUST NOT be expressed as embedded prose or markdown blocks.
- __Escalations to the user__ — when an iteration cap is reached or a back-and-forth cannot be reconciled, the agent calls `escalate_to_user` with a structured `summary` and `blocking_artifact_ids` array.
- __User dialog__ — sub-agents that talk to the user (today, only Narrative Author) MUST do so through dedicated dialog tools (`narrative_ask_user_question`, `narrative_present_for_acceptance`, `narrative_report_completed`). No other sub-agent calls a user-dialog tool — they reach the user only via `escalate_to_user`.

Intermediate reasoning text the agent emits between tool calls is allowed but unused; the harness ignores it. Sub-agent prompts SHOULD direct the agent to be terse in such text.

### Transport-agnostic tool contract

Every tool a sub-agent calls MUST be defined in code as a `ToolSpec` with a JSON Schema, or in the canonical schema files under [schemas/](../../schemas/). The engine exposes tools through whatever transport it currently supports (today: inline in [src/kodo/workflow/_engine.py](src/kodo/workflow/_engine.py) for the simple tools, MCP stdio server for the workspace tools under [src/kodo/tools/workspace/](src/kodo/tools/workspace/); planned: all tools served via MCP). The sub-agent prompt MUST name the tool but MUST NOT restate the schema, so prompts remain valid when the transport changes.

### Authoring a new sub-agent prompt — checklist

When adding or revising a sub-agent prompt, the prompt MUST contain, in this order:

1. The agent's name (one line, bold) and a one-sentence purpose statement.
2. __Inputs__ — what the agent reads, by name and source.
3. __Workflow__ — numbered stages or phases, each with concrete actions and stop conditions.
4. __Reporting__ — names of the tools the agent calls, what each one signals, and the order in which they are called over the agent's lifetime. References the tool name only; never restates the schema.
5. __What to Avoid__ — explicit exclusions, including a line that prohibits free-form output for any of the contracts covered by a reporting tool.

The frontmatter MUST list every tool the agent calls, including reporting tools.
