# Agent Skills

> Status: implemented (2026-08-19; installer added 2026-08-20). User-installed
> instruction packs under `~/.kodo/skills`, surfaced to agents by progressive
> disclosure and managed from the Kōdo Settings panel's **Skills** section or
> `python -m kodo`.

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

Three ways, all landing on the same directory-copy underneath:

**By hand.** Copy or clone a skill directory into `~/.kodo/skills` and it is
live on the next agent turn — no registry, no refresh command, no restart.
This remains the simplest path and the one with no trust decision beyond
"I put this directory here myself."

**From a local file or directory already on disk**, via
`python -m kodo --install-skill TARGET` (`TARGET` an existing local path) or
the Kōdo Settings panel's Skills section ("Install from a local file…"). This
is an assisted version of "by hand": `kodo.skills.install_local_skill` copies
**exactly one** skill — no `git`, no clone, no picker over several
candidates:

1. `TARGET` resolves to either a directory holding `SKILL.md` directly, or a
   direct path to the `SKILL.md` file itself (its parent directory is the
   skill). A `TARGET` that is neither — does not exist, or is a file not
   named `SKILL.md` — is a clear, upfront error.
2. Unlike the repository flow below, the directory is **not** scanned
   recursively for further `SKILL.md` files — a directory bundling several
   skills must be installed one at a time, each by pointing this at its own
   subdirectory. The skill's name is its directory's basename, same as
   installing by hand.
3. The candidate is parsed with the same `load_skill` §1 uses. A malformed
   `SKILL.md` is a clear, upfront error here (unlike the repo flow's silent
   skip of one broken candidate among several — there is no fallback
   candidate when only one was named).
4. A name that already exists under `~/.kodo/skills` is **not** overwritten
   until confirmed — a `y`/`n` prompt on the CLI (skipped, and answered yes,
   under `--yes`/`-y`), a native confirm dialog in the panel — then the whole
   directory is copied in, replacing the existing one.

The CLI tells the two `--install-skill` shapes apart by whether `TARGET`
resolves to an existing path on disk: if so, this local flow runs; otherwise
`TARGET` is treated as a git repository URL (below). A local git-repository
*directory* passed to `--install-skill` therefore now takes this branch —
exactly one skill, requiring `SKILL.md` directly at that path — rather than
the repository flow's recursive multi-skill scan; point at a subdirectory
directly, or use `git clone` yourself first, to install more than one skill
out of it in one pass.

**From a git repository**, via `python -m kodo --install-skill REPO_URL` (a
`TARGET` that does *not* resolve to a local path) or the Kōdo Settings
panel's Skills section ("Install from a repository…"). Both call the same
`kodo.skills` functions:

1. `git clone --depth 1` the repo into a throwaway temp directory. Requires
   the `git` CLI on `PATH` — its absence is a clear, upfront error (not a
   crash), both on the CLI (stderr) and in the panel (an error toast). This is
   the one hard external dependency the feature has, deliberately not
   vendored or worked around: cloning is exactly what `git` is for, and every
   environment that can build/run kodo already has it for kodo's own
   development.
2. Scan the clone for every `SKILL.md` it contains — a repo may bundle several
   skills, one per subdirectory (or be a single skill with `SKILL.md` at its
   root, named after the repo itself; see `kodo/skills/_install.py`).
3. Each candidate is parsed with the exact same `load_skill` §1 uses, so a
   candidate that reaches the picker is guaranteed installable exactly as
   shown — no separate, looser "does this look like a skill" check.
4. The valid candidates (name + description) are offered to the user to
   choose from — checkboxes in the panel's modal, or a `y`/`n`/`a`/`q` prompt
   per skill on the CLI (`a` = yes to everything remaining, `q` = stop).
   A candidate whose name already exists under `~/.kodo/skills` is flagged
   inline (**"already installed locally — will be overwritten"**); answering
   yes to it *is* the overwrite confirmation — there is no second prompt.
5. Only the skills the user picked are copied — the whole directory
   containing `SKILL.md`, siblings and subdirectories included — into
   `~/.kodo/skills`, overwriting an existing same-named skill only where the
   user confirmed it in step 4. The clone is deleted immediately after.

Nothing is cached between the "show me what's in this repo" step and the
"install what I picked" step — the repo is cloned once for each, matching
`SkillStore`'s own stateless-between-calls convention (§8). A user who takes
a while deciding in the modal never leaves an orphaned clone behind, and a
repo that changed in between is simply re-scanned; a name the user picked
that is no longer found is reported rather than silently skipped.

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

