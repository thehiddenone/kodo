# Heuristic Security Rules — Implementation Plan

Replacing the SMART-mode **LLM intent judge** with a deterministic, heuristic
rule engine. Companion to [SECURITY.md](SECURITY.md) (which describes the
implemented layer); this document is the phased plan and the rationale for the
switch. As phases land, SECURITY.md is updated to describe the new behavior
and this plan records what was decided and why. For "given this exact
command, what happens and why" — a use-case reference rather than a decision
log — see [SECURITY_RULES_GUIDE.md](SECURITY_RULES_GUIDE.md).

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

The layer attaches a `rule_offer` to *each part* of the ask decision (§2.6)
only when **all** hold, judged per elementary command, not over the whole
line:

1. ~~Single segment — no pipes, `&&`, `;`, redirections~~ **(superseded by
   §2.6, 2026-07-16):** a compound command is split and each segment offered
   independently; a segment's own plain redirection no longer disqualifies
   it either — only `sudo`/`eval`/nested-shell segments stay unconditionally
   unofferable, and that's driven by rule 4 (category), not this rule.
2. No substitutions (`segment.has_substitution`, judged per segment — §2.6),
   no nested shells (`nested_command`/`nested_opaque`).
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
   obfuscation, and Tier 0 structural asks never are.** `git push` can be
   permanently allowed; `sudo …`, `rm -rf …`, and `curl | sh` can only ever
   be allowed once. **Outside-workspace asks are eligible only for a small
   curated bucket** (read-only executables + `cd`/`Set-Location`) and only
   via a *different* offer shape — a resolved absolute path, not a
   subcommand — see §2.7; every other outside-workspace ask (an unknown or
   destructive command, or an eligible command targeting a credential-shaped
   path) still never offers.
5. **The segment's executable is not a control-structure keyword**
   (`_rules._CONTROL_KEYWORDS` — POSIX `if`/`then`/`elif`/`else`/`fi`,
   `for`/`while`/`until`/`do`/`done`, `case`/`esac`/`in`/`select`/
   `function`/`time`/`coproc`; PowerShell `elseif`/`switch`/`foreach`/
   `try`/`catch`/`finally`/`trap`/`begin`/`process`/`end`/`param`; cmd.exe
   `goto`) — added 2026-07-18. `._parser`/`._powershell` split pipeline
   segments on `;`/`&`/`&&`/`||`/`|` with no grammar for compound
   statements, so a loop or conditional's own keywords surface as their own
   pseudo-segment (`for f in $(...); do ...; done` → asking segments `for
   f`, `do ...`, `done`). The line still asks exactly as before — this rule
   only strips the offer, since a reserved word is never an invocable
   program a rule could meaningfully generalize over.

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
  *(Superseded by §2.6: these became the plural, index-aligned
  `parts: [{reason, rule_offer}, ...]` / `remember: [scope, ...]` once a
  compound command could carry more than one offer.)*
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

   **Superseded by §2.6 (2026-07-16):** re-examined, plain redirection was
   never the actual risk — the heredoc-hardening fix above already routes
   the one genuinely dangerous case (a script piped into a shell/interpreter
   via `<`/`<<`) through `nested_command`/`nested_opaque`, which are not
   `rule_eligible` regardless of redirection. `cat > file.ext << 'EOF' …
   EOF` is offer-eligible as of §2.6.
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

### 2.6 Compound commands split per elementary command (2026-07-16)

