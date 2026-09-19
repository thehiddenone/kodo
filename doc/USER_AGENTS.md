# User-Installed Agents

> Status: **implemented.** This is the spec for `~/.kodo/agents/` — the second
> agent root, alongside the one Kōdo ships. The rationale for choosing this
> shape over the alternatives is [TOP_AGENT_PROPOSAL.md](TOP_AGENT_PROPOSAL.md);
> the in-repo agent system it builds on is
> [ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md).

A user drops a bundle in `~/.kodo/agents/` and a new agent appears in the
picker. No code change, no restart, no Kōdo release.

---

## 1. The reserved prefix

**Every built-in agent's name begins `kodo_`** — `kodo_guide`,
`kodo_problem_solver`, `kodo_judge`, `kodo_coder`, `kodo_architect`, and so on
for all 26. **A user agent's name may not.**

That single rule is what lets both roots share one flat namespace. The registry
looks an agent up by name without caring which root it came from, and a
collision between the two is not merely unlikely — it is impossible, because a
user file carrying the prefix is refused before it is ever registered.

The prefix reaches everything a name reaches: the filename, the `<name>.json`
config, the sub-agent's spec file, and the generated `run_subagent_<name>` tool
a model actually calls. It does **not** reach what a *person* reads — the picker
still says "Guide" and "Problem Solver", because that is the `label`, not the
name.

`subagents` is reserved too, for a top-level agent's name: it is the directory
that holds the shared user sub-agents (§2), so an agent called `subagents` would
be indistinguishable from it.

> **No aliases.** The pre-rename workflow-mode vocabulary (`"guided"`,
> `"problem_solving"`) and the pre-prefix names (`"guide"`, `"judge"`) resolve
> to nothing. `TopAgent` has no `aliases` field at all. A stored selection that
> no longer resolves is reported to the user, not silently redirected — see §7.

---

## 2. Layout

```
~/.kodo/agents/
  reviewer/                    one directory per user top-level agent
    reviewer.json              how it is selected — a TopAgent config
    agent_reviewer.md          its prompt — exactly one per directory
  auditor-suite/
    auditor-suite.json
    agent_auditor-suite.md
  subagents/                   the shared user sub-agent directory
    subagent_scanner.md        a sub-agent's prompt
    scanner.json               that sub-agent's input/output contract
    subagent_scanner_critic.md
    scanner_critic.json
```

Three rules, and the reasons they are rules:

- **The directory name *is* the top-level agent's name**, and both of its files
  are named after it. A bundle cannot disagree with itself about what it is
  called, and Delete has one unambiguous thing to act on.
- **Exactly one `agent_*.md` per directory.** Two would make "which agent is
  this directory?" a question the name alone could no longer answer.
- **User sub-agents live in one shared directory**, not inside a bundle, because
  they are shared: any user top-level agent may list any of them, and a
  sub-agent published by two sources is one file rather than two copies free to
  drift.

A stray `README.md` or `.DS_Store` beside the bundles is skipped silently — only
directories are read as agents.

---

## 3. What goes in the files

### 3.1 A top-level agent

`agent_<name>.md` is an ordinary Kōdo agent prompt: YAML frontmatter, then the
system prompt body. Every frontmatter key documented in
[ADDING_A_SUBAGENT.md](ADDING_A_SUBAGENT.md) works, plus `version:`.

```markdown
---
name: reviewer
version: 1.2.0
display_name: Reviewer
capability: high
tools:
  - read_file
  - find_files
  - find_text_in_files
  - run_subagent
  - ask_user
subagents:
  - scanner
  - kodo_investigator
---
You are **Reviewer**. You audit an existing codebase against its specs.

{SHARED:working_rules}
{SHARED:security}
```

`<name>.json` says how it is chosen — the same `TopAgent` config the built-in
agents use ([_topagent.py](../src/kodo/agents/_topagent.py)):

```json
{
  "name": "reviewer",
  "label": "Reviewer",
  "description": "Audits an existing codebase against its specs.",
  "rank": 30,
  "selectable": true
}
```

Only `name` and `description` are required. `name` must equal the filename stem
*and* the directory name. Unknown keys are errors — a misspelled `"selectible"`
that was quietly ignored would produce an agent appearing in a picker it meant
to stay out of.

`"default": true` is **refused** on a user agent. Which agent a new session
starts on is Kōdo's to declare and the user's to override through the
`default_agent` setting ([SETTINGS.md](SETTINGS.md)) — not an installed agent's
to claim.

### 3.2 A sub-agent

Two files in `~/.kodo/agents/subagents/`: `subagent_<name>.md` (prompt) and
`<name>.json` (its `SubAgentSpec` — the typed contract that makes it
spawnable). Both are exactly what the built-in sub-agents use; the spec format,
including the `shape` builders and the `"shape": "raw"` escape hatch, is
documented in
[subagents/specs/_loader.py](../src/kodo/agents/subagents/specs/_loader.py).

