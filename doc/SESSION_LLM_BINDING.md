# Session ↔ LLM binding

> Status: **implemented 2026-10-17** (kodo + kodo-vsix). Every decision in §1
> was made by the user when asked. Related: [SESSIONS.md](SESSIONS.md) (where
> each session's state lives), [SETTINGS.md](SETTINGS.md) §2.2 (the sidebar's
> machine-wide selection), [LLM_GATEWAY.md](LLM_GATEWAY.md) (the local
> llama-server lease), [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.6 (profiles and
> knobs), [WS_PROTOCOL.md](WS_PROTOCOL.md) (`state.llm`,
> `llm_selection.capture`, `session.params`).

Before this change the LLM was a **machine-wide** setting: the sidebar wrote
`mode` / `models.local` / `active_cloud_vendor` / `models.cloud.*` into
`settings.json`, and `config.reload` moved every open session onto it. A
local card's profile and knobs were per quant, also machine-wide, and changing
them restarted the running llama-server at once.

Now each session remembers the LLM it ran on, down to the llama-server
command line, and the sidebar follows whichever bound session is active.

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | When does a session get an LLM? | **Only when a prompt is sent.** Picking a card in the sidebar binds nothing and starts nothing. A session that never sent a prompt is **unbound**, and that is a valid state. |
| D2 | What is a local session bound to? | **The llama-server command line** (it names the GGUF, so it carries everything that matters). On every prompt Kōdo compares the running llama-server's command line, kept as global state, with the one this session wants given all its inputs; equal → reuse, different → restart. |
| D3 | Cloud binding | **Vendor + a snapshot of its tier models** (the four effort tiers, "one model for all", OpenRouter Auto). |
| D4 | Two sessions on different local LLMs | **Wait, then swap.** A request for another command line waits until every other session's running turn ends, then llama-server restarts. A running turn is never broken. |
| D5 | Unbound session and the sidebar | **Opening or activating an unbound session never changes the sidebar.** Only bound sessions put their selection into it. |
| D6 | Profile / knob scope | **Per session.** Which profile and which knob values a local card runs with belong to the session. Profile *definitions* (their flags) stay global. |
| D7 | Sidebar edits while a bound session is active | **Saved as that session's pending selection at once**; it binds at the next prompt, and switching away and back shows the edited pick. |
| D8 | Which parameters the change element lists | **Skip default→default.** A changed parameter is listed unless the old value was the old LLM's default *and* the new value is the new LLM's default. |
| D9 | When the change element appears | **At the prompt, persisted.** One element right before the user's message, summarizing everything that changed since the last prompt; written to `session.jsonl` and replayed on reload. |
| D10 | First prompt | **An element too**: "Started on <LLM>", listing every parameter that is not at its default. |
| D11 | What "not available" means | The session's local GGUF is gone (entry removed or not installed), its self-hosted entry is gone, its cloud vendor has no key, or a model id in its cloud snapshot is no longer in the vendor's catalog. **Only these** — profile or flag drift is not "not available" (it is absorbed and listed in the next change element). |
| D12 | What happens then | A **sticky** error toast (stays until closed), and the sidebar selects the **topmost local card** (pinned first; self-hosted cards count), even if the session was on a cloud or self-hosted LLM. With no local card at all, the toast says so, nothing is selected, and a prompt is refused until the user picks. |
| D13 | Header line | Below "Session name:", one line with the LLM and the four Session Parameters, using the Session Parameters popup's glyphs. |
| D14 | Sessions created before this change | **Unbound.** They bind at their next prompt, with no change element. |

## 2. Model

A session holds two optional LLM records (`transient.json`):

- **`llm_binding`** — what the last prompt actually ran with. `null` = unbound.
- **`llm_selection`** — what the next prompt binds to. Only a bound session
  has one; it starts equal to the binding and is replaced by
  `llm_selection.capture` (D7).

The **sidebar state** is the machine-wide `settings.json` selection plus each
local entry's stored profile and knobs in `local-llm-registry.json` — exactly
what it was before. It is the source an unbound session binds from, and what
an active bound session is mirrored into (D5).

A selection is one of:

```json
{"kind": "local", "entry": "unsloth-qwen36-27b-q4-k-xl", "profile_id": "", "knobs": {"context": "128k"}}
{"kind": "cloud", "vendor": "anthropic",
 "tiers": {"low": "…", "medium": "…", "high": "…", "max": "…"},
 "uniform": {"enabled": false, "model_id": null}, "openrouter_auto": false}
```

A self-hosted (`custom_server_url`) card is `kind: "local"` with empty
`profile_id`/`knobs`. A local binding also stores `command`: the exact
llama-server argv it ran (`[]` for self-hosted).

### 2.1 Which selection the engine uses

`BindingMixin._effective_settings()` overlays the session's **binding** onto
the machine-wide settings (`_get_settings()`) when the session is bound
(`LlmSelection.overlay`), so every existing model lookup (`_resolve_model_key`
→ `resolve_model_key`, `_resolve_plugin`, the context window) follows the
session without a second code path, and a bound local entry's own
`LaunchChoice` reaches `LlamaPlugin` and `get_context_window`
(`_launch_choice_for`). An unbound session reads the machine-wide settings, as
before.

Thinking tiers are the exception: they describe what the **next** prompt will
run, so `_current_base_llm()` reads the pending selection
(`_selection_settings()`).

### 2.2 Binding (the prompt)

When the worker dequeues a prompt (`BindingMixin._bind_for_prompt`, right
after `_freeze_effective_modes`):

1. The target selection is the pending one (bound) or a snapshot of the
   sidebar state (unbound, `LlmSelection.from_settings`).
2. An empty or unknown local entry refuses the prompt with an error ("No LLM
   is selected for this session…" / "…is no longer installed…"); the
   session stays as it was and the worker moves on.
3. If the target's context window is smaller than the live context, the
   context is compacted on the **current** binding first (same rule as the old
   `config.reload` path).
4. The binding and the selection become the target; for a managed local
   entry the binding records the planned command line.
5. Thinking is re-derived if the thinking family changed (as before).
6. The parameter snapshot is diffed against the previous one (§4) and, when
   anything is listed, a `session_params` marker is appended and
   `session.params` is pushed.

### 2.3 Global changes no longer move bound sessions

`config.reload` still reaches every engine, but a bound session ignores the
model part of it (its settings are overlaid). It only refreshes its context
gauge. Knob, profile and profile-definition changes no longer restart
llama-server (D1): the next prompt that wants a different command line
restarts it.

## 3. The llama-server lease (D2, D4)

`LLMGateway` owns a **`LlamaLease`** (`kodo.llms`). A local request carries
the planned command line and its session id in `LLMRouting(launch=…,
holder=…)`. Before the request may compete for the local feed:

- no holder, or every holder on the same command line → admitted, and the
  session becomes a holder for the rest of its turn;
- a different command line → it **waits** (FIFO) until every holder released;
  the client sees `llm.waiting {reason: "model_swap"}`. A same-command request
  that arrives behind a waiter queues behind it, so a swap cannot starve.

The worker releases its session's hold when a queued task (prompt, manual
compaction, resume) finishes, and the engine releases it when it stops.

Under the lease, `ensure_llama_running(entry, kodo_dir, choice)` compares the
running server's command line with the planned one (`build_llama_command`, the
same function `LlamaServer.start` uses) and restarts on any difference. The
command line is written into `llama-server.json`, so an adopted server is
compared too; a runtime file from before this change has none, which counts
as a difference (one restart).