Before this change, §2.2 rule 1 ("single segment — no pipes, `&&`, `;`,
redirections") meant a compound command's offer was all-or-nothing at the
*whole-command* level: the moment `evaluate_command()` hit the first
non-allow segment it returned immediately, and `_rule_offer()` refused to
offer *any* segment if the command had more than one (`analysis.operators`)
or contained a substitution anywhere on the line (`analysis.unresolved`).
`git status && ./deploy.sh staging` either fully allowed (both parts already
on the built-in allow/read-only list) or produced one undifferentiated
Allow/Deny with zero checkboxes the instant either part was unrecognized.

The engine now judges **every segment independently** and offers a rule per
elementary command that still needs attention:

- `RuleDecision` gains `parts: tuple[AskPart, ...]` — one `AskPart(reason,
  rule_offer)` per segment that asks and isn't already silenced by an
  existing session/global rule, deduplicated by `(executable, subcommand)`
  shape when the same elementary command repeats in the chain (`cmd && cmd`
  → one part, not two). The existing singular fields (`shape`,
  `rule_eligible`, `rule_offer`, `category`, …) are kept, unchanged, mirroring
  the *first* asking part — additive, not a breaking change, so every
  existing single-segment test and caller keeps working verbatim.
- `sudo`/`su`/`doas` (never `rule_eligible` — `privilege` category) and the
  new dedicated `eval` ask (POSIX, `obfuscation` category, added in this same
  change — previously `eval` fell through to the generic offer-eligible
  "unknown command" default) still appear as a part with their own `reason`,
  just with `rule_offer: null` — no checkbox, wherever they sit in the chain.
- §2.2 rule 1's whole-line gates on `analysis.operators`/`analysis.unresolved`
  are gone — a pipeline no longer blocks every offer in it, and a value
  expansion (`$VAR`/`%VAR%`) in one segment no longer blocks an unrelated
  segment's offer elsewhere in the same chain (`segment.has_substitution`
  already gated per-segment; it was simply redundant with the whole-line
  check, which is what got removed).
- §2.2 rule 1's `segment.has_redirections` check is also gone — re-examined,
  the original worry (a script piped into a shell/interpreter via `<`/`<<`)
  is fully covered by the `nested_command`/`nested_opaque` checks in
  `_judge_segment`, which were never offer-eligible regardless of
  redirection. A plain, workspace-confined redirection (`cat file.txt >
  out.txt`) has nothing left to disqualify — the outside-workspace check
  (§1) still runs on every future invocation regardless of any granted rule.
  This resolves the exact friction §2.4a.6 called out as permanently
  un-offerable (`cat > file.ext << 'EOF' … EOF`).
- Command substitutions (`$(...)`/backticks) are unaffected: they still
  short-circuit the whole evaluation with a single, non-split, non-offerable
  ask before the segment loop ever runs (§1 step 2) — a failing nested
  command means the *whole* line is suspect, not just one part of it.

Wire protocol (`prompt.permission`/`.response`, WS_PROTOCOL.md §6.5): the
singular `rule_offer`/`remember` fields become the plural, index-aligned
`parts: [{reason, rule_offer}, ...]` and `remember: [scope, ...]`.
`kodo.tools._dispatch.__security_gate` now calls `add_security_rule` once
per `(part, scope)` pair where `part.rule_offer is not None and scope in
("session", "global")`, instead of once for the single offer.
`kodo.state.TransientStore.add_security_rule`/`WorkflowEngine.add_security_rule`
are unchanged — they already grant one shape at a time.

### 2.7 Workspace-escape path offers for read-only/`cd` commands (2026-07-17)

Before this change, §2.2 rule 4 made the outside-workspace category
categorically non-offerable — `cd /some/sibling/repo && git status`
(discussed live: `cd /Users/dev/dev_root/kodo && git status --short`, run
from a `kodo-vsix`-only workspace) got one plain, whole-line Allow/Deny with
no checkbox, forever, no matter how benign `cd`/`git status` are
individually. The escape check (§1 step 1, `analysis.outside_paths`) was a
**whole-command** short-circuit: the instant *any* argument/redirection
anywhere in the line resolved outside every workspace root, `evaluate_command()`
returned immediately — before the per-segment loop (§2.6) ever ran, so the
per-segment split never got a chance to help here.