A sub-agent with no spec beside it is a broken entry: the spec is what supplies
the `input_schema` its caller fills in and the `output_schema` it returns
through `return_result`.

### 3.3 Versions

`version:` in the prompt's frontmatter, free-form and **never compared**. It
exists so an install can show what you already have beside what you are about to
get, and let you decide (§4). An entry that declares none is shown as
`(unversioned)` — a missing version is a reason to look, not a reason to refuse.

Built-in agents carry Kōdo's own version, stamped by the registry rather than
written in any file: a packaged agent ships with the product and has no version
of its own to declare.

### 3.4 What is relaxed, and what is not

Two rules that bind a packaged agent are **relaxed** for a user agent:

| Rule | Built-in | User-installed |
|---|---|---|
| `{SHARED:working_rules}` + `{SHARED:security}` | required | optional |
| Artifact roles in `produces`/`consumes` | the closed built-in eleven | any string |

A user prompt may include the shared blocks and inherit Kōdo's injection-
resistance and working-rules prose, or write its own — the author's call. It may
also coin a role Kōdo has no word for (`threat_model`, `migration_plan`), because
a user pipeline legitimately has documents Kōdo's vocabulary does not cover.

What is **not** relaxed, in either root:

- an unknown `{SHARED:<name>}` block is an error, because it renders nothing at
  all — a silent failure whichever root it came from;
- every declared tool must resolve to a real `ToolSpec`;
- a declared `critic:` must resolve to something that really declares
  `role: critic`;
- every `subagents:` entry must exist;
- a consumed artifact role must be produced by *something* — the rule that
  actually matters survives the open vocabulary;
- `subagents:` and the `run_subagent` tool grant must be declared together.

### 3.5 Referring across the two roots

A user agent may name built-in sub-agents in `subagents:` (`kodo_investigator`,
`kodo_coder`, …), and a user sub-agent may pair with a built-in critic
(`critic: kodo_code_critic`). Both directions work because the prefix rule makes
the namespace unambiguous.

The reverse is not supported: a **built-in** agent never lists a user sub-agent.
Extending Kōdo's shipped pipeline would mean editing packaged frontmatter, and
an agent the user did not author changing behavior underneath them is exactly
the class of surprise that makes a bug report unanswerable.

---

## 4. Installing

Two sources, one shape. A source — a local directory or a git repository — is
laid out the way one bundle is *authored*:

```
<source>/
  reviewer.json
  agent_reviewer.md
  subagents/
    subagent_scanner.md
    scanner.json
```

Installing redistributes it to where the registry *reads* it: the top-level
agent's two files into `~/.kodo/agents/reviewer/`, everything under
`subagents/` into the shared `~/.kodo/agents/subagents/`. That redistribution is
why there is an installer at all rather than an instruction to copy a folder.

A source may carry a top-level agent, sub-agents, or both. Sub-agents alone is a
legitimate bundle: it publishes specialists for agents already installed.

### 4.1 Scan, then install

Reading a source and installing from it are deliberately **two steps**, because
of the conflict rule: when a source carries something already installed, you are
shown both versions and you decide, so a decision has to happen *between*
reading and writing.

```
  agent reviewer     installed: 2.0.0   incoming: 3.1.0
  subagent scanner   installed: 1.5.0   incoming: 1.6.0
```

The answer — **keep** what you have, or **replace** it with what the source
brings — applies to every conflicting entry at once. The comparison is rendered
once, server-side (`SourceScan.conflict_report()`), so the CLI and the Settings
panel ask the question with the same words.

For a repository that means cloning twice, once to scan and once to install,
matching [SKILLS.md](SKILLS.md) §2 and for the same reason: no temporary
directory has to stay alive across a round trip to the user. A source that
changes in between is reported as `missing`, not silently half-installed.

### 4.2 From the CLI

```console
$ python -m kodo --install-agent ./my-reviewer
agent     reviewer  3.1.0
subagent  scanner   1.6.0

Already installed:
  agent reviewer     installed: 2.0.0   incoming: 3.1.0
  subagent scanner   installed: 1.5.0   incoming: 1.6.0

Keep the installed versions, or replace them? [keep/replace] replace
installed reviewer
installed scanner

$ python -m kodo --install-agent https://github.com/owner/repo --yes
$ python -m kodo --list-agents
agent     reviewer  3.1.0
subagent  scanner   1.6.0
```

`--yes` answers the keep-or-replace question with *replace*.

### 4.3 From the Settings panel

The **Agents** section lists what is installed with each entry's kind, version
and description, and offers Install from a repository…, Install from a local
folder…, and Reload. Both install buttons go through the same modal, so the
keep-or-replace step is never skipped just because the source happened to be
local.

---

## 5. When a bundle is broken

> **Packaged agents fail fast. User agents fail visibly.**

