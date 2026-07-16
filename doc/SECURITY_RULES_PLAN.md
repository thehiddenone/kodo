# Heuristic Security Rules — Implementation Plan

Replacing the SMART-mode **LLM intent judge** with a deterministic, heuristic
rule engine. Companion to [SECURITY.md](SECURITY.md) (which describes the
implemented layer); this document is the phased plan and the rationale for the
switch. As phases land, SECURITY.md is updated to describe the new behavior
and this plan records what was decided and why.

---

## 0. Motivation

The current SMART mode sends every HIGH-impact call that static analysis
cannot settle through a single-shot LLM round (`kodo.security._judge`) that
checks the agent's declared `intent` against the call parameters. Three
problems, in practice:

1. **Reliability is bounded by the session's model.** A 9B local model is not
   a dependable security authority; verdicts vary run to run.
2. **Latency.** Even pinned to the low thinking tier, each judged call costs
   an LLM round (~seconds on local models at 14–32 tok/s), and it serializes
   every judged call — agents spend a large share of wall-clock waiting on
   security approvals.
3. **It is an intent guard, not a security guard.** The judge answers "do the
   parameters match the declared intent?" — so it asks on harmless
   intent/parameter mismatches while having no principled model of *danger*.

The replacement: a structural rule engine over the existing
`kodo.shellparser` parse and `kodo.security._analysis` facts. Deterministic,
sub-millisecond, independent of whichever LLM the session runs.

**Threat model** (unchanged): accidental harm from a well-meaning but fallible
agent. An LLM actively evading the gate is out of scope — as it equally was
for the LLM judge.

**Philosophy change:** an unmatched command produces a *deterministic,
explainable* ask ("`foo` is not in the known-safe command set") that the same
command always reproduces — instead of the judge's per-run roulette. The
release valves for false-ask friction are the growing built-in table
(Phase 1/3) and user-defined "always allow" rules (Phase 2), which replace
the judge's adaptability.

---

## Phase 1 — Heuristic rule engine replaces the LLM judge (kodo repo)

**Goal:** SMART mode never calls an LLM. Every HIGH-impact call is settled by
structure + rules, defaulting to a deterministic ask.

### 1.1 Segment classification

Extend the analysis layer with a normalized per-segment view on top of the
existing `ParsedCommand`:

- **Transparent-wrapper unwrapping**: `env [VAR=x…]`, `nohup`, `time`,
  `nice`, `stdbuf`, `mise exec … --` peel off so classification sees the real
  command underneath.
- **`(executable, subcommand, flags, args)` extraction**: leaf-named
  executable, first non-flag argument as subcommand (`git push` →
  `("git", "push")`), remaining tokens bucketed into flags vs positionals.
- **PowerShell alias normalization**: `rm`/`del`/`ri` → `Remove-Item`,
  `iwr` → `Invoke-WebRequest`, …, so the Windows tables key on canonical
  cmdlet names.
- **Nested-shell recursion**: `bash -c "…"` / `sh -c` / `pwsh -Command` /
  `cmd /c` re-parse the inner string and classify recursively (depth-capped);
  `python -c` and friends are unparseable by definition → `unknown`.
- **Structural red flags** (Tier 0): pipe-to-shell (`… | sh`),
  decode-to-shell (`base64 -d | …`), substitutions feeding a *mutating*
  segment. (Outside-workspace path detection stays as it is today.)

### 1.2 Rule schema + evaluator (`kodo/security/_rules.py`)

```python
CommandRule(executable, subcommand=None, flags_any=(), verdict="allow"|"ask",
            category, reason, rule_eligible=False)
```

- Matching is on the normalized segment: executable (post-alias leaf name),
  optional subcommand, optional trigger flags (`git push` always asks;
  `--force` sharpens the reason).
- **Evaluation ladder, first match wins per segment**: Tier 0 structural red
  flags → Tier 1 ask-rules → Tier 2 allow-rules → default **ask**.
- A multi-segment command allows only if **every** segment allows (or is
  read-only); the first asking segment's reason wins.
- `category` lands in the ask reason and the permission panel: `deployment`,
  `destructive`, `system`, `network`, `privilege`, `obfuscation`, `unknown`,
  `benign-dev`.
- `rule_eligible` is the Phase 2 hook — eligibility is data, not code.

### 1.3 Built-in default ruleset (`kodo/security/_defaults.py`)

