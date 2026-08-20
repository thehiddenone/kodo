# Agent Skills

> Status: implemented (2026-08-19). User-installed instruction packs under
> `~/.kodo/skills`, surfaced to agents by progressive disclosure and managed
> from the Kōdo Settings panel's **Skills** section.

---

## 1. What a skill is

A **skill** is a directory holding expert instructions for one kind of task —
"how to work with PDFs", "how this company writes commit messages" — that the
user installs so agents follow them instead of improvising. Kōdo uses the open
[Agent Skill](https://github.com/anthropics/skills) format, so a skill written
for any other agent runtime works here unchanged.

The format is one file plus whatever it references:

```
~/.kodo/skills/
  pdf/
    SKILL.md          <- required; the only file Kōdo itself parses
    REFERENCE.md      <- optional companion files, any name/layout
    FORMS.md
    scripts/
      fill_form.py
  commit-style/
    SKILL.md
```

`SKILL.md` is Markdown with YAML frontmatter:

```markdown
---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files —
  reading or extracting text and tables, merging, splitting, rotating, filling forms,
  or OCR on scanned documents.
---

# PDF Processing Guide

Use `pypdf` for reading and `reportlab` for generation. See REFERENCE.md for
advanced features and FORMS.md for form filling.
```

Two frontmatter keys matter to Kōdo:

| Key | Required | Used for |
|---|---|---|
| `description` | **yes** | The whole routing signal — it is all an agent sees until it decides to load the skill. |
| `name` | no | Cross-checked against the directory name; a mismatch is a load error. |

Every other key real skills carry (`license`, `allowed-tools`, …) is parsed and
ignored. Kōdo has no equivalent of `allowed-tools`: a skill refines *how* an
agent works, it never widens what the agent may do (§6).

**A skill's identity is its directory name**, not its frontmatter `name`. The
directory is what the user created, what the Settings panel's Open and Delete
act on, and what `use_skill` looks up, so making it the key keeps all three
unambiguous. `name` exists in the format, so it is validated rather than
ignored — declaring `name: pdfs` inside `skills/pdf/` is a load error, not a
silent rename.

---

## 2. Installing a skill

By hand. Copy or clone a skill directory into `~/.kodo/skills` and it is live
on the next agent turn — there is no installer, no registry, no refresh
command, and no restart.

That is a deliberate scope choice, not a gap: an installer would need a trust
model for third-party instruction text, and dropping in a directory is
something the user can already do, audit, and undo with their file manager.
The Settings panel therefore offers **Open** and **Delete** and no *Add*.

The server creates `~/.kodo/skills` on startup (`create_app`, `_app.py`) so the
directory the user is pointed at always exists.

---

## 3. How a skill reaches an agent — progressive disclosure

Two halves, because loading every installed skill's full text into every
prompt would cost thousands of tokens per turn for content most turns never
use:

**Half one — the catalog, in the system prompt.** Each agent that opts in
carries a `{SKILLS}` token in its prompt body, expanded by
`AgentRegistry.__finalize` into one line per installed skill:

```
## Available skills

A **skill** is a set of expert instructions for one kind of task, installed by
the user. […] When a task matches one, call `use_skill` with that name before
planning your approach […]

- **pdf** — Use this skill whenever the user wants to do anything with PDF files. […]
- **commit-style** — Use when writing a commit message in this repository. […]
```

Descriptions are reproduced **verbatim and uncropped**. In this format the
description *is* the routing signal, so truncating it to save tokens would
defeat the mechanism it exists for.

**Half two — the `use_skill` tool.** The model calls it with one name and gets
that skill's full instructions back, plus the skill's absolute directory path:

```json
{ "name": "pdf",
  "description": "Use this skill whenever …",
  "path": "/home/u/.kodo/skills/pdf",
  "instructions": "# PDF Processing Guide\n\nUse `pypdf` for reading …" }
```

With no skills installed the catalog still renders — as an explicit "No skills
are installed" block. That is what stops a model from inventing a plausible
name to pass to `use_skill`.

### The catalog is rendered per turn, from disk

`{SKILLS}` is expanded in `AgentRegistry.get()` (which the engine calls every
turn), not once when the registry is constructed. `SkillStore` holds no cache
and re-scans on every read. So a skill dropped in mid-session is advertised on
the next turn, and one deleted from the Settings panel stops resolving
immediately — no reload, no invalidation message, nothing to forget to send.

The scan is guarded on the token being present, so the ~30 agents that don't
use skills never touch the filesystem for it.

---

## 4. Companion files

Skills routinely split content across files — `REFERENCE.md`, `scripts/`,
`references/` — and reference them by relative name from the body. `use_skill`
returns the skill's absolute `path` so the agent can open them with
`read_file`, `find_files` or `find_text_in_files`.

**No path-guard change was needed for this**, and none was made.
`LogicalPathResolver` (doc/TOOLS.md §5) takes absolute paths as-is with no
containment check, and those three tools are `SecurityImpact.MINIMAL` with
`requires_project=False` — below every posture's ask-threshold in
`kodo.security` — so a path under `~/.kodo/skills` already resolved and already
passed the gate before this feature existed.

Nothing special *blocks* writes there either. That is consistent rather than
lax: no absolute path anywhere in Kōdo gets a bespoke write-block, and adding
one only for the skills root would be a new restriction with no counterpart.
A skill's own scripts are ordinary files; running one goes through
`run_command` and the normal Command Control posture, exactly like any other
command.

---

## 5. Managing skills — the Kōdo Settings panel

**Kōdo Settings → Skills**, between *Sessions* and *Global Allow-Rules*. A
table of every installed skill:

| Column | Content |
|---|---|
| Skill | The directory name — the skill's identity. |
| Description | Its `description`, or the load error for a broken skill. |
| (actions) | 📁 Open · 🗑 Delete |

- **📁 Open** launches a **new VS Code window** rooted at the skill's directory,
  so the user can read and edit `SKILL.md` and its companion files with the
  ordinary editor.
- **🗑 Delete** confirms with a native modal dialog and then removes the whole
  directory, recursively and irreversibly.

**Broken skills are listed, not hidden.** A directory whose `SKILL.md` is
missing, has no frontmatter, or declares no `description` appears with its
error in the Description column and is styled as an error row. An agent never
sees it. Hiding it would leave a user who mistyped their frontmatter staring at
a skill that silently does nothing; listing it makes the problem visible and
deletable in the same place.

### Wire protocol

`skills.list` / `skills.delete`, control-connection only — see
doc/WS_PROTOCOL.md §7.6j. Both reply with the same full listing, so the panel
refreshes from either response with no follow-up round trip. There is no
`skills.add`, matching §2.

---

## 6. Security posture

A skill is **instruction text the user chose to install**, and it is treated as
guidance, not authority:

- **`use_skill` grants nothing.** It returns text. Every subsequent action the
  agent takes runs through the same tool grants, the same security layer and
  the same Command Control posture as any other turn. A skill cannot widen an
  agent's tool set, which is why Kōdo parses and ignores the format's
  `allowed-tools` key.
- **The tool's own description frames what comes back**: expert guidance for
  *how* to carry out the current task, never instructions from the user, never
  an override of the agent's operating rules. `{SHARED:security}` (last block
  in every prompt, highest precedence) continues to govern.
