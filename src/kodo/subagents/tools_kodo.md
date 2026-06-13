# Kodo Tools

This is the single source of truth for every tool available to Kodo sub-agents and to the orchestrator. Each tool has:

- **Internal name** — the name agents and the harness use in tool calls.
- **External name** — the user-facing name shown in the UI when a tool call is surfaced to the user.
- **Description** — what the tool does.
- **When to use** — situations and examples that call for this tool.

Subagent prompts do not describe tools. A prompt's frontmatter lists only the internal names it is allowed to call; the behavior, schema, and usage guidance live here.

---

## publish_artifact

- **External name:** Publish Artifact
- **Description:** Publishes a new workspace artifact (narrative, tech-stack, architecture, requirements, design-plan, functional-design, test-plan, test, code, feedback, e2e-test-plan) or a revision of an existing one via `supersedes`. Returns the new `artifact_id`. The harness handles file placement; the caller supplies `type`, `author`, `project_code`, `responsibility_code`, `content`, and any type-specific fields (`requirement_ids`, `reviewed_artifact_id`, `verdict`, `concerns`, `filename_hint`, `metadata`, `supersedes`).
- **When to use:**
  - An author agent (Architect, Requirements Author, Functional Designer, Test Designer, Test Coder, Coder, End-to-End Test Designer, Narrative Author) has drafted or revised a document/code artifact and is ready for review or acceptance.
  - A critic agent (Architect Critic, Requirements Critic, Functional Design Critic, Code Reviewer, End-to-End Test Design Critic) has finished reviewing and needs to record a `feedback` artifact with a `verdict` and `concerns`.
  - An agent needs to route a concern to another agent's artifact (e.g., Coder flagging a suspected test bug to Test Coder, or End-to-End Test Designer raising a `missing_test_seam` finding against a Functional Design or the architecture document) — done by publishing a `feedback` artifact whose `reviewed_artifact_id` points at the targeted artifact.
  - Republishing a revised version of an artifact after addressing critic findings or user feedback — always with `supersedes: [<prior_artifact_id>]`.

## read_artifact