Self-hosted and `--llama-url` (headless) requests carry no command line and
never wait. The sidebar's **Start** waits for the lease like a prompt;
**Stop** is refused while a turn holds it.

## 4. The change element (D8–D10)

The parameters, each with a value and a default **for its LLM**:

| Param | Value | Default |
|---|---|---|
| `llm` | the LLM label | — (listed whenever the identity changes) |
| `agent` | top-level agent | the registry's default top-level agent |
| `mode` | `interactive` / `autonomous` | `interactive` |
| `edit_control` | Edit Control | `smart` |
| `command_control` | Tool Control | `smart` |
| `thinking` | tier slug | the LLM's thinking-family default |
| `profile` | local profile id | `""` (Default) |
| `flag:<flag>` | each llama-server flag (paths, host, port, log file and alias excluded) | the entry's Default-profile args with default knobs |
| `sampling:<param>` | the session's override | none |
| `tier:<low…max>`, `uniform`, `openrouter_auto` | cloud snapshot | `settings.json` defaults |

`diff_params(old, new)`: `llm` is listed when it changed; every other param is
listed when its value changed and **not** (old value = old default **and** new
value = new default). The first prompt lists every non-default param (D10). No
element is emitted when nothing is listed.

Marker / event payload:

```json
{"type": "session_params", "first": false, "changes": [
  {"param": "llm", "label": "LLM", "from": "Qwen3.6 27B Q4_K_XL", "to": "Granite 4.2 30B Q8_0"},
  {"param": "thinking", "label": "Thinking", "from": "unlimited", "to": "high",
   "from_base_llm": "qwen36", "to_base_llm": "granite42"}]}
```