A malformed *packaged* agent still raises `AgentLoadError` at construction and
the server does not start — a bug in the repo must stop the build. A malformed
*user* agent becomes a `BrokenAgent` row carrying the reason, is never
spawnable, and **every other agent still loads**.

This is deliberately not a second parser. The strict loaders are called
unchanged and the exception is turned into a row at the user root only — one
parser, two call sites, two regimes, the rule `kodo.skills` established.

Demotion **cascades**. Dropping one user agent can invalidate another that named
it, so cross-agent validation runs in a loop, re-checking whatever survived the
last round until nothing more is demoted.

Broken rows appear in `--list-agents`, in the Settings panel's Agents table, and
in the server log at startup. The row names the file, so the fix is usually
obvious:

```
agent     reviewer  BROKEN: tool 'reed_file' has no ToolSpec in kodo.toolspecs
subagent  scanner   BROKEN: no scanner.json beside it — a sub-agent declares its
                    input/output contract in a JSON file of the same name
```

---

## 6. Lifecycle

The registry is a **process singleton**, shared by every VS Code window on the
machine, and its validation is cross-agent — so unlike the skills store it
cannot simply re-scan on each lookup. `AgentRegistry.reload()` is the explicit
alternative: it builds a whole second registry over the same two roots and
adopts its state only once that has validated. **A failed reload leaves the
previous working set live**, so a broken edit cannot take the session you are in
down with it.

Reload is triggered by every mutating path — `agents.install`, `agents.delete`,
and `agents.reload` for a bundle edited by hand — so a newly installed agent is
selectable immediately, with no server restart.

No locking is needed: `get()` returns frozen dataclasses, so a session mid-turn
finishes on the definition it already holds and picks up the new one on its next
turn. That is the correct semantics, not a compromise.

### 6.1 Wire surface

Control connection only; full payloads in
[WS_PROTOCOL.md](WS_PROTOCOL.md) §7.6l.

| Message | What it does |
|---|---|
| `agents.list` | List user-installed agents and sub-agents, broken ones included |
| `agents.install_scan` | Read a source and report what installing it would do |
| `agents.install` | Install it, with the user's keep-or-replace answer |
| `agents.delete` | Delete one bundle, or one sub-agent's two files |
| `agents.reload` | Rebuild the registry from both roots |

Every mutating ack carries the refreshed listing, so a client redraws its table
from the response with no follow-up round trip.

---

## 7. When a session's agent goes missing

A user can now uninstall the agent a session is running. That session **says
so** rather than silently continuing as a different agent: an unrecognized
stored selection is returned unchanged rather than resolved to the default,
which routes the next prompt to a visible error naming the missing agent and
leaves the session in `intake` so another agent can be picked.

An *empty* selection is not a stale one — it is a session that never chose — so
it still takes the default.

The same path covers a session persisted before the `kodo_` rename: `"guided"`
names no agent now, so it is reported rather than quietly resolved.

---

## 8. Security

`SecurityLayer.evaluate` is deterministic, judges **per call on the tool and its
input**, and never considers which agent is calling. A user agent granting
`run_command` gets byte-identical gating to `kodo_problem_solver`, which already
holds `run_command`, `edit_file` and `create_file` and which the same user can
already type any instruction into. **The capability delta is ≈ zero.**

What changes is the *provenance of instructions*: a file that persists, is
shareable, and may have been downloaded — rather than a message typed a second
ago. `~/.kodo/agents/` is treated the way `~/.kodo/skills/` is: a directory on
the user's own machine, holding files they chose to put there. There is no
first-appearance trust prompt. See [SECURITY.md](SECURITY.md) for the model
every agent is judged under.

Install agent bundles from sources you trust, as you would any other code you
run.

---

## 9. Where the code lives

| Concern | Module |
|---|---|
| Scanning the user root, broken rows, reserved names | [agents/_userstore.py](../src/kodo/agents/_userstore.py) |
| Installing from a directory or a repo | [agents/_install.py](../src/kodo/agents/_install.py) |
| Both roots, both regimes, `reload()` | [agents/_registry.py](../src/kodo/agents/_registry.py) |
| The prompt parser (shared, unchanged) | [agents/_loader.py](../src/kodo/agents/_loader.py) |
| A top-level agent's config | [agents/_topagent.py](../src/kodo/agents/_topagent.py) |
| A sub-agent's contract | [agents/subagents/specs/](../src/kodo/agents/subagents/specs/) |
| The root paths | [project/_layout.py](../src/kodo/project/_layout.py) |
| CLI | [`__main__.py`](../src/kodo/__main__.py) — `--install-agent`, `--list-agents` |
| WS handlers | [server/_app.py](../src/kodo/server/_app.py) — `agents.*` |
| Panel UI | `kodo-vsix/src/settings-webview/AgentsSection.tsx`, `InstallAgentsModal.tsx` |

Tests: [test/test_user_agents.py](../test/test_user_agents.py) — open-world, one
deliberately broken bundle per case, each asserting the same property: this
entry degrades to a row and everything else still loads.