**Kōdo Settings → Skills**, between *Sessions* and *Global Allow-Rules*. An
**"Install from a repository…"** button and an **"Install from a local
file…"** button above a table of every installed skill:

| Column | Content |
|---|---|
| Skill | The directory name — the skill's identity. |
| Description | Its `description`, or the load error for a broken skill. |
| (actions) | 📁 Open · 🗑 Delete |

- **Install from a repository…** opens a modal: a repo URL field, then (after
  scanning) one checkbox per valid skill found — name, description, and an
  inline warning on any name that already exists locally — then, after
  installing, the list of what was actually installed. See §2 for the full
  flow. A missing `git` CLI, or a clone failure, shows an error toast and
  leaves the modal on the URL step so the user can fix the URL or install
  `git` and retry.
- **Install from a local file…** opens the OS's native file-open dialog,
  filtered to `.md` files. The user navigates to and selects a `SKILL.md`
  file directly — there is no folder picker and no recursive scan, since this
  installs exactly one skill (§2). A picked file not literally named
  `SKILL.md` is rejected client-side with an error toast, before any server
  round trip. If a same-named skill is already installed, a native confirm
  dialog asks before overwriting; declining leaves the existing skill
  untouched. No custom modal is needed — native dialogs are the whole UI.
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

`skills.list` / `skills.delete`, `skills.install_scan` / `skills.install`, and
`skills.install_local`, control-connection only — see doc/WS_PROTOCOL.md
§7.6j. `skills.list` and `skills.delete` reply with the same full listing, so
the panel refreshes from either response with no follow-up round trip;
`skills.install` and `skills.install_local` do too.

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
| [skills/_skill.py](../src/kodo/skills/_skill.py) | `Skill` (frozen) + `load_skill` — the never-raising SKILL.md parser and its own third-party-tolerant frontmatter reader. Its `name` argument lets a caller install-target-name a skill discovered somewhere other than directly under the skills root. |
| [skills/_store.py](../src/kodo/skills/_store.py) | `SkillStore` — `entries()` (all, broken included), `usable()`, `get()`, `delete()`, `ensure_root()`, and the containment guard. |
| [skills/_install.py](../src/kodo/skills/_install.py) | `require_git`, `scan_repository`, `install_skills`, `install_local_skill`, `InstallResult` — cloning a repo and copying selected skills out of it, or installing one from a local path with no clone at all (§2). |
| [skills/_catalog.py](../src/kodo/skills/_catalog.py) | `render_catalog` — the `## Available skills` block substituted for `{SKILLS}`. |
| [project/_layout.py](../src/kodo/project/_layout.py) | `kodo_skills_dir()` — `~/.kodo/skills`. |
| [toolspecs/_use_skill.py](../src/kodo/toolspecs/_use_skill.py) | The `USE_SKILL` spec. |
| [tools/_use_skill.py](../src/kodo/tools/_use_skill.py) | `UseSkillTool` — re-scans the store per call. |
| [subagents/_registry.py](../src/kodo/subagents/_registry.py) | `SKILLS_TOKEN`, `__validate_skills`, and the per-turn expansion in `__finalize`. |
| [server/_app.py](../src/kodo/server/_app.py) | `skills.list` / `skills.delete` / `skills.install_scan` / `skills.install` handlers; `ensure_root()` on startup. |
| [transport/_messages.py](../src/kodo/transport/_messages.py) | `MSG_SKILLS_LIST` / `MSG_SKILLS_DELETE` / `MSG_SKILLS_INSTALL_SCAN` / `MSG_SKILLS_INSTALL`. |
| [\_\_main\_\_.py](../src/kodo/__main__.py) | `python -m kodo --list-skills` / `--install-skill REPO_URL [--yes]` — the CLI installer. |
| kodo-vsix `settings-webview/SkillsSection.tsx` | The Skills table + "Install from a repository…" button. |
| kodo-vsix `settings-webview/InstallSkillsModal.tsx` | The URL → checkbox-picker → results modal. |
| kodo-vsix `extension/kodo-settings-bridge.ts` | `fetchSkillsForPanel`, `openSkillFolder`, `deleteSkillFromSettingsPanel`, `scanSkillRepoForPanel`, `installSkillsFromPanel`. |

`kodo.skills` is a **leaf package** — it imports nothing from `kodo`, taking
the skills root as a `SkillStore` constructor argument — so `tools` (T2.5),
`subagents` (T3) and `server` can all use it without breaching the import
ceiling (doc/INTERNALS.md §6A, CLAUDE.md).
