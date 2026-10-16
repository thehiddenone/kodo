---
name: kodo-agent-format
description: The file format of a Kōdo agent — bundle layout under ~/.kodo/agents, naming rules, every frontmatter key, the top-level <name>.json config, the sub-agent spec JSON and its schema shapes, and the shared-block, skills-catalog and phase tokens. Use this skill whenever you write, edit, copy or review the files of a Kōdo agent or sub-agent, before you write the first file.
---

# Kōdo agent format

This skill is the reference for the files that make a Kōdo agent. Every rule
here is enforced by the agent loader: a file that breaks one is listed as a
**broken** agent and does not run. The deterministic lint (the
`kodo-prompt-writing` skill) is separate and only warns.

## 1. Two kinds of agent

| | Top-level agent | Sub-agent |
|---|---|---|
| Who starts it | The user, by picking it in the agent picker | Another agent, through its `run_subagent_<name>` tool |
| Files | `agent_<name>.md` + `<name>.json` (config) | `subagent_<name>.md` + `<name>.json` (spec) |
| Typed contract | None: it talks to the user | Required: an input and an output JSON Schema |
| Ends with | Its reply to the user | One `return_result` call, granted automatically |
| `## Purpose` section | Not read | Required when any caller lists it: its text is the caller's tool description |

When a human picks it, it is a top-level agent. When an agent delegates to it,
it is a sub-agent.

## 2. Layout and names

```text
~/.kodo/agents/
  <name>/                    one directory per top-level agent
    <name>.json              its config (section 4)
    agent_<name>.md          its prompt
  subagents/                 every user sub-agent, shared by all user agents
    <name>/
      subagent_<name>.md     its prompt
      <name>.json            its spec (section 5)
```

- The directory name, the file stems and the `name` inside each file are the
  same string.
- A directory holds exactly one prompt file.
- **`kodo_` is reserved** for built-in agents. A user agent whose name starts
  `kodo_` is refused.
- **`subagents` is reserved**: it is the shared sub-agent directory, so no
  top-level agent can take that name.
- Use lowercase letters, digits and underscores, starting with a letter. The
  name becomes part of the tool name `run_subagent_<name>`, which is limited
  to 64 characters.
- A user agent can list built-in sub-agents (`kodo_investigator`,
  `kodo_coder`) in `subagents:`, and a user sub-agent can name a built-in
  critic. A built-in agent never lists a user sub-agent.

## 3. Frontmatter

A prompt file starts with a YAML frontmatter block between two `---` lines,
then the prompt body. The loader reads these keys and ignores any other:

| Key | Applies to | Meaning |
|---|---|---|
| `name` | both | **Required.** Equals the filename stem after `agent_`/`subagent_`. |
| `display_name` | both | The name shown in the UI. Defaults to the title-cased `name`. |
| `capability` | both | `max`, `high`, `medium` or `low`. Default `medium`. Any other value reads as `medium`. |
| `tools` | both | The tool names the agent is granted. Each one must be a real Kōdo tool. |
| `subagents` | both | The sub-agents this agent can spawn, in the order their tools are built. Requires the `run_subagent` tool in `tools`, and `run_subagent` requires `subagents`. |
| `critic` | sub-agent | Names this author's critic. The caller's `run_subagent_<name>` then runs an author ↔ critic review loop. The named agent must declare `role: critic`. |
| `role` | sub-agent | `critic` or absent. A critic gets no tool of its own; the engine spawns it inside its author's call. A critic cannot declare `critic`, `user_review` or `planner`. |
| `standalone` | sub-agent | `true` for an on-demand specialist outside the ordered pipeline. |
| `user_review` | sub-agent | `true` puts the finished work product to the user for sign-off. Refused on a critic and on a spec that `produces` nothing. |
| `planner` | sub-agent | `true` makes the result the session's plan. The output schema must declare `tasks` (an array of objects with a `title`) and `codebase_context` (a string). |
| `version` | both | Free-form version text, shown at install time and never compared. |

`return_result` is granted to every sub-agent with a spec. Do not list it.

### Granting tools

- `get_findings` belongs to both halves of an author ↔ critic pair, and to any
  author with `user_review: true`.
- `use_skill` requires the `{SKILLS}` token in the body (section 6).
- Granting any tool that changes files (`create_file`, `edit_file`,
  `create_directory`, `filesystem`, `run_command`, `rollback`,
  `scaffold_new_project`, `toolchain_build`, `toolchain_deps`) requires
  `{SHARED:editing}` in the body. Without such a tool, `{SHARED:editing}` is
  refused.
- `run_subagent` requires a non-empty `subagents:` list.

## 4. A top-level agent's `<name>.json`

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Equals the filename stem and the directory name. |
| `description` | yes | One line shown under the label in the picker. |
| `label` | no | The picker label. Defaults to the agent's `display_name`. |
| `rank` | no | Sort position in the picker; lower comes first. |
| `selectable` | no | `false` hides the agent from the picker. Default `true`. |
| `notes` | no | Rationale for maintainers. Never shown to a model or a user. |
| `default` | — | **Refused on a user agent.** Which agent a session starts on is the user's `default_agent` setting. |
| `interactive` | — | **Refused on a user agent.** Only built-in tools such as the Model Importer run non-interactively. |

Any other key is an error. The file cannot carry `$schema` for the same reason.

