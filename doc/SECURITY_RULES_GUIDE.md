# Security Rules Guide — a use-case walkthrough of the command engine

This is a reference, not a design doc. It exists to answer one question:
**given this exact `run_command` line, what happens, and why?** — walked
through in the order `kodo.security._rules.evaluate_command()` actually
performs its checks, so it doubles as a reading guide to that function
itself.

It deliberately does **not** cover:

- **The wire protocol / permission-prompt rendering** — how a `run_command`
  ask becomes a `prompt.permission` event and a VS Code panel. See
  [SECURITY.md](SECURITY.md) §3–§6.
- **The storage layer's on-disk format and the phased rationale for every
  design decision** — why rules are split into a command-shape store and a
  path-shape store, what was tried and superseded, the full changelog. See
  [SECURITY_RULES_PLAN.md](SECURITY_RULES_PLAN.md).

Read those two for architecture and history. Read this one when you have a
command in hand and want to know its verdict.

---

## 1. Prerequisites — what the shell-parser layer guarantees

Everything below assumes three things are already true by the time
`evaluate_command()` sees a command. They're guaranteed by
`kodo.shellparser` (the structural parse) and `kodo.security._classify`
(the normalization pass) — worth understanding once, since almost every
per-segment rule below leans on one of them implicitly.

**1. Dialect selection is explicit, not sniffed from the command text.**
`parse_command()` (POSIX) or `parse_powershell_command()` (PowerShell/cmd)
is chosen by a `windows: bool` parameter (defaulting to `os.name == "nt"`),
never by looking at the command string itself. A `ParsedCommand` carries
`.segments` (one per pipeline stage) and `.operators` (the separators
between them — `|`, `||`, `&&`, `;`, verbatim). Each `Segment` carries its
own `.args` and `.redirections` (each a `{operator, target, heredoc_body}`).

**2. Substitutions are masked *before* tokenizing, not after.** A `$(...)`,
backtick, `${VAR}`, `$VAR`, `$env:VAR`, or `%VAR%` snippet is found by regex
and replaced with an opaque marker (`SUB_MARK`) before the command string is
handed to the shell tokenizer. This matters because it prevents `$(pwd)/y`
from being tokenized into a bogus bare `/y` (which would misfire the
outside-workspace check on a path that doesn't exist as written) — the
marked token is instead recognized as unresolvable and skipped entirely by
path classification (§2 below), while the two *command*-executing families
(`$(...)`, backticks) are separately collected into `command_subs` for
recursive judgement (§3).

**3. Heredoc bodies are extracted before tokenizing, not left inline.**
`<<[-]DELIM ... DELIM` blocks are stripped out of the command text and
attached to their owning redirection as `heredoc_body`, in real left-to-right
shell order, *before* `shlex` ever sees the rest of the line. Before this
existed (fixed 2026-07-15), a heredoc body's first non-comment line could be
misparsed as a fabricated "subcommand," and worse, `bash << EOF\n<anything>\nEOF`
(no `-c`, no script argument) could slip past the nested-command check
entirely — a real bypass, not just a cosmetic bug. Today: a **bare** shell
fed a heredoc (no other positional argument) has its body recursed as
`nested_command` (same trust boundary as `-c`); a bare interpreter
(`python`/`node`/`ruby`/`perl`) marks it `nested_opaque`; `bash script.sh <<
EOF` (a real script argument present) treats the heredoc as the script's own
stdin data, unchanged; any other receiving command (`cat`, `tee`, `mysql`, …)
just has the body silently dropped from the token stream — it was always
just data to that command.

**4. Normalization happens once, uniformly, before any rule runs.** Every
segment goes through `_classify._normalize`: transparent wrappers
(`env`, `nohup`, `timeout`, `mise exec … --`) are peeled so `env rm -rf x`
resolves to `rm`, not `env`; `python -m mod`/`python3 -m mod` is
re-classified as `mod` (`python -m pip install --user x` hits the `pip`
rules); PowerShell aliases resolve to their canonical cmdlet (`ls` →
`get-childitem`, `cd`/`chdir`/`sl` → `set-location`) — **only on the
Windows dialect**, so POSIX rules never need to know PowerShell spellings
exist. The result, `NormalizedSegment`, is what every rule below actually
matches on: `executable` (canonical, lowercase leaf name), `subcommand`
(first positional, lowercase), `flags`, `args` (every positional
*including* the subcommand, original casing), `has_substitution`,
`nested_command`/`nested_opaque`, `piped_input`, `writes_file`.

**5. A recursion depth limit (`_MAX_DEPTH = 3`) caps command substitutions
and nested shells.** Past it, `evaluate_command` returns a flat
`"nests other commands too deeply to analyze"` ask (`obfuscation`,
non-offerable). No legitimate development command nests this deep — this
exists purely as a backstop against pathological/adversarial input.

---

## 2. Step 1 — workspace escape, judged per segment

**The question:** does any argument or redirection target, once resolved,
point outside every workspace root *and* outside the OS temp directory?

**How resolution works** (`_analysis._resolve`): an **absolute** token is
always resolved (`posixpath.normpath`/`ntpath.normpath`, `~` expanded via
`os.path.expanduser`). A **relative** token is resolved *only if it contains
`..`* — a plain relative like `notes.txt` or `src/x.py` is left completely
alone, because it's assumed cwd-confined by construction (the tool's `cwd`
is itself already workspace-resolved before `run_command` ever runs) and
resolving it would just be wasted work. This is the load-bearing assumption
the whole outside-workspace mechanism rests on: **a plain relative argument
can never produce an outside-path finding**, which is exactly why the
read-only fast path (§4) needs its own explicit AND-gate rather than being
able to trust "no outside-path finding" as automatic for every read-only
command.

