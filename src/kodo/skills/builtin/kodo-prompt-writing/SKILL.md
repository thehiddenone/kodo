---
name: kodo-prompt-writing
description: How to write the prompt body of a Kōdo agent or sub-agent so it passes Kōdo's prompt lint (rules L1–L7) — the one-sentence purpose, the required sections per agent kind, no vague qualifiers, quoting sources by exact identifier, naming tools without restating their schemas, and no free-form output for anything a tool reports. Use this skill whenever you draft, revise or review the prompt text of a Kōdo agent.
---

# Writing a Kōdo agent prompt

This skill holds the prompt-writing rules Kōdo's built-in agents follow and its
lint checks. Every lint finding is a **warning**: the agent still loads and
runs. Fix every warning anyway; each one marks a place where a model reading
the prompt has to guess.

## 1. Structure

### The opening

Line one of the body is the title: a `# Name` heading or a bold `**Name**`
alone on the line. The first paragraph after it is **exactly one sentence**
that names the agent and states its single purpose.

```markdown
# Release Notes Writer

You are **Release Notes Writer**: you turn the commits since the last tag into a user-facing changelog entry.
```

Put everything else — audience, tone, what the agent never does — in the
following paragraphs. Keep the agent on that one purpose; every behavior
outside it goes under `## What to Avoid`.

### Required sections

Use these exact `##` headings, spelled and capitalized as shown. `## How You
Work`, `## Procedure` or `## What to avoid` do not count.

| Kind | `## Inputs` | `## Workflow` | `## Reporting` | `## What to Avoid` |
|---|---|---|---|---|
| Author or solo sub-agent | required | required | required | required |
| Critic (`role: critic`) | required | — | required | required |
| Top-level agent | — | required | — | required |

Order them as in the table: Inputs, Workflow, Reporting, What to Avoid. A
sub-agent also needs `## Purpose` (the `kodo-agent-format` skill), which can
sit before Inputs.

- **Inputs** — every input by its exact field name or artifact label, and
  where it comes from (the task message, an injected file, a `read_file` call).
- **Workflow** — numbered stages or phases. Each stage has concrete actions and
  a stop condition: what has to be true before the next stage starts.
- **Reporting** — the tools the agent reports through, what each call means,
  and the order of calls over the agent's run. For a sub-agent that is
  `return_result`, called once, last.
- **What to Avoid** — explicit exclusions, including the line that forbids
  free-form output for the contracts the reporting tools cover.

## 2. Wording

### No vague qualifiers

Do not write: `almost`, `possibly`, `maybe`, `appropriate`, `reasonable`,
`as needed`, `may`, `could`, `should consider`. Replace each with the
definite rule or a measurable threshold it stood for.

| Vague | Definite |
|---|---|
| "Ask the user if appropriate." | "Ask the user when two readings of the request lead to different files." |
| "Keep summaries reasonably short." | "Keep the summary to one line." |
| "You may read the tests." | "Read the tests before changing the code they cover." |
| "Retry as needed." | "Retry once; on a second failure, report it." |

For permission, write "you can"; for a rule, write "always", "never" or
"must". The lint does not count these words inside code spans, fenced blocks,
`>` block quotes or double-quoted text, so a prompt can quote the list itself.

### Quote sources, name exact identifiers

- Tell the agent to quote the text it relies on or objects to, with the file
  path and line number.
- Refer to requirement IDs, codenames, test IDs, file paths and tool names by
  their exact value, in backticks, never paraphrased.

### Name tools; never restate their schemas

A tool's JSON Schema and description reach the model on the tool itself.
Name the tool (`read_file`, `return_result`) and say when to call it. Never
copy its schema into the prompt, never write a `## Tools` section, and never
restate what a `{SHARED:…}` block already says.

## 3. Output contracts

Anything the engine or the user has to act on goes through a tool, never prose:

| Output | Through |
|---|---|
| A sub-agent's result | One `return_result` call, matching the agent's output schema |
| A file | `create_file` or `edit_file` |
| A critic's findings | `return_result` with `critic_output`, after reading the backlog with `get_findings` |
| A blocker the author cannot resolve | `return_result` with a non-empty `reason` (the `author_output` escalation fields) |
| A question for the user | `ask_user`, one decision per question, recommended option first |

Tell the agent to keep the text between tool calls terse: Kōdo ignores it.

## 4. The lint rules

| Rule | Fires when | Fix |
|---|---|---|
| `L1 tool-not-granted` | A backticked word in the body is a Kōdo tool the agent is not granted. | Grant the tool, or stop naming it. Describe another agent's tool in plain words ("its build tool"). |
| `L2 subagent-not-listed` | The body names `run_subagent_<n>` for an `<n>` not in `subagents:`. An agent's own tool, and a critic's author's tool, are exempt. | List the sub-agent, or remove the mention. Write placeholders as `run_subagent_<name>`. |
| `L3 unknown-agent` | A backticked `kodo_<n>` names no loaded agent, or a backticked user agent name does not load. | Correct the name, or fix the agent it names. |
| `L4 purpose` | The first paragraph after the title is not exactly one sentence, or is a list, heading or table. | Make it one sentence naming the purpose; move the rest to the next paragraph. |
| `L5 sections` | A section the agent's kind requires is missing. | Add the `##` heading spelled as in section 1. |
| `L6 vague-qualifier` | A word from the list in section 2 appears in prose. | Replace it with the definite rule. |
| `L7 schema-restated` | A fenced block holds a JSON Schema (`"type": "object"` with `"properties"`). | Delete it and name the tool. |

Engine-driven agents are exempt from `L5 sections`. Line numbers in findings
count from the top of the file, frontmatter included. The lint reads the
prompt as written, before `{SHARED:…}` blocks are expanded.
