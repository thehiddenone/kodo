# Kodo — Stuck-Agent Detection & Remediation

> Reference: [STATE_AND_LIFECYCLE.md](STATE_AND_LIFECYCLE.md) (turn/session lifecycle), [SETTINGS.md](SETTINGS.md) §2.6 (`stuck_detection`), [WS_PROTOCOL.md](WS_PROTOCOL.md) §5.9e/§6.8, [SECURITY.md](SECURITY.md) (sibling `prompt.*` gate precedent), [TOOLS.md](TOOLS.md) §5A (`return_result`/`run_subagent` contract; §2.8 below is its companion hardening).

## 1. The failure this addresses

Local LLMs occasionally end a turn without actually finishing the task — most visibly, a call that produces **no tool call and no visible text**. The engine already had a sentinel for this (`"(no text)"`, `kodo/runtime/_engine/_turns.py`'s `_run_agent_turn`), but treated it as an ordinary turn end: the entry-agent turn just goes idle (`session.phase == "awaiting_user"`) with the task unfinished and no explanation, and a sub-agent turn hands its parent a near-empty `return_result` fallback.

A concrete example that motivated this feature: session `1784394478` (Problem Solver, local model `unsloth-gemma4-26b-ud-q8-k-xl`, One Billion Row Challenge task) — mid-investigation ("Let me check `build.sh` content...") the model's final call returned `stop_reason: "end_turn"`, zero tool calls, and empty text. The session went idle with the task nowhere near done, and nothing told the user.

A second, related failure mode motivated §2.7: a local model's *thinking block* degenerating into a repetition loop — the same few lines generated verbatim, over and over, until the thinking-token budget is exhausted. This is a documented Gemma failure (worse under grammar/structured-output constraints, which can block the token that would otherwise end the loop) rather than a one-off. Everything above catches a stall only *after* a round finishes streaming with no tool call — by the time that could see a runaway thinking block, the whole budget is already spent, which is why this second failure mode needs its own, mid-stream detector.

A third failure mode motivated §2.11, and it is the one that shows the shape of the blind spot rather than just another instance of it: a model that never stalls at all, because it calls a tool on **every** round — the same tool, the same arguments, the same result, ~1133 rounds in a row (session `1788543589`). Both detectors above, and both in §2.9/§2.10, are anchored on a round that either produced no tool call or streamed anomalous content *within* one round. A perfectly well-formed call, repeated forever, is neither. Worse, the turn loop read each repetition as evidence of progress and reset every other detector's streak on it — so the loop was not merely unobserved, it actively suppressed observation. §2.11 covers it and makes "progress" conditional on the round having actually produced something new.

## 2. Architecture

Three concerns, deliberately kept independent so each can evolve on its own:

1. **Detection** — a small, explicit registry of `TurnSignal -> RedFlag | None` functions (`kodo/runtime/_engine/_watchdog.py`).
2. **Policy** — the `stuck_detection` settings block (`kodo/server/_config.py`, [SETTINGS.md](SETTINGS.md) §2.6) decides *whether* a matched red flag triggers remediation, and whether remediation is immediate or gated behind a user prompt.
3. **Remediation** — inject a fixed continuation nudge and either retry inline or hand off to the worker queue, depending on scope.

### 2.1 Detection — `TurnSignal` → `RedFlag`

```python
@dataclass(frozen=True)
class TurnSignal:
    text: str            # the turn's visible text ("" if none)
    thinking_text: str    # the turn's thinking block, if any
    stop_reason: str      # "end_turn", "max_tokens", ...

@dataclass(frozen=True)
class RedFlag:
    code: str   # machine-readable id, persisted in the nudge's `detail`
    hint: str   # one user-facing sentence, never sent to the LLM
```

`TurnSignal`/`StallDecision` live in `kodo/runtime/_engine/_shared.py`, not `_watchdog.py` — `_proto.py` (the `EngineHost` protocol every mixin, including `WatchdogMixin`, types `self` against) needs to reference them in method signatures, and `_proto.py` cannot import a mixin module without risking a cycle (`_watchdog.py` itself imports `EngineHost` from `_proto.py`). `_shared.py` has no such constraint, so the shapes live there and both `_watchdog.py` and `_proto.py` import from it.

Three detectors ship today, all drawn directly from real failure modes:

| `code` | Fires when | Evidence |
|---|---|---|
| `empty_final_turn` | No tool call **and** no visible text | The motivating session above — a legitimate completion always says *something*; an empty final turn is never a real "I'm done". |
| `truncated_generation` | `stop_reason == "max_tokens"` | llama.cpp's `"length"` finish reason, remapped in `kodo/llms/llamacpp/_llama.py`'s `_map_finish_reason` — the model was cut off mid-generation by its output-token cap, possibly mid-sentence or mid-plan. |
| `terse_final_response` | The visible text, with punctuation stripped, reduces to zero or one word | A reply like `"Done."` or `"Yes."` carries no real content — it reads the same as an empty turn even though `text.strip()` is non-empty. Two words (`"Sounds good."`, `"All set."`) are accepted as a real, if brief, completion. |

`detect_red_flags(signal)` runs every registered detector and returns every match (never short-circuits). **To add a new red flag**: write one more `TurnSignal -> RedFlag | None` function and append it to `_DETECTORS` in `_watchdog.py` — no other wiring required. Nothing about the settings, the gate, or the turn loop needs to change.

### 2.2 Policy — `stuck_detection` settings

Three independent knobs (`kodo/server/_config.py`'s `_DEFAULT_USER_SETTINGS["stuck_detection"]`; see [SETTINGS.md](SETTINGS.md) §2.6 for the full reference):

- **`active`**: `"off" | "local_only" | "local_and_cloud"` (default `"local_only"`) — this is primarily a local-model failure mode; cloud models (Claude) essentially never exhibit it, so the default only watches local turns.
- **`scope`**: `"top_level" | "top_level_and_subagents"` (default `"top_level_and_subagents"`) — whether only the shared entry-agent turn (Guide/Problem Solver) is watched, or sub-agent turns (spawned through a `run_subagent_<name>` tool) too. Defaults to covering sub-agents: their tool calls (`return_result` in particular) stream through the same mid-stream cyclic-argument detector (§2.10) as the entry agent's, and this scope excluded them entirely from that protection until a local model generated a `return_result` array holding 1000+ repeated `"n/a"` elements with nothing ever catching it (root-caused 2026-08-31, fixed by this default change).
- **`auto_unstuck_interactive`**: `bool` (default `false`) — outside autonomous mode, whether a detected stall is nudged immediately or surfaced as a `prompt.stuck_alert` the user must confirm. **Autonomous mode always nudges immediately**, regardless of this flag.

Exposed in the **Kōdo Settings** webview panel's "General" section (kodo-vsix `kodo-settings-panel.ts`) via the `stuck_detection.get`/`.set` WS commands (`WS_PROTOCOL.md` §7.6d, `kodo/server/_app.py`'s `_handle_stuck_detection_get`/`_handle_stuck_detection_set`). The panel's radio group (`active`) and two checkboxes ("Also watch sub-agent turns" → `scope`, "Nudge LLM automatically without asking me" → `auto_unstuck_interactive`) apply on change — no explicit save button — and `.set` persists directly to `~/.kodo/etc/settings.json`; no `config.reload` follow-up is needed since this block is read fresh from disk on every stall check (§2.1 below). Hand-editing the file directly still works too, same as any other setting (SETTINGS.md §1).

### 2.3 The `on_stall` seam

`_run_agent_turn` (`_turns.py`) — the one turn loop shared by every entry-agent turn *and* every sub-agent subsession — gained one new optional parameter:

```python
on_stall: Callable[[TurnSignal], Awaitable[StallDecision]] | None = None
```

Called exactly once per round that ends with no tool calls, right before the turn would otherwise end. If it returns `StallDecision(retry=True, message=...)`, `_run_agent_turn` appends that message and loops again instead of breaking; `retry=False` (or `on_stall=None`) ends the turn exactly as before. This is the *only* seam stuck-detection has into the shared turn loop — every stuck-specific decision (settings, red-flag detection, the alarm gate, the worker queue) lives in the closure the caller supplies, built by `WatchdogMixin._make_stall_handler`. `_run_agent_turn` itself never imports settings, the gate, or the queue.

Three call sites build this closure, one per shape of turn:

- `_turns.py`'s `_run_entry_agent` — `is_entry_turn=True` (the live main turn).
- `_resume.py`'s `_resume_main_turn` — `is_entry_turn=True` (a crash-resumed continuation of the same shape of turn).
- `_subagents.py`'s `_drive_subsession` — `is_entry_turn=False`, `subsession_id=<id>`, `dispatcher=<the subsession's ToolDispatcher>` (§2.8 reads its `returned_output`; the two entry-agent call sites never pass this).

`_make_stall_handler`'s closure holds one piece of *sub-agent-only* mutable state, `stall_count`, capped at `_MAX_CONSECUTIVE_NUDGES` (2) — a safety valve against a sub-agent that never recovers: after two consecutive inline retries within *one* `_run_agent_turn` call, the closure gives up and lets the turn end normally rather than looping forever. This cap only bites a sub-agent's inline-retry paths (immediate, or repeated manual "Unstick it"). Entry-agent scope does not use `stall_count` at all — see §2.4a.

A second, much smaller closure — `on_tool_calls` (`WatchdogMixin._make_progress_handler`) — is built alongside `on_stall` at the two entry-turn call sites (`_turns.py`'s `_run_entry_agent`, `_resume.py`'s `_resume_main_turn`; **not** `_subagents.py`'s `_drive_subsession`, since sub-agent scope has no cross-turn streak to clear). `_run_agent_turn` calls it once per round that *does* produce at least one tool call, right before dispatching them — the mirror image of `on_stall`, which only fires on a round with none. See §2.4a for why this exists.

### 2.4 Remediation — two shapes, one decision tree

The closure's decision tree (inside `_on_stall`):

1. No red flags matched → `retry=False` (nothing else runs — in particular `_registry`/`_display_name` are never touched on the fast path). For entry-agent scope, this also clears `_stuck_streak` (§2.4a) — a genuine response always ends whatever streak was building.
2. Settings don't apply (`active`, `scope`, residence) → `retry=False`.
3. **Entry-agent scope** branches on `_stuck_streak` (§2.4a) before anything else: if already set, this is a second consecutive stall since the last real response — go critical (§2.4a) instead of nudging or asking again.
4. **Immediate** (`effective_autonomous` OR `auto_unstuck_interactive`) → persist the nudge, `retry=True` — appended to the *current* `messages` list, loop continues **inline**, in both the entry-agent and sub-agent case. Entry-agent scope also sets `_stuck_streak = True` here.
5. **Deferred, entry-agent scope** → the turn ends normally (`retry=False`) and `_schedule_entry_turn_alarm` is scheduled as a decoupled background task.
6. **Deferred, sub-agent scope** → `await self._gate.fire_stuck_alert(...)` is awaited **inline**, blocking this sub-agent's turn. Sub-agent scope alone still uses `stall_count >= _MAX_CONSECUTIVE_NUDGES` as its cap, unchanged from before.

Why entry-agent and sub-agent scope diverge at steps 5/6: an entry-agent turn ending normally is the *correct*, desired UX — `session.phase` goes to `"awaiting_user"`, the chat input is usable again, and the user should see that. Blocking that turn for up to several seconds (or indefinitely, waiting on a human) while it's supposed to look idle would be a regression. A sub-agent turn has no such state to protect: its parent is *already* blocked on it (spinner already showing, exactly like any other long-running sub-agent call), so asking inline — the same shape as an ordinary `prompt.permission` gate — costs nothing extra.

### 2.4a Escalation — one nudge per streak, then a critical notice

An entry-agent turn gets **at most one** nudge since its last sign of real progress. `WatchdogMixin._stuck_streak` (`bool`, engine-instance state, declared alongside `_entry_turn_seq` in `_core.py`/`_proto.py`) tracks whether that one nudge is still outstanding:

- Cleared (`False`) whenever `_on_stall` sees a round with no red flags (step 1 above) — a real response always resets the streak.
- **Also cleared (`False`) whenever a round produces at least one tool call** — `_make_progress_handler`'s `on_tool_calls` closure (§2.3), called from `_run_agent_turn` right before dispatch, regardless of whether the tool call(s) themselves later succeed or fail. This exists because `on_stall` is *only* ever invoked on a round with **zero** tool calls (§2.3) — a round that does call a tool never reaches `_on_stall` at all, so without this second clearing path, one early stall stayed "armed" through any number of subsequent successful tool-call rounds, and an unrelated later stall (e.g. a one-off model/parsing hiccup) would escalate straight to `_persist_stuck_critical` even though the agent had been visibly making progress the whole time in between. (Traced in session `1784487585`: a `unsloth-gemma4-26b-a4b-ud-q8-k-xl` GGUF/llama.cpp tool-call-parsing issue caused two unrelated stalls with 4 successful tool calls in between; the second stall went critical instead of nudging because nothing had cleared the streak. Fixed 2026-07-19.)
- Set `True` the moment a nudge actually lands for the entry agent — either inline, right after `_persist_nudge` in the immediate path (step 4), or, for the deferred/interactive path, inside `_run_entry_agent`'s `nudge_detail is not None` branch (`_turns.py`) once the queued nudge prompt is actually processed as a fresh turn. It is **not** set merely because an alarm was *scheduled* — dismissing a `prompt.stuck_alert` never sets it.
- If a *second* consecutive stall is detected while `_stuck_streak` is already `True`, `WatchdogMixin._persist_stuck_critical` runs instead of any nudge/alarm path, and the turn ends (`retry=False`). `_stuck_streak` is deliberately **not** cleared here — only a genuine response or a successful tool-call round clears it — so a third, fourth, ... consecutive stall with no real progress in between (e.g. the user manually re-prompts and it stalls again) keeps surfacing the same critical notice rather than nudging again.

This state is in-memory only, not persisted to `transient.json` — a server restart mid-streak just costs one extra nudge before the next stall goes critical, never a correctness issue (unlike `pending_security_alert`, which protects a dangling *tool dispatch*). Scope is deliberately entry-agent only: a sub-agent turn already has its own bounded inline-retry-then-silent-end behavior via `stall_count`/`_MAX_CONSECUTIVE_NUDGES` (§2.3), and a sub-agent subsession is comparatively short-lived, so the cross-turn "already tried once" problem this section addresses doesn't really arise there.

The critical notice itself (`EVT_AGENT_STUCK_CRITICAL` / `agent.stuck_critical`, `EngineEmitters.emit_agent_stuck_critical`) is a single user-facing sentence, **never fed back to the LLM** — client-only, mirroring `emit_error` rather than the nudge. See §2.5.

**`_schedule_entry_turn_alarm`** (entry-agent, deferred case only): captures `self._entry_turn_seq` (bumped once at the top of every `_run_entry_agent`/`_resume_main_turn` call), sleeps 1 second (`_ENTRY_TURN_ALARM_DELAY_S`), then re-checks `_entry_turn_seq` and `session.phase == "awaiting_user"` before firing `prompt.stuck_alert`. This double-checks (once before sleeping resolves would be redundant; the checks are *after* the sleep, and again after the gate resolves) that nothing else has superseded this turn in the meantime — a new prompt that started **and finished** inside the 1s window moves `_entry_turn_seq` forward even though `phase` would otherwise read `"awaiting_user"` again by coincidence. On "unstick", the nudge is enqueued onto the normal worker queue (`self._queue.put({"text": ..., "attachments": [], "nudge_detail": {...}})`) — functionally identical to a fresh `prompt.submit`, just tagged so it doesn't look like one (§2.5) and skips session titling (`_worker.py`: `if nudge_detail is None: self._titler.maybe_generate_session_title(text)`).

The background watcher task is held on `self._stuck_watchdog_task` so asyncio never garbage-collects it mid-sleep (a bare fire-and-forget `create_task` is only weakly referenced); a later watcher overwriting the reference is harmless since the earlier one is stale by construction and no-ops on its own `_entry_turn_seq` check.

### 2.5 The `Nudge` type — one message, two audiences, one persistence path

Every course-correction this file sends the model — an ordinary stall nudge, the missing-`return_result` reminder (§2.8), or either mid-stream notice (§2.7/§2.9/§2.10) — is one `kodo.runtime._engine._shared.Nudge`:

```python
@dataclass(frozen=True)
class Nudge:
    llm_text: str   # persisted as the message content; fed back to the model
    ui_text: str     # client-only; what the transcript/UI shows
    reasons: list[str]
    mode: str         # "auto" | "manual"
    source: str       # "stall" | "missing_return_result" | "cyclic_thinking"
                       # | "think_in_tool_call" | "tool_call_cyclic"
                       # | "repeated_tool_call"
```

Before 2026-08-03 this shape existed twice, independently: `_persist_nudge` (stall/missing-`return_result`, always `role="user"`, `kind="agent_unstuck_nudge"`) and `_persist_cyclic_thinking_notice` (always `role="assistant"`, `kind="cyclic_thinking_notice"`). Both carried the identical `{llm_text via content, ui_text via detail}` split — and, worse, kodo-vsix rendered them inconsistently: the cyclic-thinking notice showed as a **red** `<kodo_crit>` callout even though it is a real nudge the model keeps going from, visually indistinguishable from the two truly-terminal "gave up" notices. The two were unified into one `Nudge` type, one persistence path (`WatchdogMixin._persist_nudge(self, *, agent_name, subsession_id, nudge: Nudge, role: str)`), one `kind="nudge"`, and one wire event (`EVT_NUDGE`/`agent.nudge`) — every closure below builds a `Nudge` and calls this one method rather than persisting/emitting anything itself. `role` is still caller-supplied: `"user"` for stall/missing-`return_result` (the model responds to it as if told by the user to continue), `"assistant"` for cyclic-thinking/tool-call-cyclic/repeated-tool-call (first-person, read back as the model's own note — and, for §2.11, the role that keeps alternation intact after a `tool_result`).

`_persist_nudge` persists a real message the agent responds to (content = `nudge.llm_text`), so `HistoryProjector.load_main_messages` replays it into the LLM context on resume like any other turn. But it also carries `kind="nudge"` and a client-only `detail` (`{ui_text, reasons, mode, source}`) — mirrors the existing `kind="stopped_notice"` mechanism (`_persist_interrupted_turn`, `TransientStore.append_message`'s `kind` param) that already lets a real LLM-context message render as something other than a plain chat bubble.

Because the client never typed this message, it has no local echo — `EVT_NUDGE` (`agent.nudge`) is pushed live right after persisting (`EngineEmitters.emit_nudge`) so the running session shows it immediately; `HistoryProjector._message_to_entries`'s `kind == "nudge"` branch replays the same thing from `session.jsonl` on reload (the two legacy kinds, `agent_unstuck_nudge`/`cyclic_thinking_notice`, are also still recognized there and reshaped into the same entry — old sessions keep replaying correctly, nothing new is ever persisted under either legacy kind). Both produce the same `{ui_text, reasons, mode, source}` shape kodo-vsix renders (`SessionEntryView.tsx`'s `nudge` case) as a yellow `<kodo_warn>` callout — the Markdown renderer's existing warning-box tag (`markdown.tsx`), previously defined but unused; this was its first caller. `source` is what the client's reducer switches on to decide whether replaying this nudge also needs to flush a live mid-stream buffer (thinking or tool-call-argument display) — only the three mid-stream sources below (§2.7, §2.9, §2.10) ever fire with one still populated; `"stall"`, `"missing_return_result"` and `"repeated_tool_call"` all fire at a round boundary with nothing buffered.

The critical notices (§2.4a, §2.7, §2.9, §2.10) are simpler and stay outside the `Nudge` unification — they are display-only, mirroring `emit_error`/`security_rule_added` instead: each `EngineEmitters.emit_*_critical` persists a bare marker (e.g. `{"type": "agent_stuck_critical", "message": ...}`) via `TransientStore.append_marker` (not a `kind`-tagged message — there is no LLM-visible turn to attach it to) and pushes its own event live. `HistoryProjector.history_entries`'s matching branch replays each from `session.jsonl` on reload. kodo-vsix renders every one of them as a red `<kodo_crit>` callout — the same treatment as `error_notice`/`interrupted` — a plain append with no streaming state to clear for the two that fire only after a round already ended (`agent_stuck_critical`); the three mid-stream criticals (`agent_cyclic_thinking_critical`, `agent_think_in_tool_call_critical`, `agent_tool_call_cyclic_critical`) instead fully mirror `interrupted`/`runtime_error`, clearing every waiting indicator, since the turn genuinely ends there. Kept deliberately distinct from the nudge's shared element: a critical notice means "I gave up, nothing more to try," a fundamentally different signal from "I'm continuing, here's why."

Both `_persist_nudge` and every `_persist_*_critical` also emit a `_log.info`/`_log.warning` line (`session=<id> agent=<name> ... reasons=[...]`) to `server.log`, added 2026-07-19 — before this, neither path logged anything at all, so diagnosing a stuck episode after the fact meant cross-referencing `session.jsonl` against `llama-server.log` by timestamp with nothing in `server.log` to anchor the search (as happened investigating session `1784487585`).

### 2.6 The alarm gate — `prompt.stuck_alert`

A fourth `GateOrchestrator` request type (`fire_stuck_alert`, `kodo/runtime/_gates.py`), alongside `fire_approval`/`fire_questions`/`fire_permission` — same `kind=request`/Future/`register_response_future` mechanism, full spec in [WS_PROTOCOL.md](WS_PROTOCOL.md) §6.8. Modeled visually on `PermissionPanel` per its sibling-gate precedent, but:

- info-blue rather than warning-amber (`StuckAlertPanel.tsx`, `styles.ts`'s `stuckAlertCard`) — this is a behavioral observation, not a security risk. (No badge in the header — removed; the title sentence plus the STUCK? tag already say enough.)
- distinct **Unstick it** / **Dismiss** actions, no rule-offer checkboxes (there is nothing here to "always allow").
- **No `pending_*`-style crash-resume persistence.** Unlike `prompt.permission` (which persists `pending_security_alert` because a dangling *tool call* needs re-judging on resume) or `prompt.approval` (`pending_prompt`), nothing is left mid-dispatch if this wait is cut short by a server crash — the alarm is simply dropped, and the next matching stall (if any) schedules a fresh one. This was a deliberate scope cut: the heavier resume machinery those two gates need exists to protect a *tool dispatch* that might have partially landed; nothing here is dispatched at all until the user answers.

kodo-vsix renders it identically for both scopes (same blocking-panel placement PermissionPanel uses, replacing the compose box) — a simplification over a theoretically "more correct" non-blocking banner for the entry-agent case, accepted because the alarm is rare and one Dismiss click restores the input.

### 2.7 Mid-stream cyclic-thinking detection

Everything above inspects a *finished* round. This detector runs *during* a round — fed every streamed `ThinkingDelta` fragment as it arrives — so a runaway thinking block can be aborted before it burns through its whole token budget, not just explained afterward.

**Detection** — `kodo.runtime._cyclic_thinking.CyclicThinkingDetector`, a small, pure, dependency-free module (no engine imports) fed one streamed fragment at a time via `feed(fragment) -> bool`. Two layered checks against the trailing buffer (see the module docstring for the full rationale):

1. **Exact block-repeat** (the primary check, and what actually catches "the same 3 lines over and over"): fires the instant the buffer's tail is `_MIN_REPEATS` (3) back-to-back identical copies of some block whose length is between 24 and 600 characters. Admitting a full range of period lengths, not a handful of fixed sizes, is deliberate — real repeated content has whatever length its own lines happen to add up to, essentially never a "round" number. The 24-character floor is deliberate too: it structurally excludes single-word/short-phrase repetition (a doubled filler word, or even the same one word appearing three times by innocent coincidence), since the target failure mode is line-scale, not word-scale.

   Candidate periods are **found, not enumerated**, and this is load-bearing for cost rather than for behaviour. A qualifying period `p` necessarily makes the buffer's last `_MIN_PERIOD` characters (the *probe*) reappear verbatim exactly `p` characters earlier, so the candidate periods are precisely the probe's earlier occurrences in the trailing window — located with `str.rfind` at C speed and then confirmed with a single "tail equals itself shifted by one period" comparison. Ordinary non-repeating prose never re-hits its own probe, so the whole check is one failed search. This replaced a literal `for p in range(24, 601)` sweep that copied ~540 KB per call: because the check runs on **every streamed fragment**, that cost ~170 µs each time and blocked the streaming event loop for the better part of a second across a long thinking block (and made `test/test_cyclic_thinking.py` take 15 s). It is now under a microsecond per call. The rewrite is exactly equivalence-checked against the old sweep over ~16 k fuzz cases; the 24-character floor doubles as the probe length, so lowering `_MIN_PERIOD` toward word scale would both admit false positives *and* make the probe recur by chance.
2. **Fuzzy near-duplicate** (throttled — re-evaluated only every 200 new characters, since it's a pricier check): a `difflib.SequenceMatcher` similarity ratio (`autojunk=False` — the default `True` would understate similarity on exactly this repetitive text) between the two most recent 200-character chunks, catching near-repeats with minor variation (e.g. an incrementing number, mirroring the documented Gemma "Wait, I found it. The 14." loop) that the exact check would miss. Calibrated empirically, not just picked: threshold 0.90 with a required 2-in-a-row streak (mirroring the exact check's own repeat-count bar) cleanly separates a genuine loop from deliberately adversarial legitimate cases — a numbered list whose items share a template but differ substantively, and even a minimal-variation template ("Checking case A/B/C: looks fine so far.") that a looser threshold or a single-shot (non-streak) check let through as a false positive.

Only the trailing ~1800 characters are ever inspected by either check, so the retained buffer is trimmed once it grows past that — memory and per-call cost stay bounded no matter how long a round runs. A fresh detector is built per round (`_turns.py`'s `_run_agent_turn`, only when `on_cyclic_thinking` is provided) — a loop is scoped to one thinking block, never carried across rounds.

**The abort** — inside the streaming loop's `ThinkingDelta` branch, right after the fragment is fed to the client (so the live thinking display still shows the loop through the exact moment of detection): on a positive `feed()`, `await llm.cancel(stream_id)` is called and the `async for` is `break`-ed. This is the *first real caller* of `LLMPlugin.cancel()` — both the Anthropic and llama.cpp plugins have implemented it for a while (a per-stream `asyncio.Event`, checked once per streamed chunk) but nothing in the engine ever invoked it before this; `stop()` (the Stop button) instead cancels the whole worker `asyncio.Task`, a much blunter instrument. Breaking the `async for` locally, in addition to calling `cancel()`, makes the abort deterministic and immediately testable (a fake gateway just stops being consumed) rather than waiting out the provider's "within 1 second" grace period. Since `turn_end` stays `None` for an aborted round, the usual usage/cost-tracking block is automatically skipped, matching ordinary Stop-mid-stream behavior. `tool_calls` is guaranteed empty at this point — a provider always finishes streaming its thinking content before it can emit a tool call — so a cyclic abort only ever needs to interact with the turn loop's no-tool-calls branch.

**Escalation** — `WatchdogMixin._make_cyclic_thinking_handler` builds the `on_cyclic_thinking` closure, a third sibling of `_make_stall_handler`/`_make_progress_handler`, at the same three call sites as `on_stall` (§2.3). Gated by the **exact same** `stuck_detection` settings block as ordinary stalls (`active`/`scope`) — no second settings surface, no kodo-vsix settings-panel changes — via the same `_stuck_settings(...).applies(...)` check, evaluated eagerly at closure-construction time so a disabled/out-of-scope turn never even instantiates a detector. Deliberately simpler than `_make_stall_handler` in one respect: `auto_unstuck_interactive` and the `fire_stuck_alert` ask-first gate are **never consulted** — by the time this fires the stream is already dead and the repeated content already generated, so there's nothing left to defer or ask about; remediation is always immediate, for both strikes and both scopes.

- **Entry-agent scope**: a **dedicated** streak, `WatchdogMixin._cycle_streak` (declared alongside `_stuck_streak` in `_core.py`/`_proto.py`/`_watchdog.py`), deliberately separate from `_stuck_streak` (and from `_think_tag_streak`/`_tool_call_cycle_streak`, §2.9/§2.10) so an ordinary stall and a detected thinking loop never combine to trip either escalation's two-strike cap. Cleared under the same "genuine progress" conditions as `_stuck_streak` — the *existing* clearing hooks (`_on_stall`'s no-flags branch, `_make_progress_handler`'s `on_tool_calls` closure) were **extended** to also clear `_cycle_streak`, not duplicated. First hit: a `Nudge` (§2.5, `source="cyclic_thinking"`, `role="assistant"`) persisted via the shared `_persist_nudge` — one message that is *both* the LLM-visible course-correction the agent reads back next round *and* (via `kind="nudge"`) the `<kodo_warn>` callout the user sees; `retry=True`. Second consecutive hit: `_persist_cyclic_thinking_critical` — client-only, never fed back to the LLM, mirroring `_persist_stuck_critical` but with distinct wording naming the actual cause; `retry=False`, ending the turn (the session settles into `awaiting_user`, the existing idle-equivalent phase — no new phase was needed).
- **Sub-agent scope** (only reached when `scope` includes subagents): a dedicated local counter capped at the existing `_MAX_CONSECUTIVE_NUDGES`, inline-retry-then-silent-end, no critical banner at all — mirrors how ordinary sub-agent stalls already behave differently from entry-agent ones (§2.3).

**Persistence & rendering** — the first-strike notice reuses the unified `Nudge`/`_persist_nudge`/`EVT_NUDGE` machinery (§2.5) rather than anything bespoke; the critical hit is its own event, `EVT_AGENT_CYCLIC_THINKING_CRITICAL`/`agent.cyclic_thinking_critical` (`EngineEmitters.emit_cyclic_thinking_critical`), kept separate from `EVT_AGENT_STUCK_CRITICAL` since the root cause and message differ, persisted as an `agent_cyclic_thinking_critical` marker (`HistoryProjector._marker_to_entries`) exactly like `agent_stuck_critical`. kodo-vsix renders the notice as the shared yellow `<kodo_warn>` callout (`SessionEntryView.tsx`'s `nudge` case) and the critical hit as a red `<kodo_crit>` one — but the two live reducer cases are **not** both a plain append: the notice fires mid-round, with the round's repeated thinking text still live in `state.streamingThinking` (every fragment was forwarded to the client before the detector saw it), and round 2 starts immediately after with no intervening `stream_end` — a plain append would leave round 1's garbage sitting in the buffer for round 2's genuine `thinking_token` events to append onto. The `nudge` reducer case therefore checks `action.source` and, for the three mid-stream sources, commits+clears `streamingThinking`/`streamingTokens`/`thinkingActive`/`thinkingStartedAt` *and* `streamingToolgen`/`toolgenActive`/`toolgenToolName`/`toolgenStartedAt` (mirroring `toolgen_token`'s "starting" branch, which has the same "more streaming still coming" shape) but deliberately leaves `awaitingLlm`/`streaming`/`llmWaiting` alone, since round 2's imminent `llm_turn_start` will set those correctly. The critical case, by contrast, *does* fully mirror `interrupted`/`runtime_error` (clearing every waiting indicator too) since the turn genuinely ends there.

**A pre-existing bug found and fixed on this same seam**: `_run_agent_turn`'s generic `persist` callback used to re-persist an `on_stall` retry's message a second time, untagged, because `persisted_upto` never learned that the closure (`_persist_nudge`) had already written it directly via a `kind`-tagged `TransientStore.append_message` call. Confirmed live in production for the ordinary nudge (the main entry-agent turn passes a real `persist=` callback) — a duplicate, untagged `role="user"` line landed in `session.jsonl`, rendering as a fake user-typed chat bubble and duplicating the nudge text in the LLM's context on any resume-after-nudge. Fixed by flushing the round's own message *before* invoking `on_stall`/`on_cyclic_thinking` (rather than after), and marking a closure-returned retry message as already-durable (`_mark_already_persisted()`) instead of flushing it a second time.

### 2.8 Missing-`return_result` enforcement

A third, unrelated failure mode shares this same `on_stall` seam: a sub-agent
that produces a perfectly clean, non-stalled final response — no red flag
from §2.1 fires — but never called `return_result` at all. `_drive_subsession`
(`_subagents.py`) already had a fallback for this (a bare
`{schema_compliance: False}` result, so the caller isn't left with nothing),
but until this section's addition (2026-08-02) the engine never told the
model it had missed a hard requirement, and never gave it a chance to fix it
before the subsession was marked failed. Motivating incident: session
`1785719012` — `toolchain_builder` did all its real work correctly (created
every file, ran every verification command, even re-ran things twice to
confirm idempotency), then just wrote a prose summary and stopped. The
subsession correctly ended up `failed: true` (driving kodo-vsix's red
`<kodo_crit>` "subagent failed to complete the task" banner), but the model
was never told *why*, and never got a chance to actually finish the job by
calling the one tool it forgot.

**Deliberately independent of `stuck_detection` settings entirely** — the one
place in this file where that isn't true. Every other check here is a
heuristic ("this looks like it might be stuck"), gateable because a user might
reasonably not want the overhead/interruption of guessing wrong. This is not
a heuristic: every sub-agent's own `ToolDispatcher` is constructed with the
target agent's `output_schema` (`_make_dispatcher`, `_turns.py`), so
`return_result` is a hard contract, not a maybe. Gating it behind
`stuck_detection.active`/`scope` would mean a user who turned stuck detection
off (or scoped it to entry-agent-only) silently loses return_result
enforcement too, which has nothing to do with why they turned that setting
off. It also runs regardless of model residence (local or cloud) — the check
itself costs nothing when a well-behaved model already called `return_result`,
since that's exactly the condition it looks for before doing anything.

**Trigger** — `_end_or_nudge_missing_return_result`, a closure built alongside
`_on_stall` inside the same `_make_stall_handler` call (only when
`is_entry_turn=False`; entry agents never call `return_result`, so `dispatcher`
is never passed for them). It is invoked, uniformly, in place of *every*
`StallDecision(retry=False)` a sub-agent-scope `_on_stall` would otherwise
return — whether the turn looked clean (no §2.1 flags), stuck_detection
doesn't apply to this turn at all, the ordinary `stall_count` retries are
already exhausted, or the user declined a manual "Unstick it". Whatever the
reason the turn is about to end, if `dispatcher.returned_output is None`, the
sub-agent hasn't met its contract yet.

**Cap** — exactly one nudge, tracked by `return_result_nudged`, a closure-local
bool deliberately kept separate from `stall_count` and `_cycle_streak` (same
reasoning as keeping those two apart, §2.7): a subsession that already burned
its `stall_count` budget on unrelated stalls still gets its own, independent
chance to hear specifically about `return_result` before the engine gives up
on it. If `return_result` is still uncalled the next time this closure runs,
it falls through to an ordinary `retry=False` and `_drive_subsession`'s
pre-existing `{schema_compliance: False}` fallback takes over exactly as
before — this section only changes what happens *before* that point, not the
failure signal itself (see doc/TOOLS.md §5A for the companion fix to how that
signal is normalized for the caller).

**The nudge itself** reuses `_persist_nudge` verbatim — same persistence
(`kind="nudge"`, `source="missing_return_result"`), same live event
(`EVT_NUDGE`/`agent.nudge`), same kodo-vsix rendering
(`SessionEntryView.tsx`'s existing `nudge` case) as any other
stall nudge — so this needed zero new persistence/UI code. The one thing that
differs from an ordinary stall nudge is the LLM-visible text: the `Nudge`
built here sets `llm_text=_MISSING_RETURN_RESULT_LLM_TEXT` instead of the
generic `_NUDGE_LLM_TEXT` every ordinary stall nudge uses, because "continue
from exactly where you left off" doesn't actually tell a model that finished
its real work what it needs to do differently. `_MISSING_RETURN_RESULT_LLM_TEXT`
names the tool explicitly: *"You finished without calling `return_result`.
This is a hard requirement: the task is not done until you call it. Call
`return_result` now with your final result."* The user-facing `ui_text`
(`_nudge_note`, via a new `_MISSING_RETURN_RESULT_FLAG` red-flag-shaped
constant fed through the same formatting as any other reason) still reads
generically ("Kōdo noticed X appeared to stop mid-task (it finished without
calling `return_result`...) and continued it automatically") — only the text
the model itself reads back is bespoke.

### 2.9 Mid-stream think-in-tool-call detection

A third failure mode, distinct from both §2.7 and §2.8: a model emits a literal `<think>...</think>` block **inside a tool call's JSON arguments** instead of (or in addition to) using its own thinking channel — e.g. narrating its reasoning inside a `run_subagent` call's task string. This is never valid output (arguments are consumed as structured data, not prose), and it motivated this section directly: a local model (Laguna S 2.1) embedded a thinking block inside a sub-agent task description, then — still inside that same tool-call argument text — degenerated into repeating the same sentence forever (the exact shape §2.10 also catches, independently).

**Detection** — `kodo.runtime._think_tag_guard.ThinkTagDetector`, a small, pure, dependency-free module mirroring `_cyclic_thinking.py`'s style: `feed(fragment) -> bool`, boundary-safe (a tag split across two streamed fragments, e.g. `"<thi"` then `"nk>"`, is still caught via a short retained tail). Unlike the repetition detectors, there is nothing to calibrate — a single occurrence of the open tag is already a protocol violation, not a heuristic judgment call, so the check is a simple boundary-safe substring search.

**The stream** — `ToolCallArgDelta` (`kodo.llms._interface`): an incremental fragment of a tool call's arguments as the model streams them, existing since before this feature purely as a *display-only* event (surfacing a live "generating" indicator for large arguments, e.g. a `create_file` call's `content`) — `_turns.py`'s `_run_agent_turn` loop forwarded it to the client but otherwise ignored it entirely. This section (and §2.10) is the first thing to actually *inspect* that stream. Only the llama.cpp plugin emits it today (`_llama.py`); Anthropic tool calls arrive as one complete `ToolCallEvent` with no incremental fragments. Both new mid-stream detectors are therefore local-model-only in practice, architecturally rather than by settings — the event they read simply never arrives from a cloud plugin.

**The abort** — a new `elif isinstance(event, ToolCallArgDelta):` branch in the streaming loop, checked *before* `ToolCallEvent`s can ever arrive: the llama.cpp plugin only yields `ToolCallEvent`s once, after the whole stream finishes (the `for idx in sorted(tool_ids): yield ToolCallEvent(...)` block runs post-loop) — so `tool_calls` is structurally guaranteed empty the instant either new abort fires, exactly like the existing cyclic-thinking abort's documented invariant (§2.7). `think_tag_detector.feed()` is checked *before* the tool-call-cyclic detector (§2.10) on every fragment — a stray thinking tag takes priority over ordinary repetition when both would fire on the same content. On a hit: `await llm.cancel(stream_id); break`, then `on_think_in_tool_call(tool_name)` is awaited in place of `on_stall`/`on_cyclic_thinking`/`on_tool_call_cyclic`.

**Escalation** — `WatchdogMixin._make_think_in_tool_call_handler` is the **one closure in this entire module not gated by `stuck_detection` settings at all** — always returns a real closure, mirroring §2.8's missing-`return_result` gate: a stray thinking tag inside structured tool-call data is a hard contract violation, not a "might be stuck" heuristic a user might reasonably want a say in. (It is still, in practice, local-only — see above.) Otherwise identical in shape to §2.7's cyclic-thinking handler: entry-agent scope uses its own dedicated streak, `WatchdogMixin._think_tag_streak` (independent of `_stuck_streak`/`_cycle_streak`/`_tool_call_cycle_streak` — same "don't let unrelated failure modes combine to trip an escalation that isn't theirs" reasoning as every other streak in this file), one nudge then `_persist_think_in_tool_call_critical` on a second consecutive hit; sub-agent scope is a capped local counter, inline retry, silent end on the cap, no critical banner. The nudge names the tool explicitly (mirrors `_MISSING_RETURN_RESULT_LLM_TEXT`'s specificity): *"You are not allowed to think inside a tool call. Thinking happened inside your `{tool_name}` call. Put any reasoning in your own thinking block, not inside tool-call arguments, then call `{tool_name}` again with clean arguments."*

**Persistence & rendering** — reuses the unified `Nudge`/`_persist_nudge` (§2.5) verbatim: `source="think_in_tool_call"`, `role="user"` (the model is told, not narrating its own realization — unlike §2.7/§2.10's first-person notices). The critical hit (`EVT_AGENT_THINK_IN_TOOL_CALL_CRITICAL`/`agent.think_in_tool_call_critical`, `EngineEmitters.emit_think_in_tool_call_critical`) mirrors `_persist_cyclic_thinking_critical`'s shape exactly — its own marker/event, kept distinct rather than reusing an existing critical type, since the root cause differs.

### 2.10 Mid-stream tool-call-argument repetition detection

The companion check to §2.9, on the same `ToolCallArgDelta` stream: a model's tool-call arguments degenerating into a repetition loop — the same failure mode §2.7 catches inside thinking blocks, but inside structured tool-call data instead. Reuses `kodo.runtime._cyclic_thinking.CyclicThinkingDetector` **unchanged** — the class has nothing thinking-specific about it, only its module name and docstring do — as a second, independent instance per round, fed `ToolCallArgDelta.text` fragments instead of `ThinkingDelta.text` ones.

**The abort** — same streaming-loop branch as §2.9: `think_tag_detector` is checked first on every fragment; only if it does *not* fire is the fragment also fed to `tool_call_cyclic_detector`. On a hit: `await llm.cancel(stream_id); break`, then `on_tool_call_cyclic(accumulated_arg_text)` is awaited in place of `on_stall`. Like §2.9, `tool_calls` is guaranteed empty at this point.

**Escalation** — `WatchdogMixin._make_tool_call_cyclic_handler`, gated by the exact same `stuck_detection` settings as ordinary stalls and §2.7's thinking-block detector (unlike §2.9: this *is* a heuristic — "this looks like a loop" — not a hard protocol violation, so a user can reasonably turn it off). Otherwise identical in shape to `_make_cyclic_thinking_handler`: own dedicated streak, `WatchdogMixin._tool_call_cycle_streak`; entry-agent one-nudge-then-critical, sub-agent capped-then-silent; remediation always immediate for both strikes and both scopes (the stream is already dead and the repeated content already generated by the time this fires — nothing left to defer or ask about). First-person, dual-role notice text (mirrors `_CYCLIC_THINKING_NOTICE`): *"I noticed my tool call's arguments had fallen into a repetitive loop, generating the same content over and over, and stopped it before it could burn through the rest of the turn. I will not continue down that line — let me regenerate this tool call's arguments from scratch."*

**Persistence & rendering** — reuses the unified `Nudge`/`_persist_nudge` (§2.5): `source="tool_call_cyclic"`, `role="assistant"` (first-person, same dual-role shape as the cyclic-thinking notice). The critical hit (`EVT_AGENT_TOOL_CALL_CYCLIC_CRITICAL`/`agent.tool_call_cyclic_critical`, `EngineEmitters.emit_tool_call_cyclic_critical`) mirrors the other critical emitters — its own marker/event, kept distinct since the root cause differs.

### 2.11 Repeated-tool-call (no-progress round) detection

The one detector here that fires on a round which **did** call a tool. Everything above is anchored either on a round that produced *no* tool call (§2.1's `_DETECTORS` only ever run from `on_stall`) or on content streaming *within* one round (§2.7, §2.9, §2.10). That left a whole failure mode with no observer at all: a model that keeps making the *same call with the same arguments*, getting the *same result* back, round after round after round. Every such round ends `stop_reason: "tool_use"`, so nothing flagged it — and the turn loop actively counted each lap as progress (see "Progress is now conditional" below).

**The incident** — session `1788543589`, subsession `7cc7034e6bf44e1a885614407cfa442a`: the `requirements_critic` sub-agent on local model `unsloth-laguna-s-2-1-mxfp4-moe` issued `read_file{"path": "kodo-snake/specs/architecture/system.md"}` for ~1133 consecutive rounds — byte-identical call, identical `{"error": "File not found: ..."}` result every time — for **32 minutes and 62.4M cumulative input tokens** before the run was stopped by hand. `stuck_detection` was on, with the default `scope: "top_level_and_subagents"`, and not one check fired.

**Detection** — `kodo.runtime._repeated_tool_calls.RepeatedToolCallDetector`, one instance per `_run_agent_turn` call (a loop is scoped to one turn, never carried across turns — unlike the four cross-turn streaks, and unlike the three mid-stream detectors, which are per-*round*). `round_signature(calls, results)` fingerprints a whole round — every tool call **and** every result — as one SHA-256 digest; `feed(signature)` returns how many times that signature has now occurred back-to-back. `_MIN_TOOL_CALL_REPEATS = 3` consecutive identical rounds is a loop; deliberately the same number as `_cyclic_thinking._MIN_REPEATS`, with a test pinning them together so they cannot drift apart (two identical calls in a row is a plausible retry; three is a loop).

Two properties of that signature are load-bearing, and both are pinned by tests:

- **Results are part of it.** Re-reading a file an intervening edit changed, or polling a build still running, is the same *call* with a different *result* — real progress, never flagged. Only a round that was provably incapable of teaching the model anything counts.
- **The result compared is the normalized, LLM-visible one** (what `_finalize_tool_result` returns after `normalize_output` strips undeclared fields), not the raw string the tool handler returned. What the model reads back is the only thing that can constitute progress *for it*: two rounds differing solely in a field that gets normalized away are identical from inside the model's context, and must still be caught.

**Repeats must be strictly back-to-back.** An alternating `A, B, A, B` cycle is deliberately *not* caught — that shape has never been observed, and the window state a looser rule needs is false-positive surface this does not need to take on.

**Progress is now conditional** — the second half of the fix, and the reason the loop was invisible to *every* other detector rather than just to a missing one. `_run_agent_turn` used to call `on_tool_calls()` before dispatching, on every tool-calling round, and `_make_progress_handler` clears all five streaks. So each lap of the loop reset the whole watchdog and a looping agent looked permanently healthy. The hook now fires **after** dispatch (it has to: whether the round was progress depends on what the tools returned) and **only when `repeats == 1`** — a novel round. When the detector is gated off, `repeats` is hardcoded to `1` and the hook keeps its original every-tool-round behavior exactly, so the gating rides on the detector rather than on a second settings surface.

**Escalation** — `WatchdogMixin._make_repeated_tool_call_handler`, gated by the same `stuck_detection` settings as every other heuristic here (returning `None` both disables remediation *and* tells `_run_agent_turn` not to build a detector at all). Shape mirrors §2.10: remediation always immediate for both strikes and both scopes — the loop is already proven, so there is nothing `auto_unstuck_interactive`/`fire_stuck_alert` could usefully ask about — with its own dedicated streak `WatchdogMixin._repeat_streak`; entry-agent gets one nudge then a critical, sub-agent gets up to `_MAX_CONSECUTIVE_NUDGES` inline retries then ends the turn.

The detector is deliberately **not** reset by a nudge: once past the threshold, every further identical round is its own strike. A model just told in plain words to stop repeating a call, that repeats it anyway on the very next round, has already answered the question the threshold was asking. Concretely, the traced loop now ends at round 4 (entry-agent: nudge, repeat, critical) or round 5 (sub-agent: nudge, nudge, stop) instead of running to 1140.

**The notice** (`_repeated_tool_call_llm_text`) names the offending call, for the same reason §2.9's does — a generic "stop repeating yourself" leaves a model that is already looping to guess *which* call, and it is the last one to guess right. It spells out three ways out on purpose: use the result you already have, make a materially different call, **or finish and report plainly what you could not do**. A model looping on a failing call is usually missing exactly that last idea — the traced run repeated a `File not found` ~1133 times rather than proceed without the file — so the notice has to say that giving up on the call and reporting it is a legitimate ending.

**Persistence & rendering** — reuses the unified `Nudge`/`_persist_nudge` (§2.5): `source="repeated_tool_call"`, `role="assistant"` (first-person, same dual-role shape as §2.7/§2.10). `role="assistant"` also keeps strict user/assistant alternation here, which matters at this position specifically: the nudge lands right after the round's `tool_result` **user** message, so a `"user"` nudge would put two user messages back to back.

The critical (`_persist_repeated_tool_call_critical`) deliberately **reuses `emit_agent_stuck_critical`** rather than adding a fifth critical event. A tool-call loop *is* the generic "this agent is stuck and I gave up" case as far as the client is concerned; the three mid-stream detectors only have dedicated events because kodo-vsix's reducer must flush a live mid-stream buffer for them, and this one has no such buffer. **The wire protocol is therefore unchanged by §2.11** — no new event, no new marker, no kodo-vsix reducer branch. The only client-visible novelty is a new `source` string on an existing event, which the reducer already treats as "not a mid-stream source" by default.

## 3. Known limitations / deliberate scope cuts

- **`workflow_mode == "judge"` never gets the nudge's `kind`/`detail` tagging.** `_run_judge_with_input` doesn't accept `nudge_detail` (unlike `_run_guide_with_input`/`_run_problem_solver_with_input`) — a judge-session nudge would ride through as a plain untagged prompt. In practice this never surfaces: `kodo.validator._evaluate` always forces the judge session into autonomous mode (`MSG_MODE_SET, autonomous=True`), so remediation for a judge turn is always the immediate/inline path, which never touches the worker queue or `nudge_detail` at all.
- **The validator's scripted/LLM user-proxy doesn't know `prompt.stuck_alert`.** `kodo.validator._client.py`'s `__build_answer` falls back to `{"error": "unsupported_request"}` for any request type it doesn't recognize — `fire_stuck_alert` reads `action` from that (absent → defaults to `"dismiss"`), so an interactive (non-autonomous) validator scenario that hits a genuine stall gets a clean, immediate "dismiss" rather than hanging. Teaching the validator's user-proxy to actually answer "unstick" is a reasonable future enhancement, not a correctness gap.
- **Scope is `run_subagent_<name>`-spawned sub-agents only** (including the critics the engine spawns inside an author's review loop).** The internal *silent* tool-calling loops (`compactor`, `web_search`'s `_run_silent_tool_loop_turn`) don't go through `_run_agent_turn` at all and are out of scope — they already have their own "nudge the model to keep going" handling (`_run_silent_tool_loop_turn`'s own no-tool-calls branch).
- **The one-nudge-then-critical escalation (§2.4a) is entry-agent scope only**, and `_stuck_streak` is in-memory, not persisted — a deliberate pair of scope cuts, not oversights. Sub-agent scope keeps its pre-existing `stall_count`/`_MAX_CONSECUTIVE_NUDGES` behavior unchanged (up to 2 inline retries, then a silent end with no critical notice).
- **The cyclic-thinking detector (§2.7) only watches thinking blocks, not final-answer text** — the same repetition-collapse bug is also documented happening in constrained/final output, not just `<think>` blocks, but that's out of scope for this pass. Its exact-repeat check also only tries periods from 24 to 600 characters — a real loop whose period happens to fall outside that range (an unusually short or, more likely, unusually long repeated block) is left entirely to the fuzzy near-duplicate check, which may or may not catch it depending on how much the repeats vary. It is gated by the same `stuck_detection.active`/`scope` settings as ordinary stall detection, so — like the rest of this doc — it is off for cloud models by default too, consistent with this being a documented local-model failure mode.
- **Missing-`return_result` enforcement (§2.8) is always immediate, never gated behind `fire_stuck_alert`/`auto_unstuck_interactive`.** Every other sub-agent-scope remediation in this file asks first when not in the immediate path (§2.4 step 6); this one does not, by deliberate choice — it's enforcing a hard tool contract the sub-agent already agreed to (not a "maybe it's stuck" heuristic a user might reasonably want a say in), and the caller's turn is already blocked waiting on this subsession either way, so there's no idle-state UX to protect by asking first.
- **Both mid-stream tool-call-argument detectors (§2.9, §2.10) are local-only architecturally, not by a settings check.** They read `ToolCallArgDelta`, which only the llama.cpp plugin emits (Anthropic tool calls arrive as one complete event with no incremental fragments) — so a cloud turn's tool calls are never inspected by either, regardless of `stuck_detection.active`. §2.9 is additionally never gated by `stuck_detection` at all (a deliberate choice, not an oversight — see §2.9); §2.10 is gated the normal way, but the gate is redundant for cloud residence since the event never arrives there either way.
- **§2.9/§2.10 only watch tool-call *arguments*, not the tool name or the fact that a call was made at all** — a model that emits a well-formed `<think>` tag as an entire tool call's sole argument value is not caught by either. Both are scoped to the exact incident that motivated them (a stray thinking tag, or a repetition loop, *within* one call's streamed argument text). The neighbouring gap this bullet used to also claim — *"loops by repeating whole separate tool calls rather than repeating within one call's arguments"* — **is now covered by §2.11**, which was written after that exact scope cut cost 32 minutes and 62.4M input tokens in session `1788543589`. Treat the rest of this list accordingly: these are untested-in-the-wild guesses about what will not matter, and at least one of them was wrong.
- **§2.11 requires strictly back-to-back repeats and an exact signature match.** An alternating `A, B, A, B` loop, or one whose calls differ in some irrelevant field (a changing `tool_use_id` is excluded from the signature, but e.g. a timestamp inside the arguments would not be), slips through. Both are deliberate: the observed failure is exact and adjacent, and every relaxation buys coverage with false-positive risk against a check that can end a turn. If a non-adjacent loop is ever actually observed, the detector's state (one signature + one counter) is the thing to widen — not the threshold.
- **§2.11 is scoped to one `_run_agent_turn` call.** A model that loops, gets nudged into ending the turn, and then reopens the identical loop on the *next* turn starts from a clean detector. There is no cross-turn `repeat_streak` equivalent for the signature itself (only `_repeat_streak`, which tracks the one-nudge-then-critical escalation and is cleared by the next genuine response or novel tool round, like the other four).
