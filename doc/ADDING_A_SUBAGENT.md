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
   - Frontmatter: `name`, `display_name`, `capability` (`max`/`high`/`medium`/`low`,
     default `medium`), `tools:` (each must resolve to a `ToolSpec` in
     `kodo.toolspecs`). For an **author** add `critic: <critic_name>`; for a
     **critic** add `role: critic`; for an on-demand specialist add
     `standalone: true`. An agent that is simply invoked and returns declares
     none of them.
   - Body **must** contain a `## Purpose` section (caller-agnostic, third person)
     — the registry renders it into every caller's `{PLACEHOLDER:SUBAGENTS}`
     roster and **fails fast if it's missing**. Do **not** write a `## Tools`
     section: granted tools are never described in the prompt, only in the LLM
     `tools` argument (see [TOOLS.md](TOOLS.md) §7).
2. **Spec** — `src/kodo/subagents/specs/_foo.py`
   - One module-level `SubAgentSpec` constant (e.g. `FOO`), mirroring the
     one-literal-per-file `toolspecs` convention.
   - Build `input_schema`/`output_schema` from the shared builders in
     `specs/_shapes.py`: `pipeline_input(...)`, `author_output(...)`,
     `critic_output()`. Don't hand-roll envelopes; don't declare
     `schema_compliance` (the engine injects it).
   - Both schemas reach the model as **real JSON Schema on real tools** — the
     input on the caller's `run_subagent_foo`, the output bound to `foo`'s own
     `return_result` — so write the per-field `description`s for a reader who
     has no other source. Nothing restates them in prose.
   - `author_output(...)` also declares the **escalation** fields
     (`reason`/`options`, with `summary` doing double duty) and requires only
     `summary`, so a blocked author can return a compliant escalation. Pair it
     with `bases: [escalation]` in the frontmatter — the schema half without the
     prompt half is inert, and a test in `test_subagentspecs.py` fails if you
     ship one without the other. See doc/TOOLS.md §5A.
   - Every critic returns the **same** shape (`critic_output()` takes no
     arguments): `{path, accept, concerns, summary}`. A critic's **concern
     vocabulary is prose** in its prompt's `### Concern vocabulary` section, not
     a schema enum — see doc/TOOLS.md §5A for why. Choose the kinds
     deliberately; they are free-form per critic and not coupled to engine
     logic.
3. **Spec registration** — `src/kodo/subagents/specs/__init__.py`
   - Add the `from ._foo import FOO` import, the `"FOO"` entry in `__all__`, and
     `FOO` in the `ALL_SUBAGENTS` tuple. (The registry cross-references spec ↔
     `subagent_*.md` by name and fails fast if either side is missing.)
4. **Caller wiring** — the agent(s) that may spawn `foo` list it in their
   frontmatter `subagents:` allow-list (e.g. `agent_guide.md`,
   `agent_problem_solver.md`), and must also declare the `run_subagent` tool
   (the registry rejects either half without the other). Listing `foo` is what
   mints the caller's `run_subagent_foo` tool, carrying `foo`'s own
   `input_schema`; the engine gates the spawn against the same list. A sub-agent
   no caller lists can never run, and has no tool anywhere.

## Author ↔ critic pairing (how the loop knows the pair)

Two pieces of frontmatter, and nothing else:

- the **author** declares `critic: <name>`;
- that critic declares `role: critic` on itself.

The registry validates the pair at load time (the critic must exist and must
declare the role) and refuses a critic that declares a `critic:` of its own.

Everything follows from those two. `run_subagent_<author>` becomes a **loop**
tool — it takes an optional `max_rounds` and returns a `review` block — and the
engine spawns the critic inside that call. A caller never names a critic, never
gets a tool for one, and never iterates by hand (doc/TOOLS.md §5A).

Roster rendering (`_registry.py`): every non-critic in the allow-list gets a row
naming its `run_subagent_<name>` tool, with the **Review** column showing its
critic or `none — single pass`. A critic is absorbed into its author's row and
gets no row of its own — but still gets a `## Purpose` paragraph, so the caller
knows what the review will hold its output to. The roster carries **no schemas**:
those live on the tools.

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
PYTHONPATH=src python3 -m pytest test/test_agents.py test/test_subagentspecs.py test/test_main.py -q
PYTHONPATH=src python3 -c "from pathlib import Path; from kodo.subagents import AgentRegistry; AgentRegistry(Path('src/kodo/subagents'))"
ruff check src/kodo/subagents/specs/
```

Then **read the prompt your agent will actually receive** — the rendered article,
with the preambles, any `bases:` snippets, the `{PLACEHOLDER:SUBAGENTS}` roster
and the task contract all substituted:

```bash
PYTHONPATH=src python3 -m kodo --system-prompt claude-opus-5 foo
```

This is the fastest way to catch a placeholder that never got filled, a roster
row that reads wrong, or a contract that doesn't match your `SubAgentSpec`. It
calls `AgentRegistry.get` itself, so what it prints is what the engine sends
(see [INTERNALS.md](INTERNALS.md) §9a). The `LLM_ID` only has to resolve — every
model gets the same prompt today. `test/test_main.py` sweeps *every*
packaged agent through it, so a new agent that fails to render fails there too.
The same CLI's `--tools claude-opus-5 foo` prints the agent's granted tools in
the exact OpenAI wire shape, if you need to check that side too.

On Windows the canonical check is `mise exec node -- npm run check-types && ...`
in `kodo-vsix` for the front-end; the Python side is pytest + ruff as above.

## Restructuring an existing agent (e.g. splitting a role out)

When you move a responsibility from one agent to a new one (as when the Test Plan
behavioral review was split out of `test_coder` into the new
`test_design_critic`):

- **Re-point the pairing**: change the author's `critic:` to the new critic.
- **Strip the moved role** from the old agent — its prompt sections, its
  frontmatter `tools:` that only served the old role, its `role:`/`critic:`
  frontmatter, and its spec (a dual-role `oneOf` collapses back to a single
  shape once one role leaves).
- **Hunt every mention**: `grep -rn` the old agent name across `src` **and**
  `doc` and `test`. Update the guide pipeline, the INTERNALS agent-tools table,
  any `oneOf`/dual-role comments in `toolspecs/_compliance.py`, escalate-blocker
  example `reason` strings, and roster assertions in `test_agents.py`. Escalate
  `reason` strings and critic `kind`s are free-form (no engine branches on them),
  so they're safe to rename — but stale ones mislead the next reader.
- **Memory + docs**: update `project_kodo.md` and the doc set in the same change
  (see the repo's memory-discipline rule in `CLAUDE.md`).