**Scope, confirmed via `AskUserQuestion`:** eligible executables are the
existing read-only allow-list (`_READONLY_EXECUTABLES`/`_READONLY_CMDLETS` —
`cat`, `ls`, `grep`, `head`, …) plus `cd`/`Set-Location` (`_CD_EXECUTABLES`,
a separate bucket from the read-only list since `cd` doesn't read or output
anything — it's non-destructive/session-scoped, not "read-only" in the same
sense); one rule per **distinct resolved path**, not per command (`cat
/etc/hosts /etc/passwd` offers two independent checkboxes); integrated with
the §2.6 per-segment split (`cd /outside/path && git status` offers exactly
one part — `git status` doesn't even appear, since it was already silently
allowed); a small hardcoded sensitive-path denylist (`~/.ssh`, `~/.aws`,
`~/.gnupg`, `~/.kube`, `~/.docker`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`,
`~/.config/gcloud`) is never offer-eligible even for an otherwise-eligible
command — the ask still happens, just with no checkbox for that path.

**The offer shape is different from every other category**: `(executable,
resolved_absolute_path)`, not `(executable, subcommand)`. Matching a future
call means *resolving that call's own argument first* (relative or
absolute, against its own cwd) and comparing the resolved form — not literal
string equality. In practice this needs no new resolution step: the static
analysis (`kodo.security._analysis.analyze_command`/`_classify`/`_resolve`)
already computes the fully-resolved absolute form of every token it flags as
"outside" (a plain relative token without `..` is never even resolved at
all — by design, it's cwd-confined and can't escape — so it never produces
an outside-path finding to match against in the first place). `cd ../kodo`
and `cd /Users/dev/dev_root/kodo`, evaluated from the same cwd, resolve to
the identical string and hit the same granted rule.

**Read-only fast path stays safe.** §1 step 3's fast path
(`analysis.read_only`) only ever checked executables + `writes_file` — it
has no notion of paths, so its safety always depended entirely on the
outside-workspace check running first, unconditionally, over the whole
command. Splitting that check per-segment means the fast path must now
**also** require no segment has an outside-path finding
(`analysis.read_only and not any(analysis.segment_outside_paths)`), or a
lone `cat /etc/hosts` — every executable in the "line" is `cat`, read-only,
no write — would silently auto-allow. `cat file.txt` (a plain in-workspace
relative) is unaffected: it's never even resolved, so the AND changes
nothing for the overwhelming common case.

**A segment with any outside-path finding unconditionally skips the normal
per-segment rule table** (`_judge_segment`), regardless of how many of its
paths are already covered by a granted rule. This is load-bearing, not a
performance nicety: `cd` already has an unconditional built-in allow-rule
(§1.3, matches any argument) — if a segment fell through to that table the
instant its own outside-path findings happened to be fully silenced, `cd
/outside/granted-path && cd /outside/UNgranted-path` risk being conflated
(there's only one segment here, but the general shape generalizes: a
write-plus-read segment like `cat /etc/hosts > /etc/hosts2` where only the
read side is granted must not spuriously re-ask via the rule table once the
read is silenced).

**Segment-wide, not argument-wide, eligibility.** `writes_file` disqualifies
*every* path offer in a segment, including an unrelated read target (`cat
/etc/hosts > /etc/hosts2` — the read of `/etc/hosts` doesn't get offered
either). Mirrors `_rule_offer`'s own segment-wide granularity for
`has_substitution`/nested-shell — deliberate, not an oversight.

**Nested/substituted contexts stay non-offerable, for free.** Command
substitutions and nested shells already wrap a recursive `evaluate_command()`
call's failure into a single, generic, non-offerable ask (`Embedded command
substitution …`/`Nested shell command: …`), discarding the inner call's
`.parts`/`.rule_offer` unconditionally — this was already true before §2.7
and needed no change: an eligible command wrapped in `bash -c "cat
/etc/hosts"` still never surfaces its own offer, only the outer wrapping
does (though a `known_path_rules` grant still silences the wrapped
occurrence, exactly like a bare one).

**Storage is a fully separate store, not a schema change to the existing
one** — `~/.kodo/etc/security_path_rules.json` (global,
`add_global_path_rule`/`global_path_rules`) and a parallel
`TransientStore.security_path_rules`/`add_security_path_rule` (session),
mirroring `_store.py`'s/`_transient.py`'s existing command-shape machinery
almost line for line. Deliberately not folded into the existing
`security_rules.json`/`transient.json` schema: both hard-assume a flat
2-element `[executable, subcommand]` list with **no "kind" discriminator**,
and extending that live, already-shipped schema would need a migration path
the separate-file approach avoids entirely. (The two rule *kinds* were
already not fully unambiguous even within the existing single schema before
this change — an unknown-command exact-literal offer, §2.4a.7's `1brc
./measurements.txt` example, can itself store a path-shaped "subcommand" —
so keeping them in genuinely separate stores removes rather than introduces
ambiguity.)

**A `kind` field, Python-internal only, routes the grant — never on the
wire.** `AskPart` gained `kind: "command" | "path"` (default `"command"`,
so every pre-existing `AskPart` construction site needed no change).
`kodo.tools._dispatch.__security_gate`'s grant loop branches on
`part.kind` to call `add_security_rule` or `add_security_path_rule`. The
wire protocol needs **no change at all** for this: `fire_permission`
(`runtime._gates`) already serializes only `part.reason`/`part.rule_offer`
into the outbound `{reason, rule_offer}` — `kind` never crosses the socket —
and the server always grants from its **own** in-memory `AskPart` objects on
`allow`, never anything reconstructed from the client's response (the
existing "server never trusts a client-declared shape" invariant, §3.2a/§4,
extends unchanged: only now there are two kinds of shape the server might
have offered, and it always remembers which is which itself). This also
means **kodo-vsix needed zero changes** — confirmed by reading
`PermissionPanel.tsx`/`types.ts`/`session-controller.ts` in full:
`rule_offer: {executable, subcommand}` is rendered as fully opaque strings
(`${executable} ${subcommand}`.trim()) at every hop, so a resolved absolute
path in the `subcommand` slot renders correctly with no code change — it
just shows as `Always allow cat /etc/hosts`-style text.

**Windows**: `cd`/`chdir`/`sl` all normalize to the single canonical
`set-location` during `_classify._normalize`'s alias-resolution pass, before
any rule ever sees the segment — `_CD_EXECUTABLES = {"cd", "set-location"}`
needs no dialect branching. Windows paths are case-insensitive, so the
offer/dedup/`known_path_rules` key goes through the same case+slash fold
`_within_any_root` already uses for its own comparisons
(`_normalize_path_key`) — the *displayed* reason text keeps the original
resolved casing; only the granted shape itself is folded (a small,
deliberate cosmetic tradeoff — a lowercase drive letter in the permission
panel — in exchange for a rule granted as `C:\Outside` reliably silencing a
later `c:\outside`).

### 2.8 Radio-button choice + a durable "rule added" record (2026-07-17, later)

Two client-facing follow-ups, confirmed via `AskUserQuestion` before either was built:

**The session/global choice became a grouped, mutually-exclusive radio pair
instead of two independent checkboxes.** The two options were always
mutually exclusive in effect (picking one clears the other — see §2.3's
`toggleRemember`), but checkboxes read as independently toggleable, which is
misleading. `PermissionPanel.tsx`'s `RuleOfferCheckboxes` became
`RuleOfferChoice`: still the same bordered `permissionRuleOffer` box (the
existing border already delineates the group's bounds — no new styling
needed there), but `<input type="radio" name="{requestId}-rule-offer-{i}">`
per part so multiple parts' pairs in a compound-command prompt never
cross-group. Click-to-deselect (return to "no rule remembered") is preserved
across the type change by driving the toggle off `onClick` rather than
`onChange` — a native radio's `onChange` does not fire when you click an
already-checked option, but `onClick` always does, so re-clicking the
selected option still clears it exactly as the checkbox did. The component
also takes a `scopes` list and falls back to a single plain checkbox when it
has fewer than two entries — confirmed defensive-only: nothing in the wire
protocol today ever restricts a `rule_offer` to a single scope, so that
branch is currently unreachable, kept only so a future scope-restricted
offer (none planned) degrades sensibly instead of rendering a meaningless
one-option radio group. WS_PROTOCOL.md §6.5 updated to describe the radio
pair instead of checkboxes.

**A new `security.rule_added` event + `security_rule_added` session-entry
type is the user's own durable record of what they just granted** —
distinct from the gated tool call's own card, and explicitly excluded from
LLM context (`exclude_from_context: true`, same bucket as `thinking_block`/
`interrupted`/`error_notice`). Fired from `WorkflowEngine.add_security_rule`/
`add_security_path_rule` (`_core.py`) right after the rule is actually
persisted — one event per granted part, in grant order — via a new
`EngineEmitters.emit_security_rule_added(scope, executable, subcommand)`
that mirrors `emit_error`'s shape exactly: persist a `security_rule_added`
marker (`TransientStore.append_marker`) *and* push the live event, so
`HistoryProjector.history_entries` replays the same record after a reload
(new `elif kind == "security_rule_added"` branch, `_history.py`). Wire
payload is `{scope, executable, subcommand}` — the exact granted shape,
reusing the same "resolved absolute path in `subcommand`" convention as
`rule_offer` on `prompt.permission` for a path rule, so the client needs no
`kind` field and no branching to render either kind (see WS_PROTOCOL.md
§5.9d).

kodo-vsix: `SessionEntry`/`Action` gained a `security_rule_added` variant
(`types.ts`); `session-controller.ts` bridges the `security.rule_added` WS
event; `reducer.ts` handles both the live action (plain append, mirroring
how `tool_call` appends — no streaming state to fold in, since this always
fires mid-turn after any toolgen/token streaming for the call has already
committed) and the `session_history` replay branch. `SessionEntryView.tsx`
renders it as a new, deliberately plain style (`styles.securityRuleAdded`) —
an ℹ️ icon paired with ordinary `--vscode-foreground` text, *not* one of the
colored `<kodo_info>`/`<kodo_warn>`/`<kodo_crit>`/`<kodo>` Markdown callouts,
since this is neither a warning nor a success, just a factual record: "Added
an always-allow rule for `<executable> <subcommand>` — this session." /
"— all sessions."

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

## Rules management UI — global scope (post-launch, 2026-07-17)

Phase 3 item 2, global half only — session-scope listing/revoking is still
future work, left for a session-webview surface.

kodo: `kodo.security._store` gained `remove_global_rule`/
`remove_global_path_rule` (mirrors `add_global_rule`/`add_global_path_rule`;
no-op, not an error, if the rule is already absent). A new T4 facade,
`kodo.runtime._security_rules` (`list_global_security_rules`/
`delete_global_security_rules`, re-exported from `kodo.runtime`), is what
`server/_app.py` actually calls — `kodo.security` is consumed only by
`runtime` (doc/INTERNALS.md §2.2), so `server` was not given a direct
`kodo.security` import for this. New control-connection-only WS commands
`security.rules.list` / `security.rules.delete` (doc/WS_PROTOCOL.md §7.6c)
merge both on-disk stores — command-shape (`security_rules.json`) and
path-shape (`security_path_rules.json`) — into one `{kind, executable,
value}` list; `delete` takes a batch and replies with the post-deletion set
so the panel refreshes from the response alone.

kodo-vsix: new **Kōdo Settings** webview panel (`kodo-settings-panel.ts`,
singleton `createOrShow` like the existing Cloud AI / Local Inference
settings panels) with a left nav + right content layout — for now a single
"Global Allow-Rules" section: a checkbox per rule, "Select All" / "Clear
Selection" / "Delete Selected" (enabled only once ≥1 row is checked) /
"Close". Opened from a new gear (`$(gear)`) icon on the Kōdo sidebar view's
title bar — `kodo.openSettings`, replacing `kodo.openPanel` in that slot
(the "open a session tab" command still exists, just no longer has a
sidebar icon of its own; reachable via the Command Palette).

- **Pure heuristics** — the LLM judge is removed, not demoted to a fallback.
- **Build/test script runners always allow** (built-in Tier 2).
- **A compound command (pipes/`&&`/`;`) is split and judged per elementary
  command (§2.6)** — each segment is independently offer-eligible, never
  all-or-nothing at the whole-line level. Within one segment, a substitution
  or nested/opaque shell command still makes *that segment* never
  rule-eligible (session or global); a plain redirection no longer does.
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
- **A workspace-escape ask is offer-eligible only for a curated read-only/
  `cd` bucket, and only via a separate path-shaped rule** (§2.7) — every
  other outside-workspace ask (an unknown/destructive command, or an
  eligible command targeting a credential-shaped path) stays permanently
  un-offerable, matching this file's own earlier, now-superseded framing of
  "outside-workspace asks never are [rule-eligible]" for that remaining set.
- The `intent` field stays mandatory as permission-panel/feed metadata but is
  no longer a judgement input.
