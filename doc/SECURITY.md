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
allowed ends at the user — including every internal failure mode (judge
model unreachable, unparseable verdict): the gate **fails closed to a
prompt**, never open.

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
| `smart` | Below HIGH → allow (workspace-confined by construction). **HIGH → judged individually** (§3). CRITICAL → always ask. |

Current impact assignments (see `toolspecs/_*.py`): HIGH = `run_command`,
`filesystem`, `rollback`, `toolchain_deps`, `disable_autonomous_mode`;
MODERATE = `edit_file`; no tool is SEVERE or CRITICAL today. So in practice:
permissive allows everything, defensive asks on the six MODERATE+ tools, and
smart judges the HIGH ones.

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

### 3.1 `run_command`: static workspace analysis first

`kodo.security._analysis.analyze_command()` runs over the structural parse
from `kodo.shellparser` (the POSIX parser, or the PowerShell/Windows parser on
`os.name == "nt"`; see §5) and produces three facts:

1. **`outside_paths`** — argument or redirection-target tokens that resolve
   to a path **outside every workspace root**. Only tokens that *can* escape
   are resolved: absolute paths, `~`, Windows drive/UNC forms, and relatives
   containing `..` (joined lexically against the call's working directory).
   Plain relative tokens can't escape the resolver-confined cwd and are
   skipped — which also keeps subcommand words (`install`, `build`) from
   being misread as files. Executables are exempt (running `/usr/bin/python`
   is normal; the program is not a *target*). Device sinks
   (`/dev/null`, `NUL`, …) and fd-merges (`2>&1`) are exempt. `--flag=value`
   values are checked.
2. **`unresolved`** — substitution snippets (`$(...)`, backticks, `${VAR}`,
   `$VAR`/`$env:VAR`, `%VAR%`) that defeat static resolution. Substitutions
   are **masked before parsing** so `$(pwd)/y` stays one (skipped) token
   instead of shlex splitting off a bogus absolute `/y`.
3. **`read_only`** — every pipeline executable is on a conservative
   read-only allow-list *and* no redirection writes a file. The list is
   stricter than the checkpoint heuristic's (`find`, `sort`, `xargs`, `tee`
   are all excluded) because a wrong answer here skips a review, not just a
   no-op git sweep.

The verdict ladder for `run_command` in smart mode:

1. Any `outside_paths` → **ask immediately** (reason lists the paths; no LLM
   round). *A command that targets anything outside the workspace is always
   raised to the user.*
2. `read_only` and no `unresolved` → **allow immediately** (no LLM round).
3. Everything else → the **intent judge** (§3.2), with any `unresolved`
   snippets passed along as notes.

### 3.2 The LLM intent judge

Every HIGH-impact call not settled statically goes through one small,
single-shot LLM round (`kodo.security._judge`): first-degree mutators carry a
mandatory `intent` sentence (TOOLS.md §8A), and the judge's job is to check
that **the parameters do what the intent says and the intent is a plausible,
benign development step**.

- **Prompt**: a fixed ~150-word system prompt plus one compact user message —
  tool name, declared intent, parameters (per-value truncation at 400 chars,
  whole block capped at 2 000), workspace roots, analysis notes. No
  conversation history is included; the judge sees exactly one call.
- **Output contract**: exactly one JSON object,
  `{"verdict": "allow" | "ask", "reason": "<one sentence>"}`. The parser
  scans for the first decodable JSON object (tolerating prose/fences);
  anything that isn't a clean `allow` — including unreadable output — is an
  `ask`.
- **Model**: the session's active model. The engine injects
  `WorkflowEngine._security_judge` as the layer's judge callable: it
  resolves the entry-agent capability through the normal
  plugin/gateway/routing path (`_resolve_plugin`), streams silently (no feed
  events), and folds the call's USD cost into the session total as a
  cost-only `usage.update`.
