---
name: kodo-pipeline-patterns
description: Recipes for composing Kōdo agents into a pipeline — a plain sub-agent, an author ↔ critic review loop, user sign-off (user_review), a planner whose result becomes the session plan, a standalone specialist, and a per-component pipeline stage — with the frontmatter and spec each one needs. Use this skill whenever you decide how a new Kōdo agent or set of agents fits together, before writing their files.
---

# Kōdo pipeline patterns

Each recipe below is a combination of frontmatter keys and spec fields. The
`kodo-agent-format` skill defines every key; this skill says which ones to
combine for which job. Pick the smallest recipe that does the job: every extra
agent is one more prompt to keep correct.

## Choosing

| You need | Recipe |
|---|---|
| A top-level agent that does the work itself | No sub-agents. Grant the tools it needs. |
| One delegated job with a typed result | 1. Plain sub-agent |
| A document or code file that must pass a review | 2. Author ↔ critic |
| The user to approve a file before it counts | 3. User sign-off |
| A plan the caller then executes step by step | 4. Planner |
| A specialist called on demand, outside any order | 5. Standalone specialist |
| One run per component of the product | 6. Per-component stage |

A sub-agent runs in its own context and returns only its result, so delegate
work that needs a large amount of reading the caller would otherwise hold.
Keep work the caller already knows how to do in the caller.

## 1. A plain sub-agent

Invoked, does one job, returns once.

- Caller: `run_subagent` in `tools`, the sub-agent's name in `subagents:`.
- Sub-agent: `name`, `display_name`, `capability`, `tools`, and a `## Purpose`
  section written in the third person for a caller deciding whether to
  delegate.
- Spec: `raw` input and output for a contract of its own; `pipeline_input`
  and `author_output` when it reads and writes project files by artifact role.

## 2. An author ↔ critic review loop

- The author declares `critic: <critic name>`.
- The critic declares `role: critic`, uses the `critic_output` shape, and has
  no `## Purpose` (nobody delegates to it).
- Both grant `get_findings`. The author's body includes
  `{SHARED:findings_author}`; the critic's includes `{SHARED:findings_critic}`.
- The caller lists only the author. Its `run_subagent_<author>` call runs the
  whole loop: author, critic, revisions, until the findings backlog is empty,
  the round budget (`max_rounds`, default 5) is spent, or a round makes no
  progress. The result carries a `review` block with the `outcome`.
- Give the critic a `### Concern vocabulary` section listing the finding kinds
  it uses, and no tool that writes files.
- Use `{PHASE:initial}` … `{/PHASE}` and `{PHASE:revision}` … `{/PHASE}` in the
  author when its first pass and its revision passes need different text.

## 3. User sign-off

`user_review: true` on an author puts its finished work product to the user
before it is accepted; a rejection becomes a finding in the same backlog. It
combines with `critic:` (critic first, then the user) or stands alone.

- The author grants `get_findings` and includes `{SHARED:findings_author}`.
- Its spec `produces` at least one role: without a work product there is
  nothing to sign off, and the flag is refused.

## 4. A planner

`planner: true` makes the sub-agent's result the session's plan. The caller
then tracks it with `get_plan` and `plan_step_forward` instead of restating
it.

- The output schema (`raw`) declares `codebase_context` (a string) and `tasks`
  (an array of objects, each with a `title`). Add `plan_warranted` and
  `reason` so the planner can say the work is one indivisible step.
- No `produces` or `consumes`: a plan is not a work product.
- The caller holds `get_plan` and `plan_step_forward` and its prompt says how
  to step through the tasks.
- The planner gets read-only tools: it plans and never implements.

## 5. A standalone specialist

`standalone: true` marks an agent outside the ordered pipeline, with no
dependency on another stage's output: a researcher, an auditor, a formatter.
The generated tool description tells the caller that ordering does not apply.
A read-only investigator grants only reading tools (`read_file`,
`find_files`, `find_text_in_files`, `get_root_paths`, `web_search`,
`read_webpage`).

## 6. A per-component stage

A stage that runs once per component of the product:

- input shape `pipeline_input` with `"require_responsibility": true`, which
  adds the `responsibility_code` field the caller fills in;
- `consumes` entries with scope `self` for this component's own documents and
  `dependencies` (`"required": false`) for its neighbours';
- `component_paths` set to an output field holding `{codename: path}`, only
  when one run writes files for several components.

Product-level stages and every critic leave `require_responsibility` out.

## Wiring the stages together

- Callers never pass file paths. Declare what each stage needs in `consumes`
  and what it writes in `produces`; the engine hands each stage its files and
  refuses a spawn whose required input does not exist yet.
- Every consumed role must be produced by some agent.
- Write the stage order into the top-level agent's `## Workflow`: the engine
  enforces inputs, not order.
- A user pipeline can reuse built-in sub-agents (`kodo_investigator`,
  `kodo_developer`, `kodo_code_critic`) by listing them.