- **The store never escapes itself.** `use_skill`'s `name` comes from an LLM
  and `skills.delete`'s comes over the wire; `SkillStore.__resolve` requires a
  single path component with no separator and no `.`/`..`, then re-checks after
  `resolve()` — which follows symlinks — that the parent is still the resolved
  skills root. A symlinked entry pointing outside the store is refused rather
  than followed.
- **Malformed input degrades, never raises.** `load_skill` returns a `Skill`
  carrying an `error` string; nothing in the listing path can throw past the
  caller because of a file a third party wrote.
- `SecurityImpact.MINIMAL`, available in autonomous mode — it is a read of a
  file the user themselves installed.

---

## 7. Which agents get skills

The **`use_skill` tool grant is the opt-in**, declared per agent in its
frontmatter like every other capability. There is no engine-side list of
"agents that have skills" to keep in sync.

`AgentRegistry.__validate_skills` binds the two halves together at load time,
in both directions:

- grants `use_skill` but omits `{SKILLS}` → `AgentLoadError` (the model would
  have a tool it could only call with a guessed name)
- includes `{SKILLS}` but does not grant `use_skill` → `AgentLoadError` (the
  agent would be shown skills it has no way to open)

So declaring the tool is sufficient — forgetting the token is caught for you,
at construction, not at runtime. `test_agents.py` re-runs the same parity check
over every shipped agent file, reading the tool name off the live `ToolSpec`
rather than hardcoding it, so a rename moves the test with it.