- **Thinking tier**: if the active model is local and has a thinking family
  (`kodo.llms.local_thinking_tiers`), the judge round always pins
  `thinking_level="low"` (`LLMPlumbingMixin._judge_thinking_kwargs`) —
  independent of the session's own `thinking_level`, since intent
  classification doesn't need deep reasoning and the fixed low tier keeps
  the judge round fast regardless of what the user picked for their main
  conversation. `"low"` is a valid tier in both thinking families (Qwen's
  6-tier `--reasoning-budget` and GPT-OSS's 3-tier `reasoning_effort`).
  Cloud models and local models without a thinking family get no
  `thinking_level` at all, same as any other call.
- **Failure = ask.** A `None` judge, a raised exception, or an unparseable
  verdict each produce an `ask` with a matching reason.

HIGH tools without an `intent` field (`toolchain_deps`,
second-degree by design — see `toolspecs/_intent.py`) are judged on their
parameters alone; the prompt renders `(none declared)` for the intent.

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

- **Layering.** `kodo.security` imports only `kodo.toolspecs` (the catalog)
  and `kodo.shellparser` (the parse) — it sits beside `toolspecs` in the
  import graph and is consumed **only by `runtime`**. `kodo.tools` never
  imports it: the dispatcher sees the layer through the `SecurityLike` /
  `SecurityDecisionLike` structural protocols in `tools/_context.py`, exactly
  as `GateLike` decouples it from `runtime`. The engine constructs one
  `SecurityLayer` per `WorkflowEngine` and passes it into every dispatcher
  via `_make_dispatcher` — so the Guide, the Problem Solver, and **every
  sub-agent** flow through the same gate.
- **`SessionLike`** gained `command_control` (read live per call);
  `SessionState` already carried it.
- The **decision** (`SecurityDecision`) carries `action`, a one-sentence
  `reason`, and a `source` tag (`policy` / `threshold` / `workspace` /
  `static` / `judge`) that is logged for every call.

## 5. The shell parsers

`kodo.shellparser` stays **parse-only and judgement-free** (the checkpoint
heuristic and the security layer each apply their own classification over the
same structural view):

- `parse_command()` — the pre-existing POSIX/`shlex` tokenizer.
- `parse_powershell_command()` — **new**: a hand-rolled PowerShell/Windows
  tokenizer producing the same `ParsedCommand`/`Segment`/`Redirection`
  dataclasses. Understands `;` `|` `||` `&&` `&` separators (a lone `&` at
  segment start is the call operator and is dropped), single/double quoting
  with `''`/`""`/backtick escapes, backtick escaping outside quotes, and
  stream-qualified redirections (`2>`, `*>>`, `3>&1`, …; merge targets like
  `&1` are kept verbatim for callers to recognise). It never raises and
  covers `cmd.exe` syntax by overlap.

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

**`security.judging`** (server → client, `kind=event`; WS_PROTOCOL.md §5.9b.1):
brackets the SMART-mode intent judge's LLM round (§3.2) — `{"active": true}`
right before `WorkflowEngine._security_judge` calls the model, `{"active":
false}` in its `finally` once the verdict text (or an exception) comes back.
Only that judge round is slow; the static fast paths (§3.1) and the
threshold-only postures never touch the LLM, so they never fire this event.
Forwarded statelessly (like `session.naming`, no reconnect replay — the call
itself doesn't survive a restart) as `security_judging` to the webview, which
shows an "Evaluating Kōdo's action…" indicator (`SecurityJudgingIndicator` in
`indicators.tsx`) for the gap between the tool call appearing in the feed and
either the call proceeding silently (`allow`) or the permission panel above
appearing (`ask`).

**`agent.tool_call_prep` vs. `agent.tool_call_in_progress`** (WS_PROTOCOL.md
§5.5/§5.5a): the tool call's card appears on `agent.tool_call_prep`, sent
*before* `ToolDispatcher.__security_gate` runs — before it's known whether
the call will be judged, asked, or waved straight through. For `run_command`
this used to be a bug: the client stamped the card's `startedAt` (which drives
the "Waiting for tool output" timeout progress bar) at that same moment, so
the bar's clock ran through the SMART judge round and/or the `prompt.permission`
wait — both of which can outlast the command's own timeout, making an
in-flight, healthy command look like it had already timed out while
"Evaluating Kōdo's action…" was still showing.

The fix: `ToolDispatcher.dispatch` now calls
`EngineServices.notify_tool_call_in_progress(tool_use_id)` — which fires
`agent.tool_call_in_progress` — right after `__security_gate` returns (allowed
outright, *or* the user granted permission), immediately before the tool
handler runs, gated to `run_command` (the only tool the client animates a
timeout for). The client no longer stamps `startedAt` on
`agent.tool_call_prep`; it stays `null` (progress bar hidden) until
`agent.tool_call_in_progress` arrives for that `tool_call_id`, which is when
the bar actually starts. This holds for **every** posture: permissive/defensive
`ask` verdicts skip the judge round entirely and go straight to
`prompt.permission`, but they still funnel through the same
post-`__security_gate` choke point in `dispatch()`, so the bar is deferred
past that wait too — not just past SMART-mode judging.

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

Smart mode adds at most **one** short LLM round per HIGH-impact call, on the
session's model. The static fast paths exist to keep the common cases free:
outside-workspace asks and read-only allows never touch the LLM. Threshold
modes (permissive/defensive) never call the LLM at all. There is no verdict
caching: identical repeated calls are re-judged (cheap, and context — the
intent — should differ anyway).

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

- **Persistent rules** — `kodo/security/_rules.py`, `_store.py`,
  `_defaults.py` remain stubs for a "always allow commands like this"
  rule engine layered *ahead* of the per-call judgement, fed by a
  remember-this-decision affordance in the permission panel
  (`security.add_rule` stays reserved in WS_PROTOCOL.md §7.7).
- **Edit Control enforcement** — `edit_control` (`review_all`) pausing for
  sign-off on each edit is a review-workflow feature, not a security one,
  and is still state-tracking only.
- **Verdict caching / batching**, if judge latency on filesystem-heavy runs
  warrants it.