One table per dialect (POSIX / PowerShell-cmd). Highlights:

- **Tier 1 ask**: deployment (`git push`, `npm publish`, `docker push`,
  `kubectl`, `terraform`, cloud CLIs, `ssh`/`scp`/`rsync`), destructive
  (`rm -rf` broad targets, `git reset --hard`/`clean -fdx`, `dd`, `mkfs`),
  system (`sudo`/`su`, OS package managers, `npm -g`, `systemctl`,
  `crontab`, `reg add`), network-with-payload (`curl -X POST/--data/-T`,
  `nc`).
- **Tier 2 allow — build/test runners are unconditionally allowed**:
  `pytest`, `tox`, `hatch run`, `make`, `cmake`/`ctest`,
  `cargo build|test|check|fmt|clippy`, `npm|pnpm|yarn run|test|install`
  (non-`-g`), `go build|test`, `dotnet build|test`, `mvn`/`gradle`,
  `mise exec`-wrapped forms of all of the above. Plus safe VCS
  (`git status|diff|log|add|commit|checkout|branch|stash`),
  formatters/linters, in-workspace interpreters running project scripts.
- Because build/test is allowed by the *default* table, those commands never
  prompt at all — Phase 2 user rules are only ever needed for the long tail.

### 1.4 Layer rewiring (`kodo/security/_layer.py`)

- `run_command`: outside-paths → ask; read-only → allow; **rule engine →
  verdict** (replaces the judge).
- `filesystem`: allow all operations except `delete_dir` → ask.
- `rollback`: allow (the Guide's mandatory `ask_user` confirmation already
  covers it).
- `toolchain_deps`: allow plain registry `name`+`version`; ask when the name
  looks like a URL / git ref / local path / embedded index override.
- Delete `_judge.py`, `JudgeCallable`, `WorkflowEngine._security_judge`, and
  the judge-thinking-tier plumbing; retire the `security.judging` WS event
  and the `SecurityJudgingIndicator` in kodo-vsix (the gate is now instant —
  there is no gap to indicate). `SecurityDecision.source` gains `"rules"`.

### 1.5 Tests + calibration

- Table-driven unit tests per category × dialect (including
  wrapper-unwrapping, nested-shell, alias cases).
- **Corpus replay**: extract every historical `run_command` input from
  `~/.kodo-validation/runs` transcripts, run it through the new engine, and
  diff allow/ask against what actually happened — the ask-rate number that
  says the default table is ready.

### 1.6 Docs + memory

Rewrite SECURITY.md §3/§8 (drop §5.9b.1 from WS_PROTOCOL.md), update project
memory.

---

## Phase 2 — "Always allow" user rules (kodo + kodo-vsix)

**Status: implemented (2026-07-15).** doc/SECURITY.md §3.2a/§4/§6 describe
the shipped behavior; §2.4a below records where the implementation departed
from this section's original sketch and why.

**Goal:** the permission prompt can create a persistent rule — but only a
*generalized, path-free* one, and only when the command's shape makes
generalization safe.

### 2.1 What a user rule is

A user rule is exactly a **`(executable, subcommand)` shape** —
`("git", "push")`, `("docker", "build")` — nothing more. It never stores
arguments, paths, flags, or the literal command line. The user is never
offered "always allow `rm -rf build/`"; at most the shape would be offered,
and destructive shapes are excluded outright (§2.2).

### 2.2 Eligibility — computed server-side, per prompt

The layer attaches a `rule_offer` to the ask decision only when **all** hold:

1. Single segment — no pipes, `&&`, `;`, redirections. (*Complex commands
   are never rule-eligible.*)
2. No substitutions, no nested shells.
3. **Path-like arguments, tiered by whether the command is *known* or
   *unknown*** (§2.4a.7): a **known** command — one that matched an
   explicit, named `CommandRule` in the built-in table (`git push`, `apt
   install`, `npx`, …) — ignores path-like arguments entirely, since its
   offer already generalizes over everything past the subcommand and its
   danger is bounded by its category. An **unknown** command — the generic
   default-ask, e.g. a bespoke project CLI the engine has never seen — still
   excludes a path-like argument *after* the subcommand (`pytest
   ../other/` is not offered: the stored shape can't capture that path, and
   a different one would silently match the same rule), but *does* offer
   when the path-like token is the subcommand itself (`1brc
   ./measurements.txt` is offered as `("1brc", "./measurements.txt")` — the
   literal shape pins the rule to that exact file; a different file asks
   again). Err-safe either way, and build/test runners rarely prompt at all
   thanks to the default table.
