# Kodo — Stuck-Agent Detection & Remediation

> Reference: [STATE_AND_LIFECYCLE.md](STATE_AND_LIFECYCLE.md) (turn/session lifecycle), [SETTINGS.md](SETTINGS.md) §2.6 (`stuck_detection`), [WS_PROTOCOL.md](WS_PROTOCOL.md) §5.9e/§6.5a, [SECURITY.md](SECURITY.md) (sibling `prompt.*` gate precedent).

## 1. The failure this addresses

Local LLMs occasionally end a turn without actually finishing the task — most visibly, a call that produces **no tool call and no visible text**. The engine already had a sentinel for this (`"(no text)"`, `kodo/runtime/_engine/_turns.py`'s `_run_agent_turn`), but treated it as an ordinary turn end: the entry-agent turn just goes idle (`session.phase == "awaiting_user"`) with the task unfinished and no explanation, and a sub-agent turn hands its parent a near-empty `return_result` fallback.

A concrete example that motivated this feature: session `1784394478` (Problem Solver, local model `unsloth-gemma4-26b-ud-q8-k-xl`, One Billion Row Challenge task) — mid-investigation ("Let me check `build.sh` content...") the model's final call returned `stop_reason: "end_turn"`, zero tool calls, and empty text. The session went idle with the task nowhere near done, and nothing told the user.

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

Two detectors ship today, both drawn directly from real failure modes:

| `code` | Fires when | Evidence |
|---|---|---|
| `empty_final_turn` | No tool call **and** no visible text | The motivating session above — a legitimate completion always says *something*; an empty final turn is never a real "I'm done". |
| `truncated_generation` | `stop_reason == "max_tokens"` | llama.cpp's `"length"` finish reason, remapped in `kodo/llms/llamacpp/_llama.py`'s `_map_finish_reason` — the model was cut off mid-generation by its output-token cap, possibly mid-sentence or mid-plan. |

`detect_red_flags(signal)` runs every registered detector and returns every match (never short-circuits). **To add a new red flag**: write one more `TurnSignal -> RedFlag | None` function and append it to `_DETECTORS` in `_watchdog.py` — no other wiring required. Nothing about the settings, the gate, or the turn loop needs to change.

### 2.2 Policy — `stuck_detection` settings

Three independent knobs (`kodo/server/_config.py`'s `_DEFAULT_USER_SETTINGS["stuck_detection"]`; see [SETTINGS.md](SETTINGS.md) §2.6 for the full reference):

- **`active`**: `"off" | "local_only" | "local_and_cloud"` (default `"local_only"`) — this is primarily a local-model failure mode; cloud models (Claude) essentially never exhibit it, so the default only watches local turns.
- **`scope`**: `"top_level" | "top_level_and_subagents"` (default `"top_level"`) — whether only the shared entry-agent turn (Guide/Problem Solver) is watched, or sub-agent turns (`run_subagent`/`run_author_critic_iteration`) too.
- **`auto_unstuck_interactive`**: `bool` (default `false`) — outside autonomous mode, whether a detected stall is nudged immediately or surfaced as a `prompt.stuck_alert` the user must confirm. **Autonomous mode always nudges immediately**, regardless of this flag.

Not yet exposed in the Kōdo Settings webview panel or a dedicated `WS_PROTOCOL.md` set-command — for now, change it by hand-editing `~/.kodo/etc/settings.json` and sending `config.reload`, same as any other setting (SETTINGS.md §1). Wiring a UI section for it is a deliberately deferred follow-up, not a gap in the underlying mechanism.

### 2.3 The `on_stall` seam

`_run_agent_turn` (`_turns.py`) — the one turn loop shared by every entry-agent turn *and* every sub-agent subsession — gained one new optional parameter:

```python
on_stall: Callable[[TurnSignal], Awaitable[StallDecision]] | None = None
```

Called exactly once per round that ends with no tool calls, right before the turn would otherwise end. If it returns `StallDecision(retry=True, message=...)`, `_run_agent_turn` appends that message and loops again instead of breaking; `retry=False` (or `on_stall=None`) ends the turn exactly as before. This is the *only* seam stuck-detection has into the shared turn loop — every stuck-specific decision (settings, red-flag detection, the alarm gate, the worker queue) lives in the closure the caller supplies, built by `WatchdogMixin._make_stall_handler`. `_run_agent_turn` itself never imports settings, the gate, or the queue.

Three call sites build this closure, one per shape of turn:

- `_turns.py`'s `_run_entry_agent` — `is_entry_turn=True` (the live main turn).
- `_resume.py`'s `_resume_main_turn` — `is_entry_turn=True` (a crash-resumed continuation of the same shape of turn).
- `_subagents.py`'s `_drive_subsession` — `is_entry_turn=False`, `subsession_id=<id>`.

`_make_stall_handler`'s closure holds one piece of mutable state, `stall_count`, capped at `_MAX_CONSECUTIVE_NUDGES` (2) — a safety valve against a model that never recovers: after two consecutive inline retries within *one* `_run_agent_turn` call, the closure gives up and lets the turn end normally rather than looping forever. This cap only bites the inline-retry paths (immediate, or a sub-agent's repeated manual "Unstick it"); the entry-agent deferred path never loops at all (see below), so each of its nudges starts a brand-new `_run_agent_turn` call with its own fresh counter.

### 2.4 Remediation — two shapes, one decision tree

The closure's decision tree (inside `_on_stall`):

1. No red flags matched → `retry=False` (nothing else runs — in particular `_registry`/`_display_name` are never touched on the fast path).
2. Settings don't apply (`active`, `scope`, residence) → `retry=False`.
3. `stall_count >= _MAX_CONSECUTIVE_NUDGES` → `retry=False`.
4. **Immediate** (`effective_autonomous` OR `auto_unstuck_interactive`) → persist the nudge, `retry=True` — appended to the *current* `messages` list, loop continues **inline**, in both the entry-agent and sub-agent case.
5. **Deferred, entry-agent scope** → the turn ends normally (`retry=False`) and `_schedule_entry_turn_alarm` is scheduled as a decoupled background task.
6. **Deferred, sub-agent scope** → `await self._gate.fire_stuck_alert(...)` is awaited **inline**, blocking this sub-agent's turn.

Why entry-agent and sub-agent scope diverge at steps 5/6: an entry-agent turn ending normally is the *correct*, desired UX — `session.phase` goes to `"awaiting_user"`, the chat input is usable again, and the user should see that. Blocking that turn for up to several seconds (or indefinitely, waiting on a human) while it's supposed to look idle would be a regression. A sub-agent turn has no such state to protect: its parent is *already* blocked on it (spinner already showing, exactly like any other long-running sub-agent call), so asking inline — the same shape as an ordinary `prompt.permission` gate — costs nothing extra.

**`_schedule_entry_turn_alarm`** (entry-agent, deferred case only): captures `self._entry_turn_seq` (bumped once at the top of every `_run_entry_agent`/`_resume_main_turn` call), sleeps 5 seconds (`_ENTRY_TURN_ALARM_DELAY_S`), then re-checks `_entry_turn_seq` and `session.phase == "awaiting_user"` before firing `prompt.stuck_alert`. This double-checks (once before sleeping resolves would be redundant; the checks are *after* the sleep, and again after the gate resolves) that nothing else has superseded this turn in the meantime — a new prompt that started **and finished** inside the 5s window moves `_entry_turn_seq` forward even though `phase` would otherwise read `"awaiting_user"` again by coincidence. On "unstick", the nudge is enqueued onto the normal worker queue (`self._queue.put({"text": ..., "attachments": [], "nudge_detail": {...}})`) — functionally identical to a fresh `prompt.submit`, just tagged so it doesn't look like one (§2.5) and skips session titling (`_worker.py`: `if nudge_detail is None: self._titler.maybe_generate_session_title(text)`).

The background watcher task is held on `self._stuck_watchdog_task` so asyncio never garbage-collects it mid-sleep (a bare fire-and-forget `create_task` is only weakly referenced); a later watcher overwriting the reference is harmless since the earlier one is stale by construction and no-ops on its own `_entry_turn_seq` check.

### 2.5 Persistence & rendering — an LLM-visible turn, a client-only explanation

The nudge is a real `role: "user"` message the agent responds to (`_NUDGE_LLM_TEXT`, a fixed "You stopped before finishing the task... continue from exactly where you left off."), persisted normally so `HistoryProjector.load_main_messages` replays it into the LLM context on resume like any other turn. But it also carries `kind="agent_unstuck_nudge"` and a client-only `detail` (`{reasons: [...], note: "...", mode: "auto"|"manual"}`) — mirrors the existing `kind="stopped_notice"` mechanism (`_persist_interrupted_turn`, `TransientStore.append_message`'s `kind` param) that already lets a real LLM-context message render as something other than a plain chat bubble. `detail` is a new, equally excluded-from-LLM-context sibling param on `append_message`/`append_subsession_message`.

Because the client never typed this message, it has no local echo — `EVT_AGENT_UNSTUCK_NUDGE` (`agent.unstuck_nudge`) is pushed live right after persisting (`EngineEmitters.emit_agent_unstuck_nudge`) so the running session shows it immediately; `HistoryProjector._message_to_entries`'s `kind == "agent_unstuck_nudge"` branch replays the same thing from `session.jsonl` on reload. Both produce the same `{note, reasons, mode}` shape kodo-vsix renders (`SessionEntryView.tsx`'s `agent_unstuck_nudge` case — an icon + the `note` sentence, styled like `security_rule_added`'s notice row).

### 2.6 The alarm gate — `prompt.stuck_alert`

A fourth `GateOrchestrator` request type (`fire_stuck_alert`, `kodo/runtime/_gates.py`), alongside `fire_approval`/`fire_questions`/`fire_permission` — same `kind=request`/Future/`register_response_future` mechanism, full spec in [WS_PROTOCOL.md](WS_PROTOCOL.md) §6.5a. Modeled visually on `PermissionPanel` per its sibling-gate precedent, but:

- info-blue rather than warning-amber (`StuckAlertPanel.tsx`, `styles.ts`'s `stuckAlertCard`/`stuckAlertBadge`) — this is a behavioral observation, not a security risk.
- distinct **Unstick it** / **Dismiss** actions, no rule-offer checkboxes (there is nothing here to "always allow").
- **No `pending_*`-style crash-resume persistence.** Unlike `prompt.permission` (which persists `pending_security_alert` because a dangling *tool call* needs re-judging on resume) or `prompt.approval` (`pending_prompt`), nothing is left mid-dispatch if this wait is cut short by a server crash — the alarm is simply dropped, and the next matching stall (if any) schedules a fresh one. This was a deliberate scope cut: the heavier resume machinery those two gates need exists to protect a *tool dispatch* that might have partially landed; nothing here is dispatched at all until the user answers.

kodo-vsix renders it identically for both scopes (same blocking-panel placement PermissionPanel uses, replacing the compose box) — a simplification over a theoretically "more correct" non-blocking banner for the entry-agent case, accepted because the alarm is rare and one Dismiss click restores the input.

## 3. Known limitations / deliberate scope cuts

- **No settings UI yet.** `stuck_detection` is a real, functioning settings block, but changing it today means hand-editing `settings.json` (§2.2 above). The Kōdo Settings webview panel + a WS set-command are deferred follow-up work.
- **`workflow_mode == "judge"` never gets the nudge's `kind`/`detail` tagging.** `_run_judge_with_input` doesn't accept `nudge_detail` (unlike `_run_guide_with_input`/`_run_problem_solver_with_input`) — a judge-session nudge would ride through as a plain untagged prompt. In practice this never surfaces: `kodo.validator._evaluate` always forces the judge session into autonomous mode (`MSG_MODE_SET, autonomous=True`), so remediation for a judge turn is always the immediate/inline path, which never touches the worker queue or `nudge_detail` at all.
- **The validator's scripted/LLM user-proxy doesn't know `prompt.stuck_alert`.** `kodo.validator._client.py`'s `__build_answer` falls back to `{"error": "unsupported_request"}` for any request type it doesn't recognize — `fire_stuck_alert` reads `action` from that (absent → defaults to `"dismiss"`), so an interactive (non-autonomous) validator scenario that hits a genuine stall gets a clean, immediate "dismiss" rather than hanging. Teaching the validator's user-proxy to actually answer "unstick" is a reasonable future enhancement, not a correctness gap.
- **Scope is `run_subagent`/`run_author_critic_iteration` sub-agents only.** The internal *silent* tool-calling loops (`compactor`, `web_search`'s `_run_silent_tool_loop_turn`) don't go through `_run_agent_turn` at all and are out of scope — they already have their own "nudge the model to keep going" handling (`_run_silent_tool_loop_turn`'s own no-tool-calls branch).