**Shipped with the grant:** `problem_solver` — the entry agent positioned to
decide that a task matches a skill before it hands work to a sub-agent — plus
every sub-agent that itself writes code or documents: `architect`,
`developer`, `e2e_test_coder`, `e2e_test_designer`, `functional_designer`,
`narrative_author`, `requirements_author`, `test_coder`, `test_designer`, and
`coder`. `guide` does not carry the grant — it talks to the user but never
produces the code or documents a skill would guide.

**Opted out, by category, not by omission:**

- **Critics** (`architect_critic`, `code_critic`, `e2e_test_code_critic`,
  `e2e_test_design_critic`, `functional_design_critic`, `requirements_critic`,
  `test_design_critic`) and `judge` — they evaluate someone else's output
  against a spec; they never author anything a skill would guide.
- **Toolchain agents** (`toolchain_builder`, `toolchain_depsmgr`) — their task
  is fixed by the toolchain itself, not by a user-installed convention.
- **Read-only / investigative agents** (`planner`, `investigator`,
  `web_search`) — they gather and reason, they don't produce the deliverable.
- `compactor` — a single-shot transcript rewriter with no filesystem tools at
  all.

Granting or revoking the pair (`use_skill` tool + `{SKILLS}` token) is a
two-line change to an agent's `.md` file; `__validate_skills` rejects either
half added alone.

---

## 8. File reference

| File | Role |
|---|---|
| [skills/_skill.py](../src/kodo/skills/_skill.py) | `Skill` (frozen) + `load_skill` — the never-raising SKILL.md parser and its own third-party-tolerant frontmatter reader. |
| [skills/_store.py](../src/kodo/skills/_store.py) | `SkillStore` — `entries()` (all, broken included), `usable()`, `get()`, `delete()`, `ensure_root()`, and the containment guard. |
| [skills/_catalog.py](../src/kodo/skills/_catalog.py) | `render_catalog` — the `## Available skills` block substituted for `{SKILLS}`. |
| [project/_layout.py](../src/kodo/project/_layout.py) | `kodo_skills_dir()` — `~/.kodo/skills`. |
| [toolspecs/_use_skill.py](../src/kodo/toolspecs/_use_skill.py) | The `USE_SKILL` spec. |
| [tools/_use_skill.py](../src/kodo/tools/_use_skill.py) | `UseSkillTool` — re-scans the store per call. |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | `SKILLS_TOKEN`, `__validate_skills`, and the per-turn expansion in `__finalize`. |
| [server/_app.py](../src/kodo/server/_app.py) | `skills.list` / `skills.delete` handlers; `ensure_root()` on startup. |
| [transport/_messages.py](../src/kodo/transport/_messages.py) | `MSG_SKILLS_LIST` / `MSG_SKILLS_DELETE`. |
| kodo-vsix `settings-webview/SkillsSection.tsx` | The Skills table. |
| kodo-vsix `extension/kodo-settings-bridge.ts` | `fetchSkillsForPanel`, `openSkillFolder`, `deleteSkillFromSettingsPanel`. |

`kodo.skills` is a **leaf package** — it imports nothing from `kodo`, taking
the skills root as a `SkillStore` constructor argument — so `tools` (T2.5),
`subagents` (T3) and `server` can all use it without breaching the import
ceiling (doc/INTERNALS.md §6A, CLAUDE.md).
