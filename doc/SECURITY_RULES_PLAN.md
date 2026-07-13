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
3. **No path-like arguments** — no token with `/` or `\`, no `..`, `~`,
   absolute or drive-letter forms. (*Commands with paths are never
   rule-eligible* — `pytest` is eligible, `pytest ../other/` is not;
   err-safe, and build/test runners rarely prompt anyway thanks to the
   default table.)
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

## Fixed decisions

- **Pure heuristics** — the LLM judge is removed, not demoted to a fallback.
- **Build/test script runners always allow** (built-in Tier 2).
- **Complex commands and commands with path arguments are never
  rule-eligible** (session or global).
- **Destructive shapes are never rule-eligible**, even per-session.
- **Session rules survive crash-resume** (they live in `SessionState`).
- The `intent` field stays mandatory as permission-panel/feed metadata but is
  no longer a judgement input.
