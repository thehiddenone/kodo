---
name: kodo-tool-selection
description: How to choose the tools a Kōdo agent or sub-agent is granted — least privilege, read-only investigators, critics without writing tools, the shared editing-block rule for file-changing tools, approval gating, project-only tools, and the paired grants the loader enforces. Use this skill whenever you write or review the tools list of a Kōdo agent's frontmatter.
---

# Choosing a Kōdo agent's tools

This skill is the judgment part of tool selection. It deliberately holds no
tool catalog: the catalog changes with every Kōdo release. When you hold the
`list_tools` tool, call it for the live catalog — each tool's description,
when to use it, its security impact, its behavior in autonomous mode, and
whether it needs a project or changes files. Otherwise read the tool
descriptions you were given.

## 1. Grant the least the job needs

Every tool in `tools` is one more thing the model can do wrongly. Grant a tool
only when a step of the agent's `## Workflow` calls it, and name that step's
tool in the prompt.

| Agent's job | Grant | Never grant |
|---|---|---|
| Research, review, planning | Reading tools: `read_file`, `find_files`, `find_text_in_files`, `get_root_paths`; `web_search` and `read_webpage` when it needs the web | Anything that changes files or runs commands |
| Critic | Reading tools and `get_findings` | Any tool that changes files: a critic reports findings, its author makes the change |
| Author of documents or code | Reading tools plus `create_file` and `edit_file` | `filesystem` or `run_command` unless a workflow step needs them |
| Builds and tests | `toolchain_build` and `toolchain_deps` | Hand-run build commands through `run_command` when the toolchain tools cover them |
| Coordinator (top-level) | `run_subagent`, `ask_user`, reading tools | The writing tools its sub-agents hold, when it delegates all writing |

## 2. Rules the loader enforces

A grant that breaks one of these makes the agent **broken**:

- A tool that changes files (its `modifies_files` is true: `create_file`,
  `edit_file`, `create_directory`, `filesystem`, `run_command`, `rollback`,
  `scaffold_new_project`, `toolchain_build`, `toolchain_deps`) requires
  `{SHARED:editing}` in the body, and `{SHARED:editing}` is refused without one.
- `run_subagent` and a non-empty `subagents:` list come together or not at all.
- `use_skill` and the `{SKILLS}` token come together or not at all.
- `get_findings` comes with exactly one of `{SHARED:findings_author}` and
  `{SHARED:findings_critic}`.
- Every name must be a real Kōdo tool.

Never list `return_result`: every sub-agent with a spec gets it automatically.

## 3. Behaviors to plan for

- **Approval.** Under the default `smart` security posture, a call below `HIGH`
  security impact runs without asking, and a `HIGH` call (for example
  `run_command`, `filesystem`, `toolchain_deps`) is judged per call and can ask
  the user. The `defensive` posture asks before every call at `MODERATE` and
  above. An agent that calls an asking tool in a loop asks every time, so
  prefer the lower-impact tool that does the job.
- **Autonomous mode.** `ask_user` returns a synthesized answer when the user is
  away. Write the workflow so it proceeds on that answer and records the
  assumption.
- **Project-only tools.** A tool that requires a project (for example
  `run_subagent`, `get_findings`, `guided_dev_status`) fails in a session with
  no project open. An agent meant to work without a project avoids them.
- **Naming.** The lint (`L1 tool-not-granted`) warns when the body names, in
  backticks, a tool the agent is not granted. Describe another agent's tool in
  plain words instead.
