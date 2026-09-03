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
     none of them. There is no frontmatter for shared prompt text — see the
     `{SHARED:…}` list below.
   - Body **must** contain a `## Purpose` section (caller-agnostic, third
     person). It becomes the **description of your agent's
     `run_subagent_foo` tool** — the only thing a caller ever reads about it —
     so write it for whoever is deciding whether to delegate. The registry
     fails fast if an agent in some caller's `subagents:` list has none. Do
     **not** write a `## Tools` section: granted tools are never described in
     the prompt, only in the LLM `tools` argument ([TOOLS.md](TOOLS.md) §7).
   - Body **must** include the shared blocks it needs, via `{SHARED:<name>}` →
     `shared_<name>.md`. That one token is the *only* mechanism; there is no
     `bases:`, no `callouts:`, no auto-append. What to include:
     - `{SHARED:working_rules}` and `{SHARED:security}` — **mandatory**, in that
       order, as the last two things in the file. Construction fails without
       them, and so does `test_agents.py`.
     - `{SHARED:editing}` — **mandatory if** you granted any tool whose
       `ToolSpec.modifies_files` is true; forbidden otherwise, since it names
       tools you weren't given.
     - `{SHARED:task_input}` — the pointer saying the real task arrives as the
       first message. Include it (conventionally its own paragraph right after
       the opening identity paragraph). Omit it only if the engine seeds your
       agent some way other than `_render_task_input` — today only `compactor`,
       which describes its real input itself.
     - `{SHARED:escalation}` — if your agent can hand a blocker back; pairs with
       `author_output(...)` (see below).
     - `{SHARED:dependencies}` — if your agent reads or writes `DEPENDENCIES.md`.
     - `{SHARED:callouts}` — entry agents only. A sub-agent runs inside a
       subsession block where its text is collapsed and the block's own
       open/close callouts are drawn by the client.
   - One further token is **not** a `{SHARED:…}` block: `{SKILLS}` expands to
     the catalog of user-installed skills (doc/SKILLS.md). It is **mandatory
     if** you granted `use_skill`, and forbidden otherwise — construction fails
     both ways, and so does `test_agents.py`. Grant it only to an agent that
     decides *what kind of task* it is doing; a sub-agent handed an
     already-scoped task usually should not, since its caller has already made
     that call.
   - Do **not** restate anything a shared block or a tool description already
     says (minimal edits, silent reasoning, injection resistance, the
     `ask_user` discipline).
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
     with `{SHARED:escalation}` in the body — the schema half without the
     prompt half is inert, and a test in `test_subagentspecs.py` fails if you
     ship one without the other. See doc/TOOLS.md §5A.
   - Every critic returns the **same** shape (`critic_output()` takes no
     arguments): `{path, findings, summary}` — there is deliberately no
     `accept`, because the verdict is derived from an empty backlog
     (doc/FINDINGS.md §3). Each `findings` entry is either a new finding (no
     `id`) or an update to an existing one (`id` plus only what changed). A
     critic's **concern vocabulary is prose** in its prompt's
     `### Concern vocabulary` section, not a schema enum — see doc/TOOLS.md §5A
     for why. Choose the kinds deliberately; they are free-form per critic and
     not coupled to engine logic.
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

**Both halves also need the findings protocol** (doc/FINDINGS.md), which is how
they actually communicate — the loop passes no findings through the task:

- grant `get_findings` in the `tools:` frontmatter of *both* the author and the
  critic;
- include `{SHARED:findings_author}` in the author's prompt and
  `{SHARED:findings_critic}` in the critic's — exactly one each. The registry
  refuses to load an agent that grants the tool with neither block, includes
  both, or includes a block without the grant.

Write the prompt so it reads identically on a first pass and a tenth: the shared
block already says "call `get_findings` first, every time", so the agent-specific
prose must not describe a separate "revision round" shape. A critic additionally
keeps its own `### Concern vocabulary` section — the `kind` values it may use —
which the schema points at rather than duplicating.

