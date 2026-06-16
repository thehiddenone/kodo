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

## Windows-specific pitfalls

### `os.kill(pid, 0)` fires a real Ctrl+C on Windows

On Unix, `os.kill(pid, 0)` is the standard idiom for checking whether a process is alive: signal 0 is never delivered, it just probes for the process.

On Windows the semantics are completely different. Python maps `os.kill` to `GenerateConsoleCtrlEvent` for `CTRL_C_EVENT` and `CTRL_BREAK_EVENT`, and to `TerminateProcess` for `SIGTERM`. Critically, `signal.CTRL_C_EVENT == 0`, so `os.kill(pid, 0)` is identical to `os.kill(pid, signal.CTRL_C_EVENT)`. This calls `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`, which broadcasts a real Ctrl+C event to every process sharing the current console session. Python's console control handler receives it, calls `PyErr_SetInterrupt()`, and the interrupt flag sits armed. The call returns True (success), so the caller never sees an exception — the damage is invisible. The queued interrupt fires later, at the next I/O checkpoint inside any `read()` or `write()` call, raising `KeyboardInterrupt` in an apparently unrelated location.

__Rule:__ Never use `os.kill(pid, 0)` to check process liveness. On Windows use `ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)` instead: a non-NULL return value means the process exists; no signal is sent.

```python
@staticmethod
def __is_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
```

### Signal-handler tests must not touch the real signal table

Tests that exercise `signal.signal()`-based code (e.g. `install_signal_handlers`) must mock `signal.signal` via `monkeypatch` rather than calling the real function. The real function changes the process-wide signal table for the entire pytest session. On Windows, replacing the `SIGINT` handler removes asyncio's Ctrl+C wakeup path, causing async tests that run after the signal-handler tests to hang indefinitely inside `asyncio.to_thread` or similar I/O waits.

```python
def test_install_signal_handlers_sets_shutdown_requested(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda signum, h: installed.update({signum: h}))

    lifecycle.install_signal_handlers(lambda: None)

    handler = installed.get(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # type: ignore[operator]
    assert lifecycle.shutdown_requested is True
```

`monkeypatch` is automatically undone after each test with no global side-effects.

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

Authoritative locations:

- The agent-facing tool catalog — internal/external names, descriptions, autonomous-mode behavior, and when-to-use guidance for every tool — lives in each tool's `ToolSpec` under [src/kodo/toolspecs/](src/kodo/toolspecs/) (`external_name`, `user_description`, `description`, `autonomous_mode`, `when_to_use`), rendered into the `## Tools` section of every agent prompt by [src/kodo/subagents/_registry.py](src/kodo/subagents/_registry.py). Prompts name tools; they never restate schemas.
- `publish_artifact`, `read_artifact` — `ToolSpec`s in [src/kodo/toolspecs/_publish_artifact.py](src/kodo/toolspecs/_publish_artifact.py) and [src/kodo/toolspecs/_read_artifact.py](src/kodo/toolspecs/_read_artifact.py), dispatched in-process by [src/kodo/runtime/_subagent_dispatch.py](src/kodo/runtime/_subagent_dispatch.py).
- `escalate_blocker`, `ask_user`, `request_user_review_artifact`, `report_artifact_completed` — `ToolSpec`s in [src/kodo/toolspecs/](src/kodo/toolspecs/), one module per tool (`_escalate_blocker.py`, `_ask_user.py`, `_request_user_review_artifact.py`, `_report_artifact_completed.py`).

The agent's frontmatter `tools:` list MUST include every tool the agent calls.

### Workspace-mediated artifact contract

Sub-agents MUST NOT touch the filesystem directly. There is no `fileio_read_file` or `fileio_write_file` on any sub-agent's frontmatter. All artifact production, reading, revision, and cross-agent routing flows through the workspace.

