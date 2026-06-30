# Adding a Sub-Agent

A checklist for adding (or restructuring) a Guided-Dev sub-agent, distilled from
real changes. A sub-agent is **"a tool with agentic behavior"**: a prompt
(`subagent_<name>.md`) + a typed I/O contract (`SubAgentSpec`). Both halves must
exist and agree, or `AgentRegistry` construction fails fast at startup.

The two repos: prompts/specs/engine live in **`kodo`** (`src/kodo/...`); the VSIX
front-end lives in **`kodo-vsix`**. Adding a sub-agent is almost entirely a
`kodo`-side change — the VSIX only needs touching if you add a new *message* or
*event* (a new sub-agent alone adds neither).

## The pieces that must line up

A new sub-agent named `foo` needs **all** of these, or the registry raises
`AgentLoadError` at construction (see `kodo/subagents/_registry.py`):

1. **Prompt** — `src/kodo/subagents/subagent_foo.md`
   - Filename stem must be exactly `subagent_foo` (matches `name: foo`).
   - Frontmatter: `name`, `display_name`, `capability` (`high`/`medium`/`low`,
     default `medium`), `tools:` (each must resolve to a `ToolSpec` in
     `kodo.toolspecs`). For an **author** add `critic: <critic_name>`; for a
     **solo** add `solo: true`; for an on-demand specialist add
     `standalone: true`. A **pure critic** declares none of those three.
   - Body **must** contain a `## Purpose` section (caller-agnostic, third person)
     — the registry renders it into every caller's `{PLACEHOLDER:SUBAGENTS}`
     roster and **fails fast if it's missing**. Include a `## Tools` section with
     the `{PLACEHOLDER:TOOLS}` token.
2. **Spec** — `src/kodo/subagents/specs/_foo.py`
   - One module-level `SubAgentSpec` constant (e.g. `FOO`), mirroring the
     one-literal-per-file `toolspecs` convention.
   - Build `input_schema`/`output_schema` from the shared builders in
     `specs/_shapes.py`: `pipeline_input(...)`, `author_output(...)`,
     `critic_output([kinds...])`. Don't hand-roll envelopes; don't declare
     `schema_compliance` (the engine injects it).
   - A critic's `output_schema = critic_output([...])` whose list **is** its
     concern vocabulary (the `kind` enum). Choose kinds deliberately — they are
     free-form per critic and not coupled to engine logic.
3. **Spec registration** — `src/kodo/subagents/specs/__init__.py`
   - Add the `from ._foo import FOO` import, the `"FOO"` entry in `__all__`, and
     `FOO` in the `ALL_SUBAGENTS` tuple. (The registry cross-references spec ↔
     `subagent_*.md` by name and fails fast if either side is missing.)
4. **Caller wiring** — the agent(s) that may spawn `foo` list it in their
   frontmatter `subagents:` allow-list (e.g. `agent_guide.md`,
   `agent_problem_solver.md`). The engine gates every
   `run_subagent`/`run_author_critic_iteration` against this list. A sub-agent no
   caller lists can never run.

## Author ↔ critic pairing (how the loop knows the pair)

The pairing lives in the **author's** `critic:` frontmatter, nothing else. The
guide runs the loop with `run_author_critic_iteration`, one round at a time.
Roster rendering (`_registry.py`): an agent with `critic:` gets a
`run_author_critic_iteration` row naming its critic; a `solo: true` agent gets a
`run_subagent` row; a **pure critic** (no `critic`/`solo`/`standalone`) is
*absorbed* into its author's row and gets no row of its own (but still gets a
`## Purpose` paragraph). An agent can be both `solo` and a critic — the renderer
supports the combined row even if no live agent uses it today.

## Pipeline placement (guide prompt)

If `foo` is a pipeline stage (not `standalone`), update **`agent_guide.md`**:
the numbered **"The Pipeline You Run"** list, the **Stage → agent map** table,
and any cascade/escalation prose that names the stage. The guide prompt — not
the roster — is the source of truth for stage order. Keep author and critic
adjacent in the `subagents:` list so the rendered roster reads naturally.

## Tests

- `test/test_subagentspecs.py` — schema well-formedness + per-critic concern
  enums are auto-parametrized over `ALL_SUBAGENTS`, so a new critic is covered
  for free; add a focused test if it has notable kinds.
- `test/test_agents.py::test_real_guide_roster_reproduces_pipeline_pairs`
  asserts specific roster rows — update it when you change a pairing.
- Both build `AgentRegistry(_REAL_AGENTS_DIR)`, which is the real fail-fast check
  that every `## Purpose`, tool, base, and roster reference resolves.

## Run / verify

From `kodo` (deps `aiohttp` etc. may be absent in some envs — the agent/spec
tests don't need them):

```bash
PYTHONPATH=src python3 -m pytest test/test_agents.py test/test_subagentspecs.py -q
PYTHONPATH=src python3 -c "from pathlib import Path; from kodo.subagents import AgentRegistry; AgentRegistry(Path('src/kodo/subagents'))"
ruff check src/kodo/subagents/specs/
```

On Windows the canonical check is `mise exec node -- npm run check-types && ...`
in `kodo-vsix` for the front-end; the Python side is pytest + ruff as above.

## Restructuring an existing agent (e.g. splitting a role out)

When you move a responsibility from one agent to a new one (as when the Test Plan
behavioral review was split out of `test_coder` into the new
`test_design_critic`):

- **Re-point the pairing**: change the author's `critic:` to the new critic.
- **Strip the moved role** from the old agent — its prompt sections, its
  frontmatter `tools:` that only served the old role (e.g. a critic that no
  longer reviews drops `document_feedback`), and its spec (a dual-role `oneOf`
  collapses back to a single shape once one role leaves).
- **Hunt every mention**: `grep -rn` the old agent name across `src` **and**
  `doc` and `test`. Update the guide pipeline, the INTERNALS agent-tools table,
  any `oneOf`/dual-role comments in `toolspecs/_compliance.py`, escalate-blocker
  example `reason` strings, and roster assertions in `test_agents.py`. Escalate
  `reason` strings and critic `kind`s are free-form (no engine branches on them),
  so they're safe to rename — but stale ones mislead the next reader.
- **Memory + docs**: update `project_kodo.md` and the doc set in the same change
  (see the repo's memory-discipline rule in `CLAUDE.md`).