```json
{
  "name": "reviewer",
  "label": "Reviewer",
  "description": "Audits an existing codebase against its specs.",
  "rank": 30
}
```

## 5. A sub-agent's spec `<name>.json`

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Equals the filename stem and the directory name. |
| `input_schema` | yes | A shape object (below): what the caller fills in. |
| `output_schema` | yes | A shape object: what `return_result` accepts. |
| `notes` | no | Rationale for maintainers. Never shown to a model. |
| `produces` | no | `{role: output field}`: the artifact roles this agent writes, and which output field holds their paths. `"paths"` means every path it reported. |
| `consumes` | no | A list of `{role, scope, required}` needs the engine resolves to files before the spawn. |
| `component_paths` | no | The output field holding `{codename: path}` when one run writes files for several components. |

Any other key is an error, at the top level, inside a shape and inside a
`consumes` entry.

### Schema shapes

A schema is the name of a shape plus that shape's arguments, never a
hand-expanded copy of a shared envelope.

| Shape | Arguments | Use for |
|---|---|---|
| `pipeline_input` | `input_paths` (required: what the agent must read), `require_input_paths`, `require_responsibility`, `extra_properties`, `extra_required` | The input of a file-backed pipeline stage. The engine fills `input_paths`; callers never pass paths. |
| `author_output` | `extra_properties` | The output of an author that writes files. It carries the escalation fields (`reason`, `options`, `summary`); pair it with `{SHARED:escalation}`. |
| `critic_output` | none | Every critic's output: `{findings, summary}`. Pair it with `{SHARED:findings_critic}`. |
| `raw` | `schema` (required) | A contract that is the agent's own. The object under `schema` is used as written. Write a `description` on every property: it is the only documentation the caller's model sees. |

```json
{
  "name": "auditor",
  "notes": "Read-only auditor; not a pipeline stage, so a raw contract.",
  "input_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "instructions": {"type": "string", "description": "What to audit and why."}
      },
      "required": ["instructions"]
    }
  },
  "output_schema": {
    "shape": "raw",
    "schema": {
      "type": "object",
      "properties": {
        "summary": {"type": "string", "description": "One line: what was audited and the headline finding."}
      },
      "required": ["summary"]
    }
  }
}
```

### Artifact roles and scopes

`consumes` scopes: `global` (the default: the project's current work product
for the role), `self` (narrowed to this spawn's component), `all` (every work
product for the role), `under_review` (the file a critic round reviews) and
`dependencies` (the designs of neighbouring components; declare it
`"required": false`). A user agent can coin new role names, but every consumed
role must be produced by some agent. An unmet required need refuses the spawn.

## 6. Tokens in a prompt body

| Token | Rule |
|---|---|
| `{SHARED:working_rules}` | Kōdo's working rules. Required in built-ins, optional for user agents; by convention second-to-last. |
| `{SHARED:security}` | Injection resistance and safety rules. Required in built-ins, optional for user agents; by convention last. |
| `{SHARED:editing}` | Required exactly when the agent holds a tool that changes files (section 3). |
| `{SHARED:task_input}` | How the task message is laid out. For sub-agents with a spec, right after the opening paragraph. Forbidden in a top-level agent. |
| `{SHARED:findings_author}` | The author half of the findings protocol. Exactly one of the two findings blocks, and only with `get_findings`. |
| `{SHARED:findings_critic}` | The critic half of the findings protocol. |
| `{SHARED:escalation}` | How to hand a blocker back through `reason`. Pairs with `author_output`. |
| `{SHARED:dependencies}` | The `DEPENDENCIES.md` contract, for agents that read or write it. |
| `{SHARED:callouts}` | The `<kodo_info>`-style callouts. For top-level agents; a sub-agent's callouts are drawn by the client. |
| `{SKILLS}` | The skills catalog. Required exactly when `use_skill` is granted. |
| `{PHASE:initial}` … `{/PHASE}` | Text kept only on an author's first pass. |
| `{PHASE:revision}` … `{/PHASE}` | Text kept only when the author is working its findings backlog. |

- Only the shared blocks above exist. A `{SHARED:<name>}` naming any other
  block is an error, and a user agent cannot add its own `shared_*.md`.
- Phase blocks are valid only in an agent with a spec, cannot nest, and close
  with a bare `{/PHASE}`, never `{/PHASE:revision}`.
- Shared blocks render Kōdo's current text, so a copied agent picks up Kōdo's
  fixes to them on upgrade.

## 7. Broken or loading

An agent loads when the loader accepts its files and every cross-reference
resolves. These make it **broken** (listed with the reason, never run):

- a name with the `kodo_` prefix, or a name that disagrees with its file or
  directory;
- missing frontmatter, an empty body, or an unknown `role`;
- a tool that is not a real Kōdo tool;
- a `subagents:` entry that does not exist, or `subagents:` without
  `run_subagent` (or the reverse);
- a `critic:` that does not name a `role: critic` agent;
- a sub-agent with no spec, or a listed sub-agent with no `## Purpose`;
- an unknown `{SHARED:…}` block, a missing or forbidden required block, or
  `use_skill` without `{SKILLS}` (or the reverse);
- an unknown key in `<name>.json` or the spec, an unknown shape or shape
  argument, or `default`/`interactive` in a user agent's config;
- a consumed role that nothing produces.

A top-level agent that lists a broken sub-agent is broken too. After any edit,
validate the agent and fix every load error before anything else.