**Per-segment attribution, not whole-line.** Each segment's own arguments
and redirection targets are classified into *that segment's own* list
(`CommandAnalysis.segment_outside_paths[i]`, positionally aligned with
`.segments`) — not one shared list for the whole command. This matters for
a command that repeats the same outside path across two different segments
(`cat /etc/hosts && grep x /etc/hosts`): each occurrence must be
independently attributed, or the second segment's finding would be silently
lost to the first's having "already seen" that exact path string.

**Two outcomes per segment, once a finding exists:**

- **Eligible → offerable.** The segment's executable is on the read-only
  allow-list (`cat`, `ls`, `grep`, `head`, `pwd`, …) or is `cd`/`Set-Location`
  (`_CD_EXECUTABLES` — a separate bucket from "read-only," since `cd`
  doesn't read or output anything; it's non-destructive/session-scoped, not
  a reader), and the segment has no file-writing redirection anywhere in it.
  Then, unless the resolved path falls under a small sensitive-path
  denylist (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`,
  `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.config/gcloud`), the ask carries
  a `rule_offer` of `(executable, resolved_path)` — a shape unlike every
  other category's `(executable, subcommand)`: matching a *future* call
  means resolving *that* call's own argument (relative or absolute, against
  its own cwd) and comparing the resolved string, not literal equality.
  `cd ../kodo` and `cd /Users/dev/dev_root/kodo`, from the same cwd, resolve
  to the same string and hit the same granted rule.
- **Not eligible, or sensitive path → plain ask.** Same reason text, no
  checkbox — identical to how this category behaved before per-segment
  offers existed.

**One rule per distinct resolved path, not per segment or per command.** A
segment naming two outside paths (`cat /etc/hosts /etc/passwd`) produces two
independent offerable asks. Deduplication is keyed on `(executable,
resolved_path)` **across the whole command**, mirroring how the per-segment
danger-category dedup already works (§4/§7) — so a grant for `cat` never
silently covers `grep` on the same path, and `cat /etc/hosts && cat
/etc/hosts` collapses to one ask, not two.

**A segment with any outside-path finding unconditionally skips the normal
per-segment rule table** (§4) — it is never *also* judged for danger
category. This is load-bearing, not a nicety: `cd` already has an
unconditional built-in allow-rule (matches any argument) in the normal
table, so if a segment fell through to it once its outside-path findings
happened to be silenced, `cd /outside/granted && cd /outside/UNgranted` risk
conflating a granted path with an ungranted one sharing the same segment.

**Windows case-folding.** `cd`/`chdir`/`sl` all normalize to `set-location`
before this step ever runs (§1.4), so no dialect branching is needed in the
eligibility check itself. But Windows paths are case-insensitive, so the
*offer* itself (and the `known_path_rules` lookup) goes through the same
case+slash fold `_within_any_root` already uses for containment checks — a
rule granted for `C:\Outside` must still silence `c:\outside`. The
*displayed* reason text keeps the original resolved casing; only the
granted/matched shape is folded (a cosmetic tradeoff — a lowercase drive
letter in the permission panel — for reliability).

| Command | Outcome |
|---|---|
| `cat /etc/hosts` | **Ask, offerable** — `(cat, /etc/hosts)` |
| `cd /Users/dev/dev_root/kodo && git status --short` | **Ask, one offerable part** — `(cd, /Users/dev/dev_root/kodo)`; `git status` doesn't even appear (already known-safe, see §4) |
| `cat /etc/hosts /etc/passwd` | **Ask, two offerable parts** — one per path |
| `cat /etc/hosts && grep x /etc/hosts` | **Ask, two offerable parts** — same path, different executable, not collapsed |
| `cat ~/.ssh/id_rsa` | **Ask, no offer** — sensitive-path denylist |
| `rm -rf /outside/thing` | **Ask, no offer** — `rm` isn't in the eligible bucket |
| `cat /etc/hosts > /etc/hosts2` | **Ask, both paths no-offer** — `writes_file` disqualifies the whole segment |
| `cat notes.txt` (in-workspace relative) | *(never reaches this step — no finding at all)* |
| `cp secrets.txt /etc/passwd` | **Ask, no offer** — `cp` isn't eligible |

---

## 3. Step 2 — command substitutions

**The question:** does a `$(...)`/backtick snippet, recursively evaluated
as its own command, come back as anything other than `allow`?

Every `command_subs` entry (a subset of `unresolved` — the ones that
*execute* rather than just *expand a value*) is stripped of its `$(`/`)`
or backtick wrapper and recursively judged via `evaluate_command()` at
`_depth + 1`. If the inner verdict isn't `allow`, the **entire** outer
evaluation returns immediately with a reason of the form "Embedded command
substitution `snippet`: `inner reason`", built through the same `_ask()`
helper every other single-part ask uses — which means it is **always
exactly one AskPart, never offerable**, regardless of what the inner
recursive call computed.

This is deliberate and unconditional, unlike step 1's per-segment
treatment: a failing/dangerous nested command means the *whole line* is
suspect, not just the part that happened to substitute it in. Concretely,
even if the embedded command is itself something step 1 would have offered
standalone (`echo $(cat /etc/hosts)`), the outer ask never surfaces that
offer — the wrapping discards it. `known_rules`/`known_path_rules` are
still threaded into the recursive call, though, so a rule granted for the
inner shape *does* silence a wrapped occurrence exactly like a bare one —
only a *new* offer is suppressed, not the silencing of an *existing* one.

| Command | Outcome |
|---|---|
| `echo $(date)` | **Allow** — inner `date` (no args) is read-only |
| `echo $(rm -rf /)` | **Ask, no offer** — `"Embedded command substitution...: 'rm -rf' is destructive."` |
| `` echo `cat /etc/hosts` `` | **Ask, no offer** — inner ask wrapped, offer discarded even though `cat /etc/hosts` would be offerable standalone |

Value expansions (`$VAR`, `${VAR}`, `%VAR%`) are a *different* family —
they don't execute anything, they just defeat static path resolution. They
never trigger this step; they're handled per-segment inside §4/§5 instead
(read-only executables tolerate them, mutating ones don't).

---

## 4. Step 3 — the read-only fast path

**The question:** is every executable in the whole pipeline on the
conservative read-only allow-list, with no redirection writing a file
anywhere — **and** does no segment have an outside-workspace finding?

`_is_read_only` itself only ever checked the first half: every named
segment's executable in `_READONLY_EXECUTABLES` (`echo`, `cat`, `ls`, `grep`,
`pwd`, `head`, `tail`, `wc`, `diff`, `stat`, `du`, `df`, `tree`, `basename`,
`realpath`, `sleep`, …) or, on the Windows dialect, also
`_READONLY_CMDLETS` (`get-childitem`, `get-content`, `select-string`,
`test-path`, …), plus no segment's `writes_file`. It has **no notion of
paths at all** — deliberately narrower than the checkpoint heuristic's own
read-only list (`find` with `-delete`/`-exec`, `sort -o`, and anything else
with a write/exec flag are excluded here, since a wrong answer in *this*
list skips a user review entirely, not just a no-op git sweep).

That's why this step now ANDs in `not any(segment_outside_paths)`
(added alongside §2's per-segment split): without it, a lone `cat
/etc/hosts` — one segment, executable `cat` is read-only, nothing writes —
would silently auto-allow the instant §2 stopped being a whole-line
short-circuit. The AND changes nothing for the overwhelming common case
(`cat notes.txt`, `grep TODO src/`), since a plain in-workspace relative is
never even resolved (§2) — it only matters for the read-only-and-escaping
combination.

Value expansions (`$VAR`) *are* tolerated by this fast path (and by the
per-segment readonly check in §5) — an unresolvable value fed to a pure
reader cannot mutate anything, unlike a writing command where the same
unresolvable value could be the dangerous form.

| Command | Outcome |
|---|---|
| `cat notes.txt` | **Allow**, `source="static"` |
| `grep $PATTERN src/x.py` | **Allow** — substitution tolerated for a reader |
| `cat /etc/hosts` | **Not this step** — falls through to §2's per-segment path-ask |
| `cat /etc/hosts && echo safe` | **Not this step** (one segment has a finding) — falls through; `cat` asks (offerable), `echo safe` silently allows via §5's per-segment readonly check |
| `mv $SRC $DST` | **Not this step** (`mv` isn't read-only) — asks via §5, substitution defeats the allow-rule match |

---

## 5. Step 4 — the per-segment rule table

Reached only for a segment with **no** outside-workspace finding of its own
(§2 intercepted those already). Checked in this order, first hit wins:

**5a. Bare shell / interpreter as the executable** (`SHELL_EXECUTABLES` —
`sh`, `bash`, `zsh`, `cmd`, `powershell`, `pwsh`, …):
- `nested_opaque` (inline/encoded code — `-c`, `-e`, `-EncodedCommand`,
  `Invoke-Expression`'s argument) → **ask, `obfuscation`**, never offerable.
- `nested_command` set (a real `-c "…"` string, or a bare-shell heredoc body
  per §1.3) → recurse via `evaluate_command()` at `depth + 1`; a non-allow
  inner verdict wraps into a single non-offerable ask
  (`"Nested shell command: {inner reason}"`) — same all-or-nothing wrapping
  as §3's command substitutions, and for the same reason.
- `piped_input` (data piped *into* the shell, `curl … | sh`) → **ask,
  `obfuscation`**.
- A real positional script argument (`bash build.sh`, `sh script.sh << EOF`
  where the heredoc is the script's own stdin) → **allow**, same trust as
  `python x.py`.
- No args at all (a bare interactive shell) → **ask, `unknown`**, shape
  recorded but **never offerable** (`rule_eligible=False`) — unlike the
  generic "not in the known-safe command set" default-ask at 5g, which *is*
  `rule_eligible=True`. A bare shell is a distinct, more conservative ask,
  not an instance of the generic default.

**5b. Inline code on a non-shell executable** (`python -c`, `node -e`,
`perl -e`, PowerShell `-EncodedCommand`) → `nested_opaque` → **ask,
`obfuscation`**, never offerable. Prefer `python -m module` or a script
file — both are allowed.

**5c. `xargs`** → its child command's actual arguments come from stdin, not
the command line, so they're fundamentally unknowable statically. A
read-only child (`ls | xargs cat`) allows; anything else (`ls | xargs rm`)
asks, `unknown`, no special leniency.

**5d. Dual-mode commands** (`sysctl`, `ulimit`, `date`, `hostname` —
`_DUAL_MODE`) — benign when read-only, dangerous when mutating, in a way no
blanket allow-list or `flags_any` rule can express (a *positional value*,
not a flag, decides): `sysctl -w`/an assignment (`vm.swappiness=10`)
mutates a kernel parameter; a bare query or `-a`/`-n` reads. `ulimit` with a
numeric or `unlimited` value sets a limit; a bare query reads. `date -s`/a
bare positional sets the clock; a `+FORMAT` string reads. `hostname` with a
positional sets it; bare reads. **A substitution here always asks** — unlike
the general read-only leniency, an unresolvable value on a dual-mode command
could be exactly the mutating form, so no benefit of the doubt is given.

**5e. Read-only executables tolerate value expansions** (unlike everywhere
else): `exe in readonly and not segment.writes_file` → **allow**, even with
an unresolved `$VAR` — "an unknown value fed to a reader cannot mutate
anything." A writing redirection still disqualifies (the target file *is* a
mutation, resolved or not).

**5f. The ordered `CommandRule` table** (`_defaults.py`, one table per
dialect, specific rules before general, ask-rules and allow-rules
interleaved). Category taxonomy, with a representative example each:

| Category | Rule-eligible? | Example |
|---|---|---|
| `deployment` | Yes | `git push` (bare — publishes commits) |
| `destructive` | **Never** | `git push --force`, `git reset --hard`, `git clean` |
| `system` | Yes | `npm install -g`, `pip install --user`, `npx …` |
| `network` | Yes | `curl …`, `ssh …`, `docker login` |
| `privilege` | **Never** | `sudo`, `su`, `doas` |
| `obfuscation` | **Never** | `eval "…"` (POSIX), `Invoke-Expression` (PowerShell) |
| `unknown` | Yes (default-ask) | any executable not in the table at all |
| `benign-dev` | n/a (this is the *allow* category) | `npm run build`, `pytest`, safe `git` subcommands, `cd`/`export`/`set` (shell builtins), `ps`/`top`/`free` |

Specific-before-general ordering matters: `git push --force` (destructive)
is checked before the bare `git push` (deployment) rule, so a force-push
never accidentally matches the more permissive general rule.

An **allow-rule match is voided by an embedded substitution** — `mv $SRC
$DST` asks even though `mv` (with a resolved destination) would otherwise be
in-workspace-file-mutator-allowed, because the unresolved destination could
be anywhere.

**5g. Default: ask.** Nothing in the table matched — `"'{display}' is not
in the known-safe command set."`, `unknown`, `rule_eligible=True`. The one
recurring friction here is inline interpreter code (5b), which is opaque by
design and always asks regardless of how trivial the snippet is; agents
should prefer `-m`/a script file.

---

## 6. Rule-offer computation: two parallel functions, one per shape

`_rule_offer` (command-shape, §5's asks) and `_path_rule_offer` (path-shape,
§2's asks) both answer "may this specific ask become a permanent rule?", but
over different inputs and with different disqualifiers:

| | `_rule_offer` (command-shape) | `_path_rule_offer` (path-shape) |
|---|---|---|
| Precondition from the caller | `rule_eligible` category (deployment/system/network/unknown) | executable in read-only/`cd` bucket, no write in the segment |
| Control-structure keyword (`for`/`if`/`do`/`done`/…) | Disqualifies unconditionally, known or not | n/a — never in the read-only/`cd` bucket, so never eligible in the first place |
| Substitution in the segment | Disqualifies | *(segments with substitutions never reach here — they're either in §3's whole-line wrap or the general leniency of §5e)* |
| Nested/opaque shell | Disqualifies | *(same — never reaches here)* |
| Path-like **argument** | Known command: ignored. Unknown command: disqualifies *unless* it's the subcommand itself | n/a — the whole point of this shape *is* a path |
| Credential-shaped path | n/a | Disqualifies (`_sensitive_roots`) |
| Returned shape | `(executable, subcommand)` | `(executable, resolved_absolute_path)` |

The known-vs-unknown tiering in `_rule_offer` (§2.4a.7 in the plan doc) is
the subtler of the two: a **known** command — one that matched an explicit,
named `CommandRule` (`apt install`, `npx`) — has its offer ignore every
path-like argument, because the stored shape already generalizes past the
subcommand regardless (`apt install ./local.deb` is offered as `(apt,
install)`, same as `apt install anything-else`). An **unknown** command has
no such bounded category, so a path-like argument *after* the subcommand
still disqualifies (`pytest ../other/` isn't offered — a different path
would silently match the same rule) — but a path-like token that *is* the
subcommand is fine (`1brc ./measurements.txt` offers the exact literal
`(1brc, ./measurements.txt)`; a different file produces a different,
non-matching shape and still asks — it's an exact-literal grant, not a
generalization).

| Command | Offer |
|---|---|
| `git push origin main` | `(git, push)` — known, trailing arg ignored |
| `apt install ./local.deb` | `(apt, install)` — known, path arg ignored |
| `pytest ../other/` | `None` — unknown, path-like arg after subcommand |
| `1brc ./measurements.txt` | `(1brc, ./measurements.txt)` — unknown, but the path *is* the subcommand |
| `cat /etc/hosts` | `(cat, /etc/hosts)` — path-shape, eligible executable |
| `cat ~/.ssh/id_rsa` | `None` — path-shape, sensitive-path denylist |
| `for f in $(git ls-files); do echo "$f"; done` | Three asks (`for f`, `do echo`, `done`), all `None` — the parser splits the loop into pseudo-segments on `;`, but a control-structure keyword is never an invocable program a rule could generalize over |

---

## 7. Known-rules silencing — two lookups, two matching semantics

A granted rule turns a future matching ask into a silent `allow`, checked
inline in `evaluate_command`'s per-segment loop, before an ask is even
constructed:

- **Command-shape** (`known_rules: frozenset[(executable, subcommand)]`):
  `shape in known_rules` — **literal tuple membership**. The shape is a
  fixed, generalized pair computed once at judgement time
  (`(segment.executable, segment.subcommand)`); no resolution is ever
  involved because a subcommand is just a word, not a filesystem reference.
- **Path-shape** (`known_path_rules: frozenset[(executable,
  resolved_path)]`): also a literal tuple membership check *at the point of
  comparison* — but the value being compared is one `_analysis.analyze_command`
  already resolved to absolute form for *this specific call*, before the
  lookup ever happens. The "resolve first" step isn't a special step in the
  matching function; it's a byproduct of §2's static analysis always
  producing the resolved form for anything it flags as outside the
  workspace. This is why `cd ../kodo` and `cd /Users/dev/dev_root/kodo`
  (same cwd) hit the same granted rule: both resolve to the identical
  string *before* either is ever compared to `known_path_rules`.

Both are threaded through every recursive call (command substitutions,
nested shells) unchanged, so a rule silences a wrapped occurrence exactly
like a bare one — `bash -c "cat /etc/hosts"` with `("cat", "/etc/hosts")`
already granted allows, even though (per §3/§5a) a *new* offer could never
have surfaced from inside that same nested context.

---

## 8. Assembly and deduplication

Every segment that still asks (not silenced by §7, and — for command-shape
asks — not a segment already `allow`-matched via §4/§5) is collected into
`asks: list[RuleDecision]`, then:

- **Dedup is command-wide, not per-segment**, and uses **two independent
  keyspaces**: `seen_shapes: set[(executable, subcommand)]` for §5's asks,
  `seen_paths: set[(executable, resolved_path)]` for §2's asks. `git push
  && git push` collapses to one ask (same shape, twice); `cat /etc/hosts &&
  grep x /etc/hosts` does **not** collapse (different executables, same
  path) — a grant for one executable was never meant to imply trust for a
  different one on the same path.
- The final `RuleDecision.parts` is built as `tuple(a.parts[0] for a in
  asks)` — each `RuleDecision` appended to `asks` already carries exactly
  one `AskPart` (every construction path, whether §2's inline `_ask(...,
  kind="path")` or §5's `_ask(...)` via the rule table, produces a
  single-element `parts` tuple by construction). One subtlety worth
  flagging for anyone touching this code: when a command-shape ask's
  `rule_offer` is computed *after* the fact (§6, once `rule_eligible` and
  `shape` are known), the update must replace **both** the top-level
  `RuleDecision.rule_offer` field **and** `parts[0].rule_offer` together —
  updating only the top-level field and later reading `parts[0]` back out
  would silently lose the offer.
- The top-level singular fields (`action`, `reason`, `category`, `source`,
  `shape`, `rule_offer`, `known_command`) always mirror the **first**
  asking part — additive fields kept around so a zero-or-one-part decision
  (the overwhelming majority of real commands) reads exactly as if the
  per-segment split never existed. `reason` is the exception: for 2+ parts
  it's a `"; "`-joined summary of every part's reason, shown as the
  permission prompt's top-line summary above the per-part detail blocks.
- **Zero asks** (every segment allowed or was silenced) → a single
  whole-command `allow`, `source="rules"`, `"Every command in the pipeline
  is a known-safe development operation."`

---

## 9. Quick reference — the full ladder in one table

| Step | Question | On hit |
|---|---|---|
| 1 | Any segment's argument/redirection resolves outside every root + OS temp? | Ask per finding; offerable iff executable ∈ read-only/`cd` bucket, no write in segment, path not sensitive |
| 2 | Any `$(...)`/backtick recurses to non-allow? | Ask, whole line, never offerable |
| 3 | Every executable read-only, no writes, no step-1 finding anywhere? | Allow, `source="static"` |
| 4a–g | Per remaining segment: shell/inline-code/`xargs`/dual-mode/readonly-tolerance/rule-table/default | Allow or ask, per segment, offerable per §6 |
| — | Nothing left asking? | Allow, `source="rules"` |
