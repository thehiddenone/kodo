# The Security Layer

How Kōdo decides, for every tool call an agent makes, whether to **allow** the
call or **ask the user for permission** — and how that decision is wired from
the `kodo.security` package through the tool dispatcher and the wire protocol
into the VS Code extension's permission panel.

Related docs: [TOOLS.md](TOOLS.md) (tool catalog, `intent` contract, impact
levels), [WS_PROTOCOL.md](WS_PROTOCOL.md) (§6.5 `prompt.permission`, §7.4b
`command_control.set`), [INTERNALS.md](INTERNALS.md) (package tiers),
[SECURITY_RULES_PLAN.md](SECURITY_RULES_PLAN.md) (phased design/decision
log for the `run_command` heuristic engine), and
[SECURITY_RULES_GUIDE.md](SECURITY_RULES_GUIDE.md) (a use-case-by-use-case
reference — "given this exact command, what happens and why," walked
through in the engine's own check order).

---

## 1. Overview

Every tool call — from the Guide, the Problem Solver, or any sub-agent — is
routed through one `ToolDispatcher.dispatch()` per agent run. Before a call
reaches its handler, the dispatcher consults the **security layer**
(`kodo.security.SecurityLayer`), which returns one of two verdicts:

- **`allow`** — dispatch proceeds normally.
- **`ask`** — the dispatcher fires a `prompt.permission` request to the
  client and blocks. The user's **Allow** lets the call proceed; **Deny**
  returns an error result to the agent *without executing the tool*,
  carrying the user's optional feedback text verbatim.

The layer never denies on its own. Every path that cannot be confidently
allowed ends at the user: the gate **fails closed to a prompt**, never open.
Judgement is **fully deterministic** — heuristic rules over the structural
parse, never an LLM (see [SECURITY_RULES_PLAN.md](SECURITY_RULES_PLAN.md)
for the plan that replaced the former LLM intent judge and its rationale) —
so the same call always produces the same verdict, in microseconds.

There is deliberately no bypass parameter: possession of the dispatcher means
passing through the gate. Tests and legacy callers construct dispatchers
without a `security=` argument, which disables gating for that instance only.

## 2. The three postures

The layer's mode is the session's existing **Command Control** toggle
(`command_control`: `permissive` / `defensive` / `smart`, default `smart` —
see WS_PROTOCOL.md §7.4b). It is **never frozen**: the dispatcher reads it
live per tool call, so flipping the toggle mid-turn affects the very next
call. The judgement inputs are each tool's declared
`SecurityImpact` level (`kodo.toolspecs._spec`, 7 levels NONE→CRITICAL) and,
for `run_command`, static shell analysis.

| Posture | Rule |
|---|---|
| `permissive` | Threshold only: everything **below CRITICAL** is allowed. |
| `defensive` | Threshold only: everything **at or above MODERATE** asks. |
| `smart` | Below HIGH → allow (workspace-confined by construction). **HIGH → judged individually by a per-tool static policy** (§3). CRITICAL → always ask. |

Current impact assignments (see `toolspecs/_*.py`): HIGH = `run_command`,
`filesystem`, `rollback`, `toolchain_deps`, `disable_autonomous_mode`;
MODERATE = `edit_file`; no tool is SEVERE or CRITICAL today. So in practice:
permissive allows everything, defensive asks on the six MODERATE+ tools, and
smart judges the HIGH ones. A HIGH tool *without* a per-tool policy in §3
asks unconditionally — adding a HIGH tool means adding its policy.

Two overrides apply in every posture:

- **Autonomous mode ⇒ permissive.** While `effective_autonomous` is true the
  layer operates as `permissive` server-side — the twin of the client forcing
  the Command toggle to Permissive (and locking it) while Autonomous is in
  effect: there is no user present to answer a prompt.
- **`disable_autonomous_mode` is never gated.** Its only effect is returning
  control to the user; prompting for permission to do that would be
  self-defeating.

The separate **Edit Control** toggle (`edit_control`) is a review-workflow
control, not part of the security layer — enforced independently by
`ToolDispatcher.__edit_review_gate` for `create_file`/`edit_file` only, always
evaluated *after* this gate. See WS_PROTOCOL.md §6.5b/§7.4a for the exact
rules (there is no Edit Control section in this document).

## 3. SMART mode

### 3.0 Per-tool policies for the non-command HIGH tools

Three HIGH tools are settled by direct structural policies
(`kodo/security/_layer.py`), no command parsing involved:

- **`filesystem`** — every path is resolver-confined to the workspace at
  dispatch and workspace changes are checkpointed, so `delete_file` /
  `copy_*` / `move_*` **allow**; only **`delete_dir`** (recursive, the one
  costly fat-finger) **asks**. An unrecognized/missing `operation` asks
  (fail closed).
- **`rollback`** — **allow**: workspace-confined by construction (checkpoint
  mirror), and the Guide is contractually required to confirm via `ask_user`
  first.
- **`toolchain_deps`** — a plain registry `name` (+ ordinary `version`
  constraint) **allows** (the dependency sub-agent's own commands are gated
  individually anyway); a name/version shaped like a URL, VCS ref
  (`git+…`), local path, or option injection (leading `-`) **asks** — the
  supply-chain shapes worth a user's eyes.

### 3.0a `temporary`: the session's private scratch directory

Six file tools — `create_file`, `create_directory`, `edit_file`,
`filesystem`, `find_files`, `find_text_in_files` — accept an optional
`temporary: true` input. When set, `kodo.tools.Tool.resolve_path` resolves
every path the call touches under `~/.kodo/sessions/<session_id>/tmp`
(`kodo.project.session_temp_dir`) instead of the project root/workspace
folders — confined there the same way `resolve_within` confines Guided-mode
paths to the project root (relative paths land inside it, absolute paths
must already be inside it, or a `PermissionError` — not even an `ask` —
comes back).

This bypasses the posture/impact judgement above entirely: `SecurityLayer.
evaluate` allows the call outright, in every posture, before the
threshold/per-tool logic runs (`_TEMP_ALLOWED_TOOLS` in `_layer.py`) —
including `filesystem`'s `delete_dir`, which otherwise always asks. The
exemption is symmetric on the checkpoint side:
`kodo.runtime._engine._checkpointing.CheckpointCoordinator.prepare` skips its
mirror snapshot/commit outright for a `temporary` call, so nothing there ever
earns a checkpoint, an undo/rollback entry, or a Guided `new_revision`
attribution — the scratch directory sits outside every project's git mirror
by construction. See doc/TOOLS.md and `preamble_performance.md`'s "Scratch /
Temporary Work" section for the agent-facing contract.

`get_root_paths` surfaces this same directory's path (via `temporary: true`)
without itself being a mutating call, so it is simply `NONE`-impact and
always allowed like any other read.

**`run_command`'s `working_dir` is a separate, narrower exemption.** It has
no `temporary` field of its own — `_TEMP_ALLOWED_TOOLS` doesn't cover it — but
in Guided mode `ProjectPathResolver` (`kodo.tools._paths`) now accepts an
absolute `working_dir` under the *dispatching run's own* scratch directory as
an `extra_roots` entry, alongside the project root, so the call reaches its
handler instead of failing at dispatch with a `PermissionError` (Problem
Solver's logical resolver already allowed any absolute path). This only
widens where the resolver lets the command *run*; `command_may_mutate`/
`analyze_command`'s static `outside_paths` check (§3.1) still treats the
scratch directory as outside the workspace like any other non-root,
non-OS-temp path — a command whose *arguments* (not just its `working_dir`)
explicitly name an absolute path under the scratch directory still asks in
`smart` posture, and mutating writes there still never earn a checkpoint
(`RootMirrorManager` only tracks the known project/workspace roots, so a
scratch-directory path resolves to no root and `prepare`/`commit_for_path`
are no-ops).

### 3.1 `run_command`: static workspace analysis first

`kodo.security._analysis.analyze_command()` runs over the structural parse
from `kodo.shellparser` (the POSIX parser, or the PowerShell/Windows parser on
`os.name == "nt"`; see §5) and produces three facts:

1. **`outside_paths`** — argument or redirection-target tokens that resolve
   to a path **outside every workspace root and outside the OS temp
   directory**. Only tokens that *can* escape are resolved: absolute paths,
   `~`, Windows drive/UNC forms, and relatives containing `..` (joined
   lexically against the call's working directory). Plain relative tokens
   can't escape the resolver-confined cwd and are skipped — which also keeps
   subcommand words (`install`, `build`) from being misread as files.
   Executables are exempt (running `/usr/bin/python` is normal; the program
   is not a *target*). Device sinks (`/dev/null`, `NUL`, …) and fd-merges
   (`2>&1`) are exempt. `--flag=value` values are checked. A path under
   `kodo.common.system_temp_roots()` (`tempfile.gettempdir()`, plus the
   literal `/tmp` on POSIX even when `gettempdir()` resolves elsewhere, e.g.
   macOS's per-user `TMPDIR`) is never counted as outside — scratch files
   there are expected agent territory, not a workspace escape. The same
   helper gates `kodo.tools._paths.resolve_within` (the Guided-mode file-tool
   resolver), so `create_file` / `edit_file` / `filesystem` / `read_file`
   can address the temp directory too, not just `run_command` (§4).
2. **`unresolved`** — substitution snippets (`$(...)`, backticks, `${VAR}`,
   `$VAR`/`$env:VAR`, `%VAR%`) that defeat static resolution. Substitutions
   are **masked before parsing** so `$(pwd)/y` stays one (skipped) token
   instead of shlex splitting off a bogus absolute `/y`. The *command*
   substitutions (`$(...)`, backticks — they execute code) are additionally
   surfaced as **`command_subs`** for the rule engine's recursion.
3. **`read_only`** — every pipeline executable, **after wrapper-peeling**
   (`env`/`nohup`/`timeout`/… — see point 4), is on a conservative read-only
   allow-list *and* no redirection writes a file. The list is stricter than
   the checkpoint heuristic's (`find`, `sort`, `xargs`, `tee` are all
   excluded) because a wrong answer here skips a review, not just a no-op
   git sweep. The fast path operates on the *normalized* segments, not the
   raw parse — otherwise a wrapper like `env` (itself read-only) could hide
   an unpeeled mutating command behind it (`env rm -rf x` must resolve to
   `rm`, not short-circuit on `env`). `date` and `hostname` are deliberately
   **not** on this list — both have a mutating form and are judged
   per-segment instead (§3.2 step 4).
4. **`segments`** — the normalized per-segment view
   (`kodo.security._classify`): canonical executable (transparent wrappers
   like `env`/`nohup`/`timeout`/`mise exec … --` peeled, `python -m mod`
   re-classified as `mod`, PowerShell aliases resolved to their cmdlet),
   subcommand, flags, nested `sh -c`-style command strings, inline-code
   opacity (`python -c`, `-EncodedCommand`), and per-segment substitution /
   pipe / write-redirection facts.

### 3.2 The heuristic rule engine

Every `run_command` call goes through `kodo.security._rules.
evaluate_command()` — a deterministic verdict ladder, first hit wins
(design + phased plan: [SECURITY_RULES_PLAN.md](SECURITY_RULES_PLAN.md)):

1. **Workspace escape** — any `outside_paths` → **ask** (reason lists the
   paths), **judged per segment**, not over the whole line — a plain
   redirection/argument outside every workspace root and the OS temp
   directory raises that specific segment to the user; a segment whose
   executable is on the read-only/`cd` allow-list may additionally carry a
   permanent-rule offer keyed on the resolved path itself (§3.2c). A
   temp-dir target simply skips this step for its own segment — it still
   runs the rest of the ladder, so e.g. `rm -rf /tmp/x` still asks
   (destructive, category rule) while `cat /tmp/x` or `touch /tmp/x` allow.
2. **Command substitutions** — each `$(...)`/backtick snippet is recursively
   evaluated as its own command (depth-capped at 3); a dangerous inner
   command asks. `echo $(date)` allows; `echo $(rm -rf /)` asks.
3. **Read-only fast path** — `read_only` **and no segment has an
   outside-workspace finding** → **allow**. The second half of that AND is
   new (§3.2c): `read_only` alone only knows about executables and writes,
   nothing about paths, so a lone `cat /etc/hosts` — every executable
   "read-only", nothing written — must not slip through just because step 1
   moved from a whole-line short-circuit to a per-segment one. Value
   expansions (`$VAR`) are tolerated in the fast path itself: an unknown
   value fed to a pure reader cannot mutate anything.
4. **Per-segment rules** — every pipeline segment must individually clear:
   - *structural red flags*: a bare shell as a pipe target (`curl … | sh`),
     inline/encoded code (`python -c`, `-EncodedCommand`,
     `Invoke-Expression`), `xargs` feeding stdin-supplied arguments to a
     non-read-only child; nested shell strings (`sh -c "…"`, `cmd /c …`)
     recurse through the whole ladder;
   - **dual-mode commands** (`kodo.security._rules._DUAL_MODE`): a small set
     of commands are benign when read-only and dangerous when mutating in a
     way a blanket allow-list or a `flags_any` rule can't express (a
     positional *value*, not a flag, decides) — `sysctl` (`-w`/assignment
     form writes a live kernel parameter; `-a`/a bare key reads), `ulimit`
     (a numeric/`unlimited` value sets a resource limit; a bare query
     reads), `date` (`-s`/a bare positional sets the clock; a `+FORMAT`
     string reads), `hostname` (a positional sets it; bare reads). Matched
     by executable name the same way `xargs` is, before the generic
     rule-table lookup; an unresolvable substitution asks rather than
     getting the read-only leniency below, since it could be the mutating
     form. `uname` was reviewed and has no mutating form on any platform, so
     it stays on the plain read-only list.
   - the **ordered `CommandRule` table** (`kodo.security._defaults`, one
     table per dialect, ask-rules and allow-rules interleaved, specific
     before general). Ask-rules carry a danger **category** — `deployment`
     (`git push`, `npm publish`, `kubectl`, cloud CLIs …), `destructive`
     (`rm -r`, `git reset --hard`, `dd` …), `system` (`sudo`-adjacent
     installs, services, `npm -g`, `pip --user` …), `network` (`curl`,
     `ssh`, `nc` …), `privilege` (`sudo`, `RunAs`), `obfuscation` — and a
     fixed one-sentence reason shown in the prompt. Allow-rules cover the
     benign development set: **build/test/lint runners are unconditionally
     allowed by decision** (`make`, `pytest`, `npm run`, `cargo build`,
     `hatch run` …), plus safe VCS subcommands and in-workspace file
     mutators (their visible path targets were already outside-checked, and
     workspace changes are checkpointed). An allow-rule match is **voided by
     an embedded substitution** — `mv $SRC $DST` asks.
5. **Default: ask** — "`foo` is not in the known-safe command set", the same
   deterministic sentence every time. The known friction here is inline
   interpreter code (`python -c "…"`), which is opaque by design and always
   asks; agents should prefer `python -m` or a script file, both of which
   the table allows.

Each rule (and the default-ask) also carries the Phase 2 fields:
`rule_eligible` (may a persistent user rule override this ask?) and the
generalized `(executable, subcommand)` `shape` a user rule stores —
destructive / privilege / obfuscation findings are never eligible (`sudo`/
`su`/`doas` and the dedicated `eval` ask are `privilege`/`obfuscation`, so
they can never become a permanent rule, no matter where they sit in a
pipeline). Two things happen with these, **per pipeline segment**, both in
`evaluate_command()` itself (§3.2a/§3.2b):

1. **Known-rule lookup.** If a segment's ask `shape` is already in the
   caller's `known_rules` (session rules ∪ the global store), that segment
   silently becomes an **allow** instead — no prompt for it at all.
2. **Offer computation.** Otherwise, if the segment's ask is `rule_eligible`
   *and that segment itself* (not the whole command line) has no
   substitution, nested shell, or opaque inline code, it carries a
   `rule_offer` of its own shape. `git push origin main` is offered;
   `echo $FOO && git push` still offers `git push` even though the `echo`
   segment carries a substitution — the gate is per segment, not per line
   (§3.2b). A plain, workspace-confined redirection (`cat > out.txt`) no
   longer disqualifies a segment's offer either — see §3.2b for why.

   The path-like-argument check (`/`, `\`, `..`, `~`, a drive letter) is
   **tiered by `known_command`** — whether the ask matched an explicit,
   named `CommandRule` (`git push`, `apt install`, `npx`, …) versus fell
   through to the generic "not in the known-safe command set" default:
   - **Known**: any path-like argument is ignored. The rule's danger is
     already bounded by its category, and granting it already generalizes
     over everything after the subcommand (`git push` generalizes over the
     remote) — a path there is no different. `apt install ./local.deb` is
     offered as `("apt", "install")`.
   - **Unknown**: a path-like argument *after* the subcommand still
     disqualifies the offer — `pytest ../other/` is not offered, because the
     stored `(executable, subcommand)` shape can't capture that trailing
     path, and a future call with a *different* path would silently match
     the same rule. A path-like **subcommand itself** is fine, though: a
     bespoke single-argument CLI like `1brc ./measurements.txt` is offered
     as `("1brc", "./measurements.txt")` — the shape already pins the rule
     to that exact literal text, so `1brc ./other.txt` produces a different,
     non-matching shape and still asks. This is what makes an unknown
     command's offer effectively an "exact literal match," while a known
     command's is a true generalization.

The `intent` field remains mandatory on first-degree mutators (TOOLS.md §8A)
as permission-panel and feed metadata, but it is **no longer a judgement
input**.

### 3.2a Phase 2: "always allow" user rules

A user rule is *exactly* the generalized `(executable, subcommand)` shape —
never arguments, paths, flags, or the literal command line
(SECURITY_RULES_PLAN.md §2.1). Two independent, additive scopes:

- **Session** — `kodo.runtime.SessionState.security_rules` (a
  `frozenset[tuple[str, str]]`), mirrored into `kodo.state.TransientStore`
  the same way `command_control` is (`TransientStore.add_security_rule`,
  persisted in `transient.json`) — survives a server crash/resume, dropped
  when the session itself ends. `kodo.security` never imports
  `kodo.runtime`/`kodo.state`; this scope is ordinary runtime state the
  layer only ever *receives*, via `SecurityLayer.evaluate(session_rules=…)`.
- **Global** — `kodo.security._store` (`add_global_rule`/`global_rules`), a
  flat JSON list at `~/.kodo/etc/security_rules.json`, beside the server's
  `settings.json` (`kodo.project.WorkspaceLayout` — one instance per
  machine, shared by every VS Code window's session). Read fresh from disk
  on every `run_command` judgement (the file is tiny; no cache to
  invalidate), so a rule granted in one window's session is visible to
  every other open session's very next matching call. This is a
  deliberately **user-wide, cross-project** relaxation of the user's own
  future asks — not a per-project trust boundary. (The unrelated,
  never-wired `ProjectLayout.security_json` stub predates this decision and
  is not used by Phase 2; see SECURITY_RULES_PLAN.md.)

The permission prompt is where a rule is granted: `prompt.permission`
carries one `parts` entry — `{reason, rule_offer}` — per elementary command
still needing attention (§3.2b); `prompt.permission.response` carries
`remember: (string | null)[]`, parallel to `parts`. The server never trusts
a client-declared shape — only a `remember` entry on an `allow` for a part
that itself carried a `rule_offer` is acted on, and even then the *server's
own* `rule_offer` shape is what gets granted, not anything read back off the
wire. See §4 for the exact call sequence.

### 3.2b Compound commands split per elementary command

A pipeline/`&&`/`;`/`|` chain does **not** collapse to one undifferentiated
ask. `evaluate_command()` judges every segment, silently allows whatever
already has a matching rule or an unconditional built-in allow, and
collects every remaining ask — deduplicated by `(executable, subcommand)`
shape when the same elementary command repeats in the chain — into
`RuleDecision.parts`. `git status && ./deploy.sh staging && ./deploy.sh prod`
produces two parts (`deploy.sh staging` and `deploy.sh prod` are different
shapes; `git status` is already unconditionally allowed and never becomes a
part at all); `./deploy.sh staging && ./deploy.sh staging` deduplicates to
one. A part whose ask isn't `rule_eligible` (e.g. `sudo`, always privilege-
category) still appears with its own `reason`, just with `rule_offer: null`
— no checkbox, but the user still sees why that part asks.

Two whole-line gates from the original design were narrowed to per-segment
once the split existed:

- **Redirection no longer disqualifies an offer.** The worry it guarded
  against was a script piped into a shell/interpreter via `<`/`<<`
  (`bash << EOF`) — that's caught upstream by `nested_command`/
  `nested_opaque` (§5), which were never offer-eligible to begin with. A
  plain, workspace-confined redirection (`cat file.txt > out.txt`) has
  nothing left to disqualify: the outside-workspace check (§3.2 step 1)
  still runs on every future invocation regardless of any granted rule, so
  `cat > file.ext << 'EOF' … EOF` — the exact friction case §2.4a.6 of
  SECURITY_RULES_PLAN.md called out as permanently un-offerable — is now
  offered like any other unknown command.
- **A value substitution (`$VAR`/`%VAR%`) only disqualifies its own
  segment**, not the whole line. `mycli $FOO && othercli two` still offers
  `othercli two`; only the segment actually carrying the substitution loses
  its checkbox.

`eval` (POSIX) is a dedicated, always-ask, never-offer-eligible rule
(`category="obfuscation"`), mirroring how `Invoke-Expression` already works
on Windows — previously it fell through to the generic "unknown command"
ask, which *is* offer-eligible.

### 3.2c Workspace-escape path offers for read-only/`cd` commands

A curated bucket of non-destructive commands — the read-only allow-list
(`cat`, `ls`, `grep`, `head`, …) plus `cd`/`Set-Location` — can be offered a
permanent rule even when the only issue is a path outside the workspace,
where every other outside-workspace ask (§3.2 step 1) never offers
(SECURITY_RULES_PLAN.md §2.7). The offer's shape is different from every
other category: `(executable, resolved_absolute_path)`, not `(executable,
subcommand)` — matching a future call means resolving *that* call's own
argument (relative or absolute, against its own cwd) and comparing the
resolved form, not literal string equality. `cd ../kodo` and `cd
/Users/dev/dev_root/kodo`, from the same cwd, resolve to the same string and
hit the same granted rule.

- **One offer per distinct resolved path**, not per command — `cat
  /etc/hosts /etc/passwd` offers two independent checkboxes; a grant for one
  executable doesn't cover another (`cat /etc/hosts && grep x /etc/hosts`
  still asks twice, once per executable).
- **Integrates with the §3.2b split**: `cd /outside/path && git status`
  offers exactly one part — `git status` doesn't even appear, since it was
  already silently allowed.
- **Segment-wide, not argument-wide**: a write anywhere in the segment
  (`writes_file`) disqualifies every path offer in that segment, including
  an unrelated read target (`cat /etc/hosts > /etc/hosts2` — neither path is
  offered).
- **A small sensitive-path denylist** (`~/.ssh`, `~/.aws`, `~/.gnupg`,
  `~/.kube`, `~/.docker`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`,
  `~/.config/gcloud`) is never offer-eligible even for an otherwise-eligible
  command — the ask still happens, just with no checkbox for that path.
- **Nested/substituted contexts stay non-offerable for free** — the existing
  command-substitution/nested-shell wrapping (§3.2 steps 2/4) already
  discards a recursive `evaluate_command()` call's `.parts`/`.rule_offer`
  unconditionally on failure, so an eligible command wrapped in `bash -c
  "cat /etc/hosts"` never surfaces its own offer (a `known_path_rules` grant
  still silences the wrapped occurrence, same as a bare one).

**Storage is a separate store**, not a new field on the existing one:
`~/.kodo/etc/security_path_rules.json` (global) and a parallel
`TransientStore.security_path_rules`/`SessionState.security_path_rules`
(session) — mirroring the command-shape machinery, kept genuinely separate
since the two rule kinds are matched with different semantics (literal vs.
resolve-then-compare) and the existing schema has no "kind" discriminator to
extend safely. **The wire protocol needs no change at all**: `AskPart`
gained a Python-internal `kind: "command" | "path"` field that routes
`kodo.tools._dispatch.__security_gate`'s grant call to `add_security_rule`
or `add_security_path_rule`, but `runtime._gates.fire_permission` only ever
serializes `{reason, rule_offer}` onto the wire — `kind` never crosses the
socket, and the server always grants from its own in-memory `AskPart`, never
from anything read back off the client (§3.2a's "never trusts a
client-declared shape" invariant, unchanged). kodo-vsix needed **zero**
changes: `rule_offer: {executable, subcommand}` was already rendered as
fully opaque strings at every hop, so a resolved absolute path in the
`subcommand` slot renders correctly as-is.

## 4. Wiring

```
ToolDispatcher.dispatch(tool, input, tool_use_id)          kodo/tools/_dispatch.py
  ├─ intent presence check (unchanged)
  ├─ __security_gate():
  │    decision = ctx.security.evaluate(                   kodo/security/_layer.py
  │        tool_name, tool_input,
  │        command_control = ctx.session.command_control,  ← live, never frozen
  │        autonomous      = ctx.session.effective_autonomous,
  │        default_cwd     = ctx.resolver.default_cwd,
  │        roots           = ctx.root_paths,
  │        session_rules   = ctx.session.security_rules,   ← merged with the global store inside
  │        session_path_rules = ctx.session.security_path_rules)  ← ditto, §3.2c
  │    "allow" → proceed
  │    "ask"   → ctx.gate.fire_permission(..., parts=decision.parts)
  │              → prompt.permission (kind=request)        WS_PROTOCOL.md §6.5
  │              → user allows, remember=["session"/"global"/null, …]  ← parallel to parts
  │                 → for each (part, scope) in zip(parts, remember):
  │                      part.rule_offer is not None and scope set
  │                         → part.kind == "path"
  │                              → ctx.services.add_security_path_rule(scope, *part.rule_offer)
  │                           else → ctx.services.add_security_rule(scope, *part.rule_offer)
  │                 → proceed
  │              → user denies → {"error": "The user DENIED …"} (tool NOT run;
  │                 any `remember` on a denial is ignored — see §3.2a)
  └─ tool_cls(ctx).handle(input)
```

`add_security_rule` (`kodo.tools.EngineServices` protocol) reaches
`WorkflowEngine.add_security_rule` (`kodo/runtime/_engine/_core.py`):
`"session"` updates `SessionState.security_rules` and
`TransientStore.add_security_rule` together (the same session/transient
pairing `handle_command_control_set` uses); `"global"` calls
`kodo.security.add_global_rule` directly — no session-state mirroring
needed, since every session already reads the global store live per call.

- **Layering.** `kodo.security` imports only `kodo.common` (the
  OS-temp-directory helper, see below), `kodo.toolspecs` (the catalog),
  `kodo.shellparser` (the parse), and `kodo.project` (`WorkspaceLayout`,
  for the global rule store's on-disk location only) — it sits beside
  `toolspecs` in the import graph and is consumed **only by `runtime`**.
  `kodo.tools` never imports it: the dispatcher sees the layer through the
  `SecurityLike` / `SecurityDecisionLike` structural protocols in
  `tools/_context.py`, exactly as `GateLike` decouples it from `runtime`.
  Session-scoped rules are the one Phase 2 exception to "security never
  reaches into runtime" — they flow the *other* direction, as a plain
  `frozenset` argument the caller passes into `evaluate()`, so
  `kodo.security` still never imports `kodo.runtime`/`kodo.state`. The
  engine constructs one `SecurityLayer` per `WorkflowEngine` and passes it
  into every dispatcher via `_make_dispatcher` — so the Guide, the Problem
  Solver, and **every sub-agent** flow through the same gate.
- **The OS temp directory carve-out spans two independent gatekeepers**, not
  just the security layer: `kodo.common.system_temp_roots()` (T0, no
  intra-kodo dependencies) is consumed by both `kodo.security._analysis`
  (the `run_command` workspace-escape check above) and
  `kodo.tools._paths.resolve_within` (the `ProjectPathResolver` used in
  Guided mode, which otherwise raises `PermissionError` — not even an
  `ask` — for any file-tool path outside the locked project root, before the
  security layer is ever consulted). Routing the shared fact through
  `kodo.common` rather than a direct import keeps the `kodo.tools` /
  `kodo.security` decoupling above intact. `LogicalPathResolver` (Problem
  Solver mode) already took absolute paths as-is, so it needed no change —
  the temp directory was already reachable there.
- **`SessionLike`** carries `command_control` and `security_rules` (both
  read live per call — a rule granted mid-session applies to the very next
  matching call, same as a `command_control` toggle).
- The **decision** (`SecurityDecision`) carries `action`, a one-sentence
  `reason`, a `source` tag (`policy` / `threshold` / `workspace` /
  `static` / `rules`) that is logged for every call, and (for `run_command`
  only) `rule_offer` (the first asking part's shape, kept for logging/tests)
  and `parts` — every elementary command still needing attention (§3.2b),
  the field `fire_permission` and the wire actually use.

## 5. The shell parsers

`kodo.shellparser` stays **parse-only and judgement-free** (the checkpoint
heuristic and the security layer each apply their own classification over the
same structural view):

- `parse_command()` — the pre-existing POSIX/`shlex` tokenizer.
- `parse_powershell_command()` — a hand-rolled PowerShell/Windows
  tokenizer producing the same `ParsedCommand`/`Segment`/`Redirection`
  dataclasses. Understands `;` `|` `||` `&&` `&` separators (a lone `&` at
  segment start is the call operator and is dropped), single/double quoting
  with `''`/`""`/backtick escapes, backtick escaping outside quotes, and
  stream-qualified redirections (`2>`, `*>>`, `3>&1`, …; merge targets like
  `&1` are kept verbatim for callers to recognise). It never raises and
  covers `cmd.exe` syntax by overlap.

Both parsers also flatten **bare subshell/brace grouping** — POSIX `(...)`
and `{ ...; }`, PowerShell `(...)` and `{...}` (including the `& { cmd }`
call-operator + script-block form) — distinct from `$(...)` command
substitution (which already executes and is recursively judged, §3.2 step
2). Grouping doesn't change what runs inside, so it's simply dropped,
letting the separators already present inside do their normal job:
`(cmd1 && cmd2)` parses identically to `cmd1 && cmd2`. This is what lets a
fully-benign subshell (`(cd dir && git status)`) auto-allow instead of
always asking, and lets a dangerous one get its precise category/reason
(`destructive`, `network`, …) instead of a generic "unknown command" for
the bogus `"("`/`"{"` executable that used to result. Quoted literal parens
(`grep "(error)" file.txt`) and non-grouping uses (`/tmp/{a,b}` brace
expansion, `find`'s `{}` placeholder) are never touched — POSIX only
strips a token when it is built *entirely* from operator characters (a
quoted or word token always contains something else), and PowerShell only
strips unquoted occurrences (its quote handling already consumes quoted
content whole, before the grouping check runs). This is deliberately
**bounded**: real control-flow forms (`if (...) { ... }`, `foreach`,
`while`) are not parsed and still fail closed to ask, unchanged — only the
common "just wrap a command" forms are flattened.

`parse_command()` also extracts **here-document bodies** (`kodo.shellparser.
_parser._extract_heredocs`) before tokenizing: `<<DELIM`/`<<-DELIM` through
the matching terminator line is pulled out as a separate `Redirection.
heredoc_body` rather than being left in the token stream, where it used to
be misread as bogus trailing words/segments of whatever command preceded
it — `cat > out.cpp << 'EOF'` followed by a C++ snippet would pick up the
snippet's first token as a fabricated "subcommand" (`'cat static' is not in
the known-safe command set`) purely because the body happened to contain
shell metacharacters (`;`, `&&`, `()`, …) the tokenizer had no way to know
were data, not syntax. Handles multiple heredocs on one line (`cmd1 <<A |
cmd2 <<B`) in the real left-to-right order shells resolve them.

This was more than a confusing-reason bug: a **bare shell fed a heredoc**
(`bash << 'EOF' … EOF`, no `-c`, no script argument) used to have its
misparsed body tokens satisfy the "`sh build.sh` runs a workspace script"
allowance (`if segment.args: return ok`) — silently **allowing arbitrary
shell code smuggled in over a heredoc**, e.g. `bash << 'EOF'\nrm -rf
important/\nEOF` asked nothing. `kodo.security._classify._heredoc_nested_command`
closes this the same way `-c`/`-e` already work: a bare shell's heredoc body
becomes its `nested_command` (recursively judged, same as `bash -c "…"`); a
bare script-interpreter's (`python`, `node`, `ruby`, `perl`, …) becomes
`nested_opaque` (statically unanalyzable, always asks) — in both cases only
when there's no other positional argument (`bash script.sh << EOF` feeds the
heredoc to *script.sh*'s stdin as data, the same trust the flagless form
already gets; the `-c`/`-e` inline-code rules apply identically). Every other
receiving command (`cat`, `tee`, `mysql`, …) simply has its heredoc body
discarded from the token stream — it was already just data, and the fix's
only job there is to stop it from polluting `args`/`subcommand`.

The security layer picks the dialect by platform (`os.name`).

## 6. Wire protocol & VSIX UI

**`prompt.permission`** (server → client, `kind=request`; the reserved
`SREQ_PROMPT_PERMISSION` constant is now emitted):

```json
{ "type": "prompt.permission",
  "tool_call_id": "toolu_…",
  "tool_name": "run_command",
  "external_name": "Run Command",
  "risk": "High",
  "intent": "Install the test runner the plan's step 3 requires",
  "reason": "The command targets paths outside the workspace: /etc/hosts.",
  "params": [ { "name": "command", "value": "…" }, … ],
  "recovered": false,
  "parts": [
    { "reason": "'push' publishes commits to a remote.", "rule_offer": { "executable": "git", "subcommand": "push" } }
  ] }
```

`recovered` (default `false`) is `true` only when the prompt is for a
*salvaged malformed tool call* — see §9. The client renders an extra warning
banner above the reason when it is set.

`params` is the customer-visible preview: input properties projected through
the tool's `input_visibility` map (hidden properties never reach the prompt),
values truncated at 400 chars. `parts` (default `[]`) is one `{reason,
rule_offer}` per elementary command still needing attention (§3.2b) — a
compound pipeline/`&&`/`;` chain may carry several, each independently
offerable; the client shows two "always allow" checkboxes for every entry
whose `rule_offer` is non-null. Response (`kind=response`, correlated by id):

```json
{ "type": "prompt.permission.response", "action": "allow" | "deny", "feedback": "…" | null, "remember": ["session" | "global" | null, …] }
```

Malformed/unknown actions are treated as **deny** server-side. `remember`
(default `[]`) is an array parallel to `parts` — the checkbox scope chosen
for each entry, if any — see §3.2a/§3.2b/§4 for how the server verifies and
applies it.

**VSIX**: `session-controller.ts` caches the request as `pendingPermission`
(re-posted to the webview on rehydrate, like the approval gate) and forwards
it as a `permission_request` message; `PermissionPanel.tsx` renders in place
of the prompt input (the `ApprovalGate` pattern) with the tool name, a risk
badge, the declared intent, the layer's reason, the parameter rows, an
optional feedback textarea, and **Allow** / **Deny** buttons. A single
`parts` entry renders exactly as before: when its `ruleOffer` is set, two
mutually-exclusive checkboxes ("Always allow `<shape>` — this session" /
"— all sessions") appear above the buttons. More than one part renders the
top `reason` as a summary, then one block per part with its own reason line
and its own checkbox pair. Every checked scope (or `null`) rides along with
whichever button is clicked as `permission_respond`'s `remember` array,
index-aligned with `parts` — the server only *acts* on an entry alongside
Allow, and only for a part that actually carried a `rule_offer` (§3.2a), so
a stray checked box on Deny is inert. The panel is **transient** — never a
session entry: the gated tool call's own card is already in the feed and its
result records the outcome (a denial is visible as the tool's error result).
Reconnects re-deliver an unanswered request via the standard `Outbox`
buffer-and-replay.

The former **`security.judging`** event (and its "Evaluating Kōdo's action…"
`SecurityJudgingIndicator`) is **retired**: it existed to cover the LLM
judge's multi-second silent round, and the rule engine has no such gap —
verdicts are effectively instant. The constant is removed from the wire
protocol (WS_PROTOCOL.md) and the VSIX no longer handles the message.

**`agent.tool_call_prep` vs. `agent.tool_call_in_progress`** (WS_PROTOCOL.md
§5.5/§5.5a): the tool call's card appears on `agent.tool_call_prep`, sent
*before* `ToolDispatcher.__security_gate` runs — before it's known whether
the call will be asked or waved straight through. For `run_command` this
used to be a bug: the client stamped the card's `startedAt` (which drives
the "Waiting for tool output" timeout progress bar) at that same moment, so
the bar's clock ran through the `prompt.permission` wait — which can outlast
the command's own timeout, making an in-flight, healthy command look like it
had already timed out.

The fix: `ToolDispatcher.dispatch` now calls
`EngineServices.notify_tool_call_in_progress(tool_use_id)` — which fires
`agent.tool_call_in_progress` — right after `__security_gate` returns (allowed
outright, *or* the user granted permission), immediately before the tool
handler runs, gated to `run_command` (the only tool the client animates a
timeout for). The client no longer stamps `startedAt` on
`agent.tool_call_prep`; it stays `null` (progress bar hidden) until
`agent.tool_call_in_progress` arrives for that `tool_call_id`, which is when
the bar actually starts. This holds for **every** posture: any `ask` verdict
funnels through the same post-`__security_gate` choke point in `dispatch()`,
so the bar is deferred past the permission wait.

## 7. Crash / resume semantics

The gated `tool_use` is flushed to `session.jsonl` **before** dispatch (the
round-4 flush-before-dispatch rule, see SESSIONS.md), and no `pending_prompt`
is persisted for permission gates (same choice as `ask_user`). This section
covers two distinct interruptions and how each is recovered — a genuine
server-process restart (the "dangling security alert" mechanism below), and a
live client disconnect/reconnect that never kills the process at all (§7b).

### 7a. Dangling security alerts (cold-restart resume)

Because no `pending_prompt` is persisted, the naive dangling-`tool_use`
resume (SESSIONS.md "Resume") cannot tell "this call died while still
waiting on the permission gate — it never actually ran" apart from "this call
died mid-execution — it may already have written a file or run a command."
Confusing the two would be unsafe: blindly re-executing a call whose side
effects may already have landed could duplicate them.

`TransientStore.pending_security_alert` closes that gap: it holds the
`tool_call_id` of the one `run_command`-class call currently blocked inside
`GateOrchestrator.fire_permission`'s `await future` — set immediately before
the wait, cleared the instant it resolves (normally, or on any exception
*other than* cancellation), mirroring how `fire_approval` persists
`pending_prompt`. A cancelled wait — process death, or the worker task being
torn down for real — leaves it set, because that is exactly proof the call
never reached dispatch.

On cold-restart resume, `_resume_main_turn` (`kodo/runtime/_engine/_resume.py`)
claims this marker once, up front, and clears it unconditionally — this
resume pass is the one deciding that call's fate either way. If the
dangling `tool_use` it is currently handling matches the claimed id, it is
redispatched for real (the same treatment `_RESUME_REDISPATCH_TOOLS` gives
sub-agent spawns and `ask_user`): security judgement runs fresh — picking up
e.g. an "always allow" rule granted since — and, if still `ask`, the exact
same `prompt.permission` is re-fired to the user. Any other dangling call
(including a security-gated one whose marker doesn't match — it must have
died elsewhere, not at the gate) still gets the conservative
interrupted-tool stand-in, unchanged. A live, user-initiated Stop
(`_persist_interrupted_turn`) never redispatches anything — including a
gate-pending call — but does clear a stale marker so it cannot outlive the
call it pointed at.

### 7b. Live disconnect/reconnect (no process restart)

A VS Code window reload — or any transient socket drop where the singleton
server process itself keeps running — is a different event from a crash: the
session stays resident (`SessionManager` never rebuilds it), so the
dangling-`tool_use` resume path above never runs at all. Before this was
fixed, every server-initiated request/response future (`prompt.approval`,
`prompt.question`, `prompt.permission`, and the API-key broker) was owned by
the *socket* (`Connection`), which `ConnectionRegistry.run_ws`'s `finally`
unconditionally cancelled on every disconnect — including a reconnect a
moment later. The cancellation propagated, uncaught, straight through the
worker task, silently and permanently ending it: no more prompts, ever, for
that session, until the whole server process was restarted.

The fix moves this ownership to `SessionChannel` (session-scoped, survives
reconnects — `kodo/transport/_connection.py`) instead of `Connection`
(socket-scoped, dies on disconnect): a disconnect no longer cancels anything,
so the worker task simply stays parked at its `await future`, exactly as
intended. `SessionChannel.send()` additionally remembers every `kind=request`
envelope until its response arrives; `SessionManager.replay_backlog` (called
right after the reconnect base layer — `hello.ack`/`state`/`session.history`
— per `_app.py`'s `_handle_session_hello`) now also calls
`SessionChannel.replay_pending_requests()`, re-sending any still-unanswered
request with its original id to the newly attached connection. That is what
lets a *fresh* webview — one with no in-memory record of the prompt at all,
e.g. because the extension host itself restarted — re-render the panel and
still resolve the same waiting future. Only genuine session teardown (delete,
server shutdown) actually ends one of these waits now, via the owning
engine's worker task being cancelled — `KeyBroker.get_key` still catches that
`CancelledError` and returns a `connection_lost` error rather than
propagating it.

## 8. Costs and short-circuits

Judgement is pure in-process computation — parsing plus table matching, no
LLM round, no I/O — so it is effectively free in both latency and money, in
every posture. There is no verdict caching because there is nothing worth
caching: identical repeated calls deterministically re-produce the identical
verdict.

## 9. Recovered (malformed) tool calls

A local model can emit a tool call as **plain text** instead of a structured
tool call — the gpt-oss "harmony" wrong-channel slip (see
[LOCAL_INFERENCE.md](LOCAL_INFERENCE.md)). `LlamaPlugin` salvages this: when a
turn makes no structured tool call but its content channel is a JSON object
whose keys match exactly one available tool's schema, the plugin synthesises a
`ToolCallEvent(recovered=True)` instead of persisting the JSON as an answer.
The tool *name* was lost with the wrong channel, so it is inferred from the
argument shape — which the user must be given a chance to reject.

That confirmation is layered onto **this** security gate rather than a separate
mechanism:

- `ToolDispatcher.dispatch(..., recovered=True)` flows the flag into
  `__security_gate`. Outside autonomous mode a recovered call **forces**
  `fire_permission` regardless of the security verdict (`force_ask`), with a
  reason that explains the recovery; if the security layer *also* returned
  `ask`, its reason is appended so the user sees both. In autonomous mode the
  flag is ignored — the call runs exactly as any other allowed call would
  (`In autonomous mode, just run the tool`).
- The forced prompt sets `recovered: true` on the `prompt.permission` payload;
  `PermissionPanel.tsx` shows a distinct "the agent produced a malformed tool
  call, which Kōdo recovered" banner above the usual reason.
- The engine threads the flag from the stream to dispatch by tool_use id:
  `_run_agent_turn` collects `{tc.tool_use_id … if tc.recovered}` into a
  `recovered_ids` set and passes it to `_dispatch_tool_calls`, which dispatches
  each matching id with `recovered=True`. The crash-resume path never passes
  the set — a persisted call replays as an ordinary call.

If the salvaged JSON matches **zero or several** tools, it cannot be recovered
unambiguously: the plugin raises `MalformedToolCallError`, which the worker's
generic handler turns into a recoverable `error_notice` and resets the phase to
`awaiting_user` — the raw JSON is *not* shown as an answer and the model is
expected to simply retry.

## 10. Future work (deliberately out of scope)

Phase 2 user rules (§3.2a) are implemented. What's left:

- **Phase 3** — ask-rate telemetry, a standalone rules-management UI
  (list/revoke a session's or the machine's granted rules outside the flow
  of answering a live prompt — `security.add_rule`/`security.rules.list`/
  `security.rules.delete` stay reserved in WS_PROTOCOL.md §7.7 for this),
  deeper PowerShell/cmd tables, validator scenarios for the gate; and
  possibly an AST-based safe-subset analysis for inline `python -c` code,
  today's main deterministic-ask friction. Full design: SECURITY_RULES_PLAN.md
  Phase 3.
- **An unknown command's path-like *argument* (not subcommand) still stays
  outside the offer** (§3.2 rule 3, "Unknown" — `pytest ../other/`, a
  bespoke CLI's second-plus path argument) — the release valve for that
  class is growing the built-in allow table (Tier 3 tuning), not a user
  rule; a conservative, deliberate choice, not a gap to close casually
  (SECURITY_RULES_PLAN.md "Fixed decisions"). Plain redirection (`cat >
  out.txt`) no longer falls in this bucket — see §3.2b.
