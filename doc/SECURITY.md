# The Security Layer

How Kōdo decides, for every tool call an agent makes, whether to **allow** the
call or **ask the user for permission** — and how that decision is wired from
the `kodo.security` package through the tool dispatcher and the wire protocol
into the VS Code extension's permission panel.

Related docs: [TOOLS.md](TOOLS.md) (tool catalog, `intent` contract, impact
levels), [WS_PROTOCOL.md](WS_PROTOCOL.md) (§6.5 `prompt.permission`, §7.4b
`command_control.set`), [INTERNALS.md](INTERNALS.md) (package tiers).

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

The separate **Edit Control** toggle (`edit_control`) remains state-tracking
only — it is a review-workflow control, not part of the security layer.

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
   paths). *A command that targets anything outside the workspace and the OS
   temp directory is always raised to the user.* A temp-dir target simply
   skips this step — it still runs the rest of the ladder, so e.g. `rm -rf
   /tmp/x` still asks (destructive, category rule) while `cat /tmp/x` or
   `touch /tmp/x` allow.
2. **Command substitutions** — each `$(...)`/backtick snippet is recursively
   evaluated as its own command (depth-capped at 3); a dangerous inner
   command asks. `echo $(date)` allows; `echo $(rm -rf /)` asks.
3. **Read-only fast path** — `read_only` → **allow**. Value expansions
   (`$VAR`) are tolerated here: an unknown value fed to a pure reader
   cannot mutate anything.
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

Each rule (and the default-ask) also carries the Phase 2 hooks:
`rule_eligible` (may a persistent user rule override this ask?) and the
generalized `(executable, subcommand)` `shape` a user rule would store —
destructive / privilege / obfuscation findings are never eligible. The
`intent` field remains mandatory on first-degree mutators (TOOLS.md §8A) as
permission-panel and feed metadata, but it is **no longer a judgement
input**.

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
  │        roots           = ctx.root_paths)
  │    "allow" → proceed
  │    "ask"   → ctx.gate.fire_permission(...)             kodo/runtime/_gates.py
  │              → prompt.permission (kind=request)        WS_PROTOCOL.md §6.5
  │              → user allows → proceed
  │              → user denies → {"error": "The user DENIED …"} (tool NOT run)
  └─ tool_cls(ctx).handle(input)
```

- **Layering.** `kodo.security` imports only `kodo.common` (the
  OS-temp-directory helper, see below), `kodo.toolspecs` (the catalog), and
  `kodo.shellparser` (the parse) — it sits beside `toolspecs` in the import
  graph and is consumed **only by `runtime`**. `kodo.tools` never imports it:
  the dispatcher sees the layer through the `SecurityLike` /
  `SecurityDecisionLike` structural protocols in `tools/_context.py`, exactly
  as `GateLike` decouples it from `runtime`. The engine constructs one
  `SecurityLayer` per `WorkflowEngine` and passes it into every dispatcher
  via `_make_dispatcher` — so the Guide, the Problem Solver, and **every
  sub-agent** flow through the same gate.
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
- **`SessionLike`** gained `command_control` (read live per call);
  `SessionState` already carried it.
- The **decision** (`SecurityDecision`) carries `action`, a one-sentence
  `reason`, and a `source` tag (`policy` / `threshold` / `workspace` /
  `static` / `rules`) that is logged for every call.

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
  "recovered": false }
```

`recovered` (default `false`) is `true` only when the prompt is for a
*salvaged malformed tool call* — see §9. The client renders an extra warning
banner above the reason when it is set.

`params` is the customer-visible preview: input properties projected through
the tool's `input_visibility` map (hidden properties never reach the prompt),
values truncated at 400 chars. Response (`kind=response`, correlated by id):

```json
{ "type": "prompt.permission.response", "action": "allow" | "deny", "feedback": "…" | null }
```

Malformed/unknown actions are treated as **deny** server-side.

**VSIX**: `session-controller.ts` caches the request as `pendingPermission`
(re-posted to the webview on rehydrate, like the approval gate) and forwards
it as a `permission_request` message; `PermissionPanel.tsx` renders in place
of the prompt input (the `ApprovalGate` pattern) with the tool name, a risk
badge, the declared intent, the layer's reason, the parameter rows, an
optional feedback textarea, and **Allow** / **Deny** buttons. The panel is
**transient** — never a session entry: the gated tool call's own card is
already in the feed and its result records the outcome (a denial is visible
as the tool's error result). Reconnects re-deliver an unanswered request via
the standard `Outbox` buffer-and-replay.

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
is persisted for permission gates (same choice as `ask_user`). If the server
dies while a prompt is open, resume finds a dangling non-spawn `tool_use` and
stubs it with an interrupted-tool result — the tool never ran, the agent sees
the interruption and may retry, which re-triggers the same judgement. The
gated tool is deliberately **not** in `_RESUME_REDISPATCH_TOOLS`: re-executing
is unsafe for calls that might have been mid-flight rather than mid-prompt.

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

- **Phase 2 user rules** — persistent "always allow `git push`"-style rules
  (generalized `(executable, subcommand)` shapes only; complex commands and
  commands with path arguments are never rule-eligible), created from the
  permission panel and stored per-session / globally (`kodo/security/
  _store.py` remains the stub; `security.add_rule` stays reserved in
  WS_PROTOCOL.md §7.7). Full design: SECURITY_RULES_PLAN.md Phase 2.
- **Phase 3** — ask-rate telemetry, rules-management UI, deeper
  PowerShell/cmd tables, validator scenarios for the gate; and possibly an
  AST-based safe-subset analysis for inline `python -c` code, today's main
  deterministic-ask friction.
- **Edit Control enforcement** — `edit_control` (`review_all`) pausing for
  sign-off on each edit is a review-workflow feature, not a security one,
  and is still state-tracking only.