- **External name:** Read Artifact
- **Description:** Fetches an artifact the harness has not already injected inline as task input. Filter by `artifact_id`, or by `(project_code, type)` / `(project_code, responsibility_code, type)` to find a specific published artifact (e.g., another component's Functional Design, a prior feedback artifact, or a superseded version).
- **When to use:**
  - An agent needs an input artifact that wasn't delivered inline — e.g., Coder fetching another component's Functional Design via `read_artifact(project_code=<PROJECTCODE>, responsibility_code=<OTHER_CODENAME>, type="functional-design")`.
  - A critic needs to check consistency with its own prior feedback on a predecessor artifact, e.g. `read_artifact(reviewed_artifact_id=<predecessor_id>, author="architect_critic")`.
  - End-to-End Test Designer fetching a specific component's Functional Design or the architecture's End-to-End Testability section.
  - Narrative Author re-examining a previously published Narrative or Tech Stack while handling feedback.

## escalate_to_user

- **External name:** Escalate to User
- **Description:** Raises a blocking issue to the user with a structured `reason`, `summary`, and `blocking_artifact_ids` (and sometimes `outstanding_findings`). The user's resolution arrives as the agent's next input. This is an author/coder-side tool — critics do not have it.
- **When to use:**
  - Inputs are too under-specified to make a defensible call (e.g., Architect cannot construct a "why it is single" argument, Requirements Author cannot write an unambiguous requirement, Test Designer cannot derive a behavioral test).
  - The orchestrator ends an author/critic or reviewer loop without convergence and the critic is still rejecting (`reason: "critic_iteration_cap"` / `"reviewer_iteration_cap"`).
  - User feedback at an approval gate contradicts upstream artifacts or itself in a way the agent cannot resolve (`reason: "feedback_contradiction"`).
  - Coder's own Stage 3 red/green loop stops converging (`reason: "test_iteration_cap"`), or the Coder/Test Coder exchange ends without agreement (`reason: "test_coder_disagreement"`).
  - Functional Designer hits DAG validation failures (`reason: "dag_validation_failed"`) or a reopen cascade beyond two designs (`reason: "reopen_cascade"`).

## toolchain_build

- **External name:** Build Project
- **Description:** Compiles or builds the project in the language/tooling declared by the Tech Stack. Returns success or a list of build errors.
- **When to use:**
  - Coder, after publishing new or superseding `code` artifacts in Stage 2, to confirm the project builds before running tests.
  - After any refactor change in Stage 4, to confirm the build still succeeds before re-running tests.

## toolchain_test

- **External name:** Run Tests
- **Description:** Runs the component's test suite and returns the execution log — pass/fail status per test, error codes, assertion failures, stack traces.
- **When to use:**
  - Coder, after a successful build, to check whether tests pass and to diagnose failures (implementation bug vs. test bug vs. spec ambiguity).
  - After each refactor change, to confirm tests remain green.
  - After addressing Code Reviewer feedback or user feedback that touches code, to confirm tests still pass (or to detect that feedback breaks tests, triggering `escalate_to_user` with `reason: "feedback_breaks_tests"`).

## toolchain_deps

- **External name:** Manage Dependencies
- **Description:** Adds, removes, or updates project dependencies in the project's dependency configuration. The only sanctioned way to change dependency files — agents do not edit them directly.
- **When to use:**
  - Coder needs a new library (database driver, HTTP client, message queue client, parser, etc.) before referencing it in an implementation.
  - A dependency is no longer needed and should be removed, or an existing dependency needs a version bump required by the implementation.

## narrative_ask_user_question

- **External name:** Ask Narrative Question
- **Description:** Asks the user exactly one focused clarifying question, tagged with `phase` (`"narrative"` or `"tech_stack"`) and `covers_points` (which of the seven Narrative points or which Tech Stack field the question targets). Only Narrative Author has this tool.
- **When to use:**
  - During initial gap-filling (Phase A.2), to fill in the single most important uncovered or partially-covered Narrative understanding point (Customer, Problem, Primary function, Integrations, Deployment model, Operations, North Star).
  - During Tech Stack derivation (Phase B.2), to resolve a Tech Stack field the Narrative does not imply.
  - When user feedback on the Narrative or Tech Stack reveals a contradiction that must be resolved before incorporating the feedback — one question per contradiction, one call per turn.

## narrative_present_for_acceptance

- **External name:** Present for Acceptance
- **Description:** Presents a just-published artifact (`artifact_kind: "narrative"` or `"tech_stack"`) to the user for accept/feedback by its `artifact_id`. The user's response arrives as the next input.
- **When to use:**
  - Immediately after publishing a Narrative draft (Phase A.3/A.4) or a revised Narrative.
  - Immediately after publishing a Tech Stack draft (Phase B.3/B.4) or a revised Tech Stack.
  - Always called on the `artifact_id` from the immediately preceding `publish_artifact` call — never on an older artifact.

## narrative_report_completed

- **External name:** Report Narrative Complete
- **Description:** Signals that the Narrative Author run is finished, carrying the `narrative_artifact_id` and `tech_stack_artifact_id` of the latest accepted artifacts. This is the only signal the engine treats as "Narrative Author done." No further tool calls or text follow it.
- **When to use:**
  - Exactly once, after both the Narrative and the Tech Stack have been accepted by the user (Phase B.5). Never called before both acceptances have happened.

## compute_frontier

- **External name:** Review Workspace
- **Description:** Read-only. Returns, per codename and per requirement, which artifacts are done, in progress, or missing — the ground truth for what stage each part of the product has reached.
- **When to use:**
  - Kodo calls this before every scheduling decision — the first step of the core loop, every time, including after invalidation cascades or when the user brings pre-existing artifacts into the workspace.
  - To determine the furthest stage each codename can advance to, and to confirm that an invalidation cascade has correctly marked downstream artifacts as missing.

## list_artifacts

- **External name:** List Artifacts
- **Description:** Read-only. Lists existing artifacts in the workspace and their states (e.g., draft, accepted, superseded).
- **When to use:**
  - Kodo needs a broader inventory view than `compute_frontier` provides — e.g., to enumerate all artifacts for a codename, find superseded versions, or audit workspace state during diagnosis of a non-converging loop.

## run_subagent

- **External name:** Run Sub-Agent
- **Description:** Invokes a solo sub-agent that has no critic loop — currently Narrative Author, and Test Coder in its solo (stub + test generation) stage.
- **When to use:**
  - Kicking off Narrative Author at the start of a project.
  - Invoking Test Coder's solo stage to produce stubs and tests from an accepted Test Plan.

## run_author_critic_iteration

- **External name:** Run Author/Critic Round
- **Description:** Invokes exactly one round of an author/critic (or reviewer) loop — the author revises (or publishes for the first time) and the critic reviews. Returns that round's outcome (findings remaining, findings resolved, escalation raised). Call repeatedly to iterate; Kodo decides how many rounds to run per loop.
- **When to use:**
  - Every stage with an author/critic pairing: Architect ↔ Architect Critic, Requirements Author ↔ Requirements Critic, Functional Designer ↔ Functional Design Critic, Coder ↔ Code Reviewer, Test Coder ↔ Code Reviewer, End-to-End Test Designer ↔ End-to-End Test Design Critic.
  - Kodo calls this repeatedly within its per-loop iteration budget (a sensible default is up to 5 rounds), stopping early when findings converge or when findings stop decreasing (treating the latter as non-convergence).

## request_user_approval

- **External name:** Request Approval
- **Description:** Surfaces an acceptance gate to the user for a specific artifact. Blocks until the user responds (accept or feedback). Used in interactive mode at artifact-acceptance points.
- **When to use:**
  - An author/critic loop has converged (critic verdict `accepted`) and the artifact is ready for the user's sign-off — e.g., after Architect Critic accepts the architecture document, or after Functional Design Critic accepts a component's design.
  - Before executing a large invalidation cascade (more than one codename's worth of downstream artifacts) in interactive mode.

## ask_user

- **External name:** Ask User
- **Description:** Surfaces a question to the user and blocks until they respond. Used for escalation triage and substantive judgment calls in interactive mode.
- **When to use:**
  - Triaging a substantive escalation in interactive mode — a judgment call about what the product should do, or which of two deadlocked positions is correct.
  - Resolving an ambiguous rework target (an upstream contradiction that could be fixed on either side) in interactive mode.
  - Confirming a `rollback` before executing it, in either mode.
  - Presenting the root-cause diagnosis after pipeline-level cycle detection fires (alongside `disable_autonomous_mode`).

## rollback

- **External name:** Rollback Project
- **Description:** Restores the project workspace to a prior checkpoint. Always requires user confirmation first via `ask_user` or `request_user_approval` — never silent, in any mode.
- **When to use:**
  - Rework-in-place would be worse than starting a stage over — typically after a root-cause resolution invalidates a large frontier and a checkpoint predates the contaminated work.

## disable_autonomous_mode

- **External name:** Disable Autonomous Mode
- **Description:** Break-glass tool. Forces the pipeline into interactive mode. Once pulled, autonomous mode stays off until the user explicitly re-enables it.
- **When to use:**
  - Only for diagnosed pipeline-level non-convergence — the same artifact (or pair of artifacts) reworked repeatedly (as a guideline, 3+ rework cycles without net progress) with a root cause that requires the user's intent to resolve.
  - Never for ordinary, single-loop escalations — those are triaged normally (procedurally or substantively) without pulling the break-glass.

## post_update

- **External name:** Post Progress Update
- **Description:** Sends a non-blocking progress update to the UI. Describes state transitions and decisions — never artifact content (no requirement text, design excerpts, or code).
- **When to use:**
  - A stage starts or completes for a codename, or a product-level stage starts or completes.
  - An escalation is triaged, an invalidation cascade executes, a substantive autonomous decision is made, or the break-glass is pulled.
  - Recording that stage 8 (end-to-end testing) is skipped because the Architect's verdict was `excluded`.
