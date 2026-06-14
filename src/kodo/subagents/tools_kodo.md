# Kodo Tools

This is the single source of truth for every tool available to Kodo sub-agents and to the orchestrator. Each tool has:

- **Internal name** — the name agents and the harness use in tool calls.
- **External name** — the user-facing name shown in the UI when a tool call is surfaced to the user.
- **Description** — what the tool does.
- **Autonomous mode** *(optional)* — how the tool behaves when the user is away. Two values matter to the engine: `unavailable` (the tool is withheld entirely — excluded from the agent's tool list and its rendered `## Tools` section) and `auto-accepted` (the tool stays available but the engine synthesizes the user's response). A tool with no such field is available and behaves identically in both modes.
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

## escalate_blocker

- **External name:** Escalate Blocker
- **Description:** Hands a blocking issue the agent cannot defensibly resolve to the orchestrator, with a structured `reason`, `summary`, and `blocking_artifact_ids` (and sometimes `outstanding_findings`). The orchestrator owns the resolution: it triages procedurally, makes the call itself in autonomous mode, or — in interactive mode — opts to surface the matter to the user via `ask_user`. The resolution arrives as the agent's next input. Use this only when the agent has relinquished the decision; for an input or clarification the agent can act on itself, use `ask_user` instead. This is an author/coder-side tool — critics do not have it. Available in both interactive and autonomous mode.
- **When to use:**
  - Inputs are too under-specified to make a defensible call (e.g., Architect cannot construct a "why it is single" argument, Requirements Author cannot write an unambiguous requirement, Test Designer cannot derive a behavioral test).
  - The orchestrator ends an author/critic or reviewer loop without convergence and the critic is still rejecting (`reason: "critic_iteration_cap"` / `"reviewer_iteration_cap"`).
  - User feedback at a review gate contradicts upstream artifacts or itself in a way the agent cannot resolve (`reason: "feedback_contradiction"`).
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
  - After addressing Code Reviewer feedback or user feedback that touches code, to confirm tests still pass (or to detect that feedback breaks tests, triggering `escalate_blocker` with `reason: "feedback_breaks_tests"`).

## toolchain_deps

- **External name:** Manage Dependencies
- **Description:** Adds, removes, or updates project dependencies in the project's dependency configuration. The only sanctioned way to change dependency files — agents do not edit them directly.
- **When to use:**
  - Coder needs a new library (database driver, HTTP client, message queue client, parser, etc.) before referencing it in an implementation.
  - A dependency is no longer needed and should be removed, or an existing dependency needs a version bump required by the implementation.

## request_user_review_artifact

- **External name:** Request Review
- **Description:** Presents a converged, just-published artifact to the user for accept/feedback by its `artifact_id`. Here the user acts as **critic**, judging a finished artifact. Blocks until the user responds; accept ends the review gate, feedback opens a revision round. Held by **critics and solo agents** — the agent that owns the convergence verdict — never by an author of an author/critic pair. Always called on the `artifact_id` of the artifact the loop just converged on.
- **Autonomous mode:** auto-accepted — when the user is away, the engine synthesizes an accept and returns immediately, so the caller fires it unconditionally without branching on mode.
- **When to use:**
  - A critic's verdict on the author's artifact is `accepted` and the artifact is ready for the user's sign-off (e.g., after Architect Critic accepts the architecture document, or Functional Design Critic accepts a component's design).
  - A solo agent (Narrative Author) has published an artifact it considers ready and wants the user — who is the real author of the underlying information — to confirm the synthesis captures their intent.
  - Never used to ask whether an artifact "looks ok" mid-draft — that is what `ask_user` is for. This tool is a structured sign-off on a specific, finished `artifact_id`.

## report_artifact_completed

- **External name:** Report Artifact Complete
- **Description:** Marks one artifact as having passed **all** of its gates — critic acceptance and, in interactive mode, user review — so it is good to go. This is the explicit, authoritative completion signal: from this point `query_frontier` reports the artifact as completed and the engine promotes it. Reported per artifact. Held by **critics and solo agents**; an author never reports its own work complete.
- **When to use:**
  - A critic, immediately after its verdict is `accepted` and (in interactive mode) the user has accepted the artifact via `request_user_review_artifact`.
  - A solo agent, once an artifact it produced has cleared its review gate — Narrative Author fires it for the Narrative and again for the Tech Stack (one call per artifact), never bundling the two.
  - Never before every gate condition for that artifact has been met; publishing an artifact does not make it complete.

## query_frontier

- **External name:** Review Workspace
- **Description:** Read-only. Queries and returns the most recent status of every artifact — per codename and per requirement, which artifacts are **completed**, **in flight** (published but not yet through critic and/or user review), or **missing**. It does not compute or decide completion: an artifact counts as completed only once an agent has marked it so via `report_artifact_completed`. This is the ground truth for what stage each part of the product has reached.
- **When to use:**
  - Kodo calls this before every scheduling decision — the first step of the core loop, every time, including after invalidation cascades or when the user brings pre-existing artifacts into the workspace.
  - To determine the furthest stage each codename can advance to, to discover artifacts still in flight, and to confirm that an invalidation cascade has correctly marked downstream artifacts as missing.

## list_artifacts

- **External name:** List Artifacts
- **Description:** Read-only. Lists existing artifacts in the workspace and their states (e.g., draft, accepted, superseded).
- **When to use:**
  - Kodo needs a broader inventory view than `query_frontier` provides — e.g., to enumerate all artifacts for a codename, find superseded versions, or audit workspace state during diagnosis of a non-converging loop.

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

## ask_user

- **External name:** Ask User
- **Description:** Asks the user one focused question and blocks until they respond. Here the user acts as the **source/author** of information the agent needs and can then act on itself — the asking agent retains ownership of its task and applies the answer. Used both for an agent eliciting or validating user-supplied information (acting as a critic of the user's input, raising one concern at a time and driving its resolution) and for the orchestrator's substantive judgment calls and confirmations. One focused question per call; never bundle. Distinct from `escalate_blocker` (which relinquishes a decision the agent cannot make) and from `request_user_review_artifact` (which is a sign-off on a finished artifact, not a question).
- **Autonomous mode:** unavailable — there is no answer to synthesize when the user is away, so this tool is withheld entirely. An agent that would have asked must instead assume-and-document or, if blocked, `escalate_blocker`.
- **When to use:**
  - Eliciting the single most important uncovered or partially-covered piece of information during gap-filling, or resolving a contradiction in user-supplied input before incorporating it — one concern per call.
  - Triaging a substantive escalation in interactive mode — a judgment call about what the product should do, or which of two deadlocked positions is correct.
  - Resolving an ambiguous rework target (an upstream contradiction that could be fixed on either side) in interactive mode.
  - Confirming a `rollback`, or confirming a large invalidation cascade (more than one codename's worth of downstream artifacts), before executing it — interactive mode only.
  - Presenting the root-cause diagnosis after pipeline-level cycle detection fires (alongside `disable_autonomous_mode`).

## rollback

- **External name:** Rollback Project
- **Description:** Restores the project workspace to a prior checkpoint. In interactive mode it requires user confirmation first via `ask_user` — never silent. In autonomous mode the orchestrator makes the call itself and documents it via `post_update`; the user is away, so there is no confirmation to wait on.
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