`from`/`to` are `null` for "not set". The client renders thinking tiers,
modes and agents with its own display names; `label` is the fallback.

## 5. Client (kodo-vsix)

- **Activation** (`extension/session-llm-sync.ts`). When a session tab comes to
  the front (`panel.onDidChangeViewState`), is restored in front (after its
  `hello.ack`), or its window regains focus (`onDidChangeWindowState`), and the
  session is **bound**, the extension checks its pending selection (D11) and,
  unless the sidebar already shows it, writes it into the sidebar's state:
  `settings.json` (`settingsPatchFor`) + `config.reload`, plus
  `local_llm.set_active_profile` / `set_knobs` when the entry's stored choice
  differs. An **unbound** session changes nothing (D5). An activation that
  comes before the registries arrive waits for them (`onRegistriesLoaded`).
- **Not available** → `showErrorMessage(…, 'Close')` (an error with an action
  stays until closed), the topmost local card (`topmostLocalCard`: pinned
  first, self-hosted counts) goes into the sidebar, and the session captures it
  as its next LLM (D12). With no local card, `models.local` becomes `""` and
  the next prompt is refused server-side.
- **Sidebar edits** (`extension/session-llm-capture.ts`). Every setter —
  `setMode`, `setActiveLocalModel`, `setActiveCloudVendor`, `setCloudModel`,
  `setCloudUniformEnabled`/`Model`, `setOpenRouterAutoMode` — ends with
  `captureIntoActiveSession()`; `setActiveProfile`/`setKnobs` capture after the
  server's `local_llm.registry_state` reply, because the server makes that
  write on the control connection while the capture travels on the session's.
  The target is the tab most recently brought to the front
  (`state.lastActiveSessionKey`), which stays the target while focus is in the
  sidebar. Writes made while applying a session's card
  (`state.applyingSessionLlm`) are not captured back.
- **Per-tab contexts.** `currentThinkingContext(selection)` /
  `currentSamplingContext(selection)` describe a bound tab's own pending
  selection, and the sidebar's card for an unbound tab;
  `broadcastThinkingContext` asks every tab to `refreshLlmContexts()`.
- **Header line** (`webview/UsagePanel.tsx`, `sessionParamsLine` in
  `webview/paramChanges.ts`): `LLM: <label> · 💬 Interactive · Edit: 🧠 Smart ·
  Tools: 🛡️ Defensive · Thinking: 🔥 High`; `LLM: not bound yet` while unbound;
  `<bound> → <next> (next prompt)` while a capture is pending. Names and glyphs
  come from `webview/sessionParamLabels.ts`, shared with the Session Parameters
  popup.
- **Change element** (`webview/SessionParamsView.tsx`): a `session_params`
  feed entry — "Started on <LLM>" plus the non-default rows, or "Session
  parameters changed" plus one "from → to" row each — inserted before the
  user's echoed prompt (`insertBeforeLastUserMessage`), replayed from history.
  Thinking tiers are named by each side's own family (`llm_state.families`).

## 6. Other behavior that changed

- Knob, profile and profile-definition commands no longer restart
  llama-server (D1); the next prompt that needs another command line does.
- A self-hosted session no longer stops kodo's managed llama-server — another
  session may be bound to it. The sidebar's **Start** for a self-hosted card
  still stops it, but only while no turn holds it.
- **Start** waits for the lease; **Stop** is refused while a turn holds it.
- `kodo-headless` sessions are ordinary unbound sessions: they bind to the
  server's settings (`--model`) at their first prompt; `--llama-url` requests
  carry no command line and never use the lease. `kodo-llama-server` and
  `kodo-harbor` launch with the registry's stored choice, as before.

## 7. Known limits

- A turn waiting on the user (a permission prompt, `ask_user`) still holds
  llama-server, so another session's swap waits for the answer (D4: a running
  turn is never broken).
- The sidebar state is machine-wide; two windows with different bound sessions
  in front take turns writing it (each re-applies on focus).
- A background bound tab whose Default-profile knobs differ from the sidebar's
  shows sampling defaults from the sidebar's Default profile (the registry
  payload resolves knob args per entry, not per session).
- The launch-warning dialog before a prompt compares model names, not command
  lines, so a knob-only change does not show it.

## 8. Tests

kodo: `test_llama_lease.py`, `test_llama_command_binding.py`,
`test_llm_selection.py`, `test_session_params.py`, `test_engine_binding.py`,
plus additions to `test_engine_worker.py`, `test_engine_history.py`,
`test_server_app_session.py`, `test_server_app_llama.py`. kodo-vsix:
`session-llm.test.ts`, `param-changes.test.ts`.