4. The matched rule (or default-ask) is `rule_eligible`: **deployment,
   system, network, and unknown are eligible; destructive, privilege,
   obfuscation, Tier 0 structural, and outside-workspace asks never are.**
   `git push` can be permanently allowed; `sudo …`, `rm -rf …`, and
   `curl | sh` can only ever be allowed once.

No offer → the panel is exactly today's Allow / Deny.

### 2.3 The user's choice in the panel

When an offer is present, `PermissionPanel.tsx` shows the generalized shape —
not the concrete command — as the thing being granted:

```
⚠ Run Command — deployment
  git push origin main
  Reason: 'git push' publishes commits to a remote (deployment).

  [ Allow once ]  [ Deny ]
  [ Always allow `git push` — this session ]
  [ Always allow `git push` — all sessions ]
```

### 2.4 Storage and wire protocol

- **`kodo/security/_store.py`**: session rules live in `SessionState`
  (persisted, survive crash-resume); global rules in a JSON store beside the
  server's existing settings storage. Both are flat lists of
  `(executable, subcommand, created_at)`.
- **Wire**: `prompt.permission` gains
  `rule_offer: {executable, subcommand} | null`;
  `prompt.permission.response` gains `remember: "session" | "global" | null`.
  The reserved `security.add_rule` command becomes the internal effect of
  `remember` (and stays available for a future standalone rules UI).
- **Evaluation order**: user rules are checked *after* Tier 0 and the
  non-overridable Tier 1 categories, *before* overridable Tier 1 asks and the
  default-ask. A user rule can silence "unknown command" and
  deployment-class asks, but can never override destructive / privilege /
  structural findings — the same boundary as offer eligibility, enforced
  twice.

### 2.5 Tests, docs, memory

Store round-trip + resume tests; eligibility-matrix tests (each exclusion
rule); protocol tests; SECURITY.md gains a "user rules" section;
WS_PROTOCOL.md §6.5/§7.7 updated.

### 2.4a Implementation notes (post-launch, 2026-07-15)

Where the shipped code departs from §2.4's sketch, and why:

1. **No `security.add_rule` wire command.** §2.4 says the reserved command
   "becomes the internal effect of `remember`" — in the shipped design that
   effect is entirely server-side (`ToolDispatcher.__security_gate` →
   `EngineServices.add_security_rule` → `WorkflowEngine.add_security_rule`),
   never a *second* client→server request. `remember` on
   `prompt.permission.response` is the only wire surface Phase 2 needed;
   `security.add_rule` stays reserved for a possible future **standalone**
   rules UI (Phase 3) that grants a rule outside the flow of answering a
   live prompt, per §2.4's own parenthetical.
2. **No `created_at` on a rule.** Both stores hold plain `(executable,
   subcommand)` — the timestamp in §2.4's sketch was dropped: nothing reads
   it (there is no rules-listing UI yet), and adding it now purely for a
   hypothetical Phase 3 screen would be exactly the kind of premature
   abstraction this project avoids elsewhere. Cheap to add — a
   non-breaking schema change — when Phase 3 actually needs to show it.