- __Production__ — authors call `publish_artifact` with the artifact `type` (drawn from the workspace's type enum: `narrative`, `architecture`, `requirements`, `functional-design`, `design-plan`, `tech-stack`, `code`, `test-plan`, `test`, `feedback`), the `project_code` and `responsibility_code` assigned by Architect, and the `content`. There is no separate "I'm done" tool call — the agent simply publishes its best work. The workspace owns directory placement based on (project_code, responsibility_code); `filename_hint` is optional and names only the leaf file.
- __Completion__ — completion is an explicit, authoritative signal, not an inference. The agent that owns the convergence verdict — a critic in an author/critic pair, or a solo agent that has no critic — calls `report_artifact_completed` once an artifact has passed __all__ of its gates: critic acceptance and, in interactive mode, the user's review. From that call the engine promotes the artifact (materialize to `src/`/`gen/`, mirror commit, mark `completed` in the index, move out of the workspace; see [STATE_AND_LIFECYCLE.md §8](../../doc/STATE_AND_LIFECYCLE.md)). The signal is per artifact and is never fired by an author about its own work. Concretely:
  - For an author with a critic, the critic reports completion after its verdict is `accepted` and (interactive mode) the user has accepted the artifact via `request_user_review_artifact`. The author may publish many superseding revisions before the critic accepts.
  - For an author without a critic, the agent is its own owner: it fires `request_user_review_artifact` for the artifact it produced and, on acceptance, `report_artifact_completed`.
  - Narrative Author is a solo agent: it elicits and validates user input via `ask_user`, then for each artifact it produces (the Narrative, then the Tech Stack) fires `request_user_review_artifact` followed by `report_artifact_completed` — one pair per artifact, never bundled.
- __Reading inputs__ — when the engine has not already injected an input artifact's content into the task message, agents call `read_artifact` with filters (`artifact_id`, `responsibility_code`, `type`, `requirement_id`, etc.). At least one filter is required.
- __Revisions__ — when an author addresses critic feedback or user feedback, the new artifact is published with `supersedes: [<old_id>, ...]`. The workspace retires the old artifact; it remains on disk for audit.
- __Critic verdicts__ — critics publish a `feedback` artifact with `reviewed_artifact_id` pointing at the artifact under review, `verdict: "accepted"` or `"rejected"`, and (when rejected) a non-empty `concerns` array. Each concern uses a `kind` from the critic's defined vocabulary; the shared base vocabulary lives in the `publish_artifact` schema description and each critic prompt may extend it.
- __Cross-agent routings__ — when one author challenges another author's artifact (e.g. Coder challenges a `test` artifact owned by Test Coder, or a `functional-design` artifact owned by Functional Designer), the challenger publishes a `feedback` artifact whose `reviewed_artifact_id` points at the artifact under challenge. The engine routes the feedback to the artifact's author. There is no separate "routing" tool — routing is an emergent property of `reviewed_artifact_id` pointing at the right artifact.

### No free-form output

A sub-agent MUST NOT use free-form text for any output the harness has to interpret or that is delivered to the user. Specifically:

- __Verdicts__ — every verdict is a `publish_artifact(type="feedback", verdict=...)` call. Critics MUST NOT signal accept/revise via prose or via convention substrings.
- __Completion__ — production is a `publish_artifact` call; completion is a separate, explicit `report_artifact_completed` call made by the artifact's owner (the critic of an author/critic pair, or a solo agent) once every gate has passed. Authors never report their own work complete, and completion MUST NOT be signalled via prose.
- __Cross-agent routings__ — every routing is a `publish_artifact(type="feedback", reviewed_artifact_id=...)` call. Routings MUST NOT be expressed as embedded prose or markdown blocks.
- __Escalations to the user__ — when an author cannot defensibly make a call (an iteration cap is reached, or a back-and-forth cannot be reconciled), it calls `escalate_blocker` with a structured `reason`, `summary`, and `blocking_artifact_ids` array. This relinquishes the decision to the orchestrator; it is an author/coder-side tool, never held by critics.
- __User dialog__ — agents reach the user only through the common dialog tools, never free-form. `ask_user` elicits or validates a single piece of user-supplied information the agent then acts on itself (used by Narrative Author and by critics gap-filling against user input); `request_user_review_artifact` is a structured sign-off on a finished `artifact_id` (held by critics and solo agents); `report_artifact_completed` marks an artifact done. Which agents hold which tool is set by each agent's frontmatter; `ask_user` is withheld entirely in autonomous mode (no answer to synthesize), and `request_user_review_artifact` auto-accepts.

Intermediate reasoning text the agent emits between tool calls is allowed but unused; the harness ignores it. Sub-agent prompts SHOULD direct the agent to be terse in such text.

### Transport-agnostic tool contract

Every tool a sub-agent calls MUST be defined as a `ToolSpec` with a JSON Schema in [src/kodo/toolspecs/](src/kodo/toolspecs/), one module per tool. All built-in tools run in-process, dispatched by [src/kodo/runtime/_subagent_dispatch.py](src/kodo/runtime/_subagent_dispatch.py) and [src/kodo/runtime/_engine.py](src/kodo/runtime/_engine.py); external MCP tool support is planned post-MVP. The sub-agent prompt MUST name the tool but MUST NOT restate the schema, so prompts remain valid when the transport changes.

### Authoring a new sub-agent prompt — checklist

When adding or revising a sub-agent prompt, the prompt MUST contain, in this order:

1. The agent's name (one line, bold) and a one-sentence purpose statement.
2. __Inputs__ — what the agent reads, by name and source.
3. __Workflow__ — numbered stages or phases, each with concrete actions and stop conditions.
4. __Reporting__ — names of the tools the agent calls, what each one signals, and the order in which they are called over the agent's lifetime. References the tool name only; never restates the schema.
5. __What to Avoid__ — explicit exclusions, including a line that prohibits free-form output for any of the contracts covered by a reporting tool.

The frontmatter MUST list every tool the agent calls, including reporting tools.