Tool generation (`_registry.py`): every non-critic in the allow-list gets a
`run_subagent_<name>` tool whose description is that agent's own `## Purpose`,
plus a sentence saying whether it is a workflow stage or a standalone
specialist, plus — for an author — the review-loop contract naming its critic.
A critic gets no tool at all; what a caller needs to know about it is in its
author's description. **Nothing about a sub-agent is written into a caller's
prompt** — there is no roster.

## Pipeline placement (guide prompt)

If `foo` is a pipeline stage (not `standalone`), update **`agent_guide.md`**:
the numbered **"The Pipeline You Run"** list, the **Stage → agent map** table,
and any cascade/escalation prose that names the stage. The guide prompt is the
source of truth for stage order; a tool description says what its agent does,
never where it sits in the sequence. Keep author and critic adjacent in the
`subagents:` list so the generated tools read in pipeline order.

## Tests

- `test/test_subagentspecs.py` — schema well-formedness + per-critic concern
  enums are auto-parametrized over `ALL_SUBAGENTS`, so a new critic is covered
  for free; add a focused test if it has notable kinds.
- `test/test_agents.py::test_shipped_guide_tools_carry_every_pipeline_pairing`
  checks each author's tool names its critic — update it when you change a
  pairing.
- `test/test_agents.py` also parametrizes a scan over **every** shipped
  `agent_*.md` / `subagent_*.md`: required blocks present, security last, no
  unknown block, editing-block-iff-write-tools,
  findings-block-iff-`get_findings`, no retired `bases:`/`callouts:`
  /`{PLACEHOLDER:…}`. That is where a forgotten token should fail — at build
  time, not on a running server.
- Both build `AgentRegistry(_REAL_AGENTS_DIR)`, the last-resort runtime copy of
  the same checks.

## Run / verify

From `kodo` (deps `aiohttp` etc. may be absent in some envs — the agent/spec
tests don't need them):

```bash
PYTHONPATH=src python3 -m pytest test/test_agents.py test/test_subagentspecs.py test/test_main.py -q
PYTHONPATH=src python3 -c "from pathlib import Path; from kodo.subagents import AgentRegistry; AgentRegistry(Path('src/kodo/subagents'))"
ruff check src/kodo/subagents/specs/
```

Then **read the prompt your agent will actually receive** — your body with every
`{SHARED:…}` token expanded in place:

```bash
PYTHONPATH=src python3 -m kodo --system-prompt foo --model claude-opus-5
```

This is the fastest way to see the blocks land where you meant them to. It
will **not** show your concrete task or its
schema — no schema is ever restated in a sub-agent's own prompt (input or
output). The input schema reaches a *caller* as real JSON Schema on
`run_subagent_foo`; the sub-agent itself sees concrete values, per-field
descriptions, and the `return_result` reminder rendered fresh per call under
`## Input Parameters` in its first user turn (`_render_task_input`,
doc/SESSIONS.md "Typed sub-agent interface") — check that path if you need to
verify a field's description reads well. It calls `AgentRegistry.get` itself,
so what `--system-prompt` prints is what the engine sends
(see [INTERNALS.md](INTERNALS.md) §9a). `--model`/`-m` only has to resolve —
every model gets the same prompt today — and can be omitted to use the first
installed model in the local registry. `test/test_main.py` sweeps *every*
packaged agent through it, so a new agent that fails to render fails there too.
The same CLI's `--tools foo --model claude-opus-5` prints the agent's granted
tools in the exact OpenAI wire shape, if you need to check that side too.

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
  example `reason` strings, and the pairing assertions in `test_agents.py`. Escalate
  `reason` strings and critic `kind`s are free-form (no engine branches on them),
  so they're safe to rename — but stale ones mislead the next reader.
- **Memory + docs**: update `project_kodo.md` and the doc set in the same change
  (see the repo's memory-discipline rule in `CLAUDE.md`).