3. **`kodo/security/_store.py` holds only the global store.** Session rules
   turned out to need no code in `kodo.security` at all: they're ordinary
   session state (`kodo.runtime.SessionState.security_rules`, persisted via
   `kodo.state.TransientStore.add_security_rule` — the same
   session/transient pairing `command_control` already uses, "survives
   crash-resume" for free) that the *caller* passes into
   `SecurityLayer.evaluate(session_rules=…)`. This keeps `kodo.security`
   from ever importing `kodo.runtime`/`kodo.state` (T2 must not reach up
   into T3+) — `_store.py` is purely the global (user-wide) side.
4. **"Global" means genuinely user-wide, not per-project.** §2.4's "beside
   the server's existing settings storage" is read literally:
   `~/.kodo/etc/security_rules.json`, next to `settings.json`
   (`kodo.project.WorkspaceLayout` — "one instance per machine, shared by
   every VS Code window's session"). This is *not* the same thing as the
   pre-existing, unused `ProjectLayout.security_json` stub
   (`<root>/.kodo/security.json`, docstringed "project-scoped security
   rules") — that stub predates this phase's actual design and was never
   wired up; it's a candidate for deletion in a future cleanup pass if a
   genuinely project-scoped (third) tier never gets built, but Phase 2 did
   not touch or remove it.
5. **Global store reads straight from disk, no cache.** `global_rules()` is
   checked at most once per HIGH-impact `run_command` judgement — cheap
   enough that a module-level cache wasn't worth the cross-session/test
   invalidation complexity it would add. Every concurrently open session in
   the process sees a newly granted global rule on its very next call, with
   nothing to invalidate.
6. **The offer computation's "no redirections" gate (§2.2 rule 1) turned out
   to matter more than expected in practice**: it means a very common
   friction case — `cat > file.ext << 'EOF' … EOF` writing a new file — is
   *never* offer-eligible (a redirection), even though the ask itself is
   `rule_eligible` (`category=unknown`). This is exactly the plan's own
   "Fixed decisions" intent ("complex commands and commands with path
   arguments are never rule-eligible"), just worth calling out because it's
   also exactly the case the heredoc-parsing bugfix (doc/SECURITY.md §5)
   was prompted by — the two fixes are complementary, not the same fix:
   heredoc extraction stops the ask from lying about *why* (no more
   fabricated "subcommand" from the C++/whatever snippet inside), Phase 2
   lets the user stop being asked *at all* for the shapes that are safe to
   generalize (`git push`, `npm publish`, `docker run`, …) — but a
   file-writing `cat`/`tee`/… still asks every time, once, on purpose.
7. **The blanket path-argument exclusion (§2.2 rule 3, original form) was
   too strict for single-argument bespoke CLIs (2026-07-15 fix).** A user
   ran a project-local tool, `1brc ./measurements.txt`, and got no offer at
   all — reported as "the permission prompt has no checkboxes." Root cause:
   the ask's *only* positional argument is inherently a path (there's no
   verb/subcommand structure the way `git`/`npm` have one), so it always
   tripped the path check, and — being genuinely unknown to the engine —
   would keep tripping it forever; there was no way to ever silence that
   class of ask. But the shape a rule stores is always literally
   `(executable, subcommand)`: for a command like this, the "subcommand" *is*
   the path, so offering it doesn't generalize to other files the way `git
   push` generalizes to other remotes — it only ever re-matches that exact
   invocation. Fixed by tiering rule 3 on `RuleDecision.known_command`
   (`kodo/security/_rules.py`, `_rule_offer()`): a command that matched an
   explicit built-in `CommandRule` skips the path check entirely (its
   category already bounds the risk, and its offer already ignores
   everything past the subcommand — same as before, just made explicit); a
   command that fell through to the generic default-ask only skips the
   check when the path-like token *is* the subcommand — a path-like
   argument anywhere else still disqualifies the offer, since the shape
   can't capture it and a different path would silently match. `git push`,
   `pytest ../other/`, and `cat > out.txt` are unaffected by this change.

---

## Phase 3 — Tuning, management, and hardening

1. **Ask-rate telemetry**: log every rules-engine ask (category +
   generalized shape, deduped) so real usage drives default-table growth;
   fold the replay script into a repeatable report.
2. **Rules management UI**: list/revoke session and global rules (vsix
   settings panel + `security.rules.list`/`security.rules.delete` WS
   commands) — trust requires being able to see and undo what was granted.
3. **Windows depth**: flesh out the PowerShell/cmd tables (cmdlet parameter
   matching such as `Remove-Item -Recurse`, `Invoke-Expression`,
   `Start-Process -Verb RunAs`) beyond the Phase 1 core.
4. **Validator coverage**: scenarios that exercise the gate end-to-end (a
   scripted deployment attempt must ask; a build command must pass
   silently).

---

## Phase 1 hardening (post-launch, 2026-07-14)

Three gaps found and closed while extending the engine to recognize
dual-mode commands, none captured by the original Phase 1 bullets above:

1. **Wrapper/read-only-fast-path bypass (critical).** `_analysis._is_read_only`
   checked the *raw* parsed executable, not the wrapper-peeled one — and
   `env` was listed both as a transparent wrapper (peeled for the per-segment
   rule table) and as unconditionally read-only for the fast path. Result:
   `env <anything>` short-circuited straight to allow without ever looking at
   the wrapped command (`env rm -rf <workspace file>` was silently allowed).
   Fixed by having the fast path consume the same normalized/peeled segments
   the per-segment rules already use.
2. **Dual-mode commands.** `sysctl`, `ulimit`, `date`, `hostname` each have a
   read form and a mutating form that a blanket allow-list or a
   `flags_any`-only `CommandRule` can't distinguish (the mutating form is
   often a *positional value*, not a flag: `sysctl vm.swappiness=10`,
   `ulimit -n 4096`, `date 010112002026`, `hostname newname`). Added a small
   `exe -> predicate` table (`_rules._DUAL_MODE`), matched the same way the
   pre-existing `xargs` structural check is — before the generic rule table,
   not through it. `uname` was reviewed and has no mutating form on any
   platform, so it needed no change.
3. **Bare subshell/brace grouping.** POSIX `(...)`/`{ ...; }` and PowerShell
   `(...)`/`{...}` (distinct from `$(...)`, which already recursed) weren't
   recognized by `kodo.shellparser` as structural — they parsed with a bogus
   executable literally named `"("`/`"{"`. This failed *safe* (always asked)
   but defeated the "auto-allow if every constituent is benign" goal for
   grouped commands, and gave dangerous grouped commands a generic "unknown
   command" reason instead of the real one. Fixed by having both parsers
   flatten bare grouping punctuation — deliberately bounded to the common
   "just wrap a command" forms, not full control-flow parsing (`if`/
   `foreach`/`while` script blocks still fail closed to ask, unchanged).

## OS temp directory carve-out (post-launch, 2026-07-14)

The `outside_paths` workspace-escape check (§1.4, ladder step 1) asked for
*any* path outside the workspace roots — including the OS temp directory
(`/tmp` on POSIX, `%TEMP%` on Windows), even though scratch work there is
ordinary, expected agent territory, not a workspace escape. Fixed by adding
`kodo.common.system_temp_roots()` (new T0 module, no intra-kodo
dependencies: `tempfile.gettempdir()` plus the literal `/tmp` on POSIX even
when `gettempdir()` resolves elsewhere, e.g. macOS's per-user `TMPDIR` vs.
`/tmp` → `/private/tmp`) and treating it as an implicit extra root in
`kodo.security._analysis._classify`. The carve-out only lifts the
*workspace-escape* ask — every other ladder step still applies to temp-dir
targets exactly as it does to workspace ones, so `rm -rf /tmp/x` still asks
(destructive category) while `cat /tmp/x` / `touch /tmp/x` allow.

The same fact gates a **second, independent gatekeeper** that isn't part of
`kodo.security` at all: `kodo.tools._paths.resolve_within` (the
`ProjectPathResolver` used in Guided mode for `create_file` / `edit_file` /
`filesystem` / `read_file`) previously raised `PermissionError` — not even an
`ask` — for any path outside the locked project root, before the security
layer was ever consulted. Without also loosening this resolver, file-tool
calls under `/tmp` would still hard-fail in Guided mode even after the
security layer stopped asking about them. `kodo.common` (T0) is the shared
home for `system_temp_roots()` specifically so `kodo.tools` and
`kodo.security` don't need to import each other to agree on it — preserving
the existing `kodo.tools` → `kodo.security` decoupling (§4). Problem Solver
mode's `LogicalPathResolver` already took absolute paths as-is, so it needed
no change.

## Fixed decisions

- **Pure heuristics** — the LLM judge is removed, not demoted to a fallback.
- **Build/test script runners always allow** (built-in Tier 2).
- **Complex commands (pipes/`&&`/`;`/redirections/substitutions/nested
  shells) are never rule-eligible** (session or global).
- **Path-like arguments are tiered by `known_command`** (§2.4a.7): a known
  command's offer ignores them (bounded category, already generalizes past
  the subcommand); an unknown command's offer excludes them unless the
  path-like token *is* the subcommand — that case is an exact-literal
  match, not a generalization, so it's safe.
- **Destructive shapes are never rule-eligible**, even per-session.
- **The OS temp directory is always reachable**, in every workflow mode and
  regardless of `command_control` posture nuance within the rule engine —
  the same way `/tmp` is ordinary scratch space on a real machine.
- **Session rules survive crash-resume** (they live in `SessionState`).
- The `intent` field stays mandatory as permission-panel/feed metadata but is
  no longer a judgement input.
