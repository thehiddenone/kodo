# Kōdo WS Protocol × Agent Client Protocol (ACP) — Evaluation

> Status: **evaluation — nothing implemented, nothing decided**.
> Question answered: what ACP and the Kōdo WS protocol each cover, what only
> one of them covers, and what a **total conversion** of the Kōdo wire onto ACP
> would cost and buy.
> Sources read (2026-09-17):
> `github.com/agentclientprotocol/agent-client-protocol` @ `main` —
> `schema/v1/{meta.json,schema.json}`, `schema/v2/meta.json`,
> `agent-client-protocol-schema/src/v1/error.rs`,
> `docs/protocol/v1/{overview,initialization,session-setup,prompt-turn,tool-calls,`
> `session-config-options,session-modes,elicitation,slash-commands,extensibility,`
> `transports,file-system,terminals}.mdx`, `docs/protocol/v2/migration.mdx`,
> `docs/rfds/{streamable-http-websocket-transport,session-compaction,session-notices,`
> `session-fork,custom-llm-endpoint,get-auth-state}.mdx`, `docs/libraries/python.mdx`.
> Kōdo-side: [WS_PROTOCOL.md](WS_PROTOCOL.md),
> [`transport/_messages.py`](../src/kodo/transport/_messages.py),
> [`common/_envelope.py`](../src/kodo/common/_envelope.py),
> `kodo-vsix/src/{ws-client.ts,envelope.ts,extension/control-channel.ts,session/*}`.
> Related: [HARBOR_INTEGRATION.md](HARBOR_INTEGRATION.md) §4 Option D / D12,
> which parked ACP *as a second front door for Harbor*. This document asks the
> different question that was explicitly left open there: **replacement**, not
> addition.

---

## 1. Executive summary

ACP and the Kōdo WS protocol solve the same problem — a code-editor front end
driving a long-running agent process — and they converge on the same primitives:
sessions, streamed assistant text, streamed reasoning, tool-call cards with
status, permission prompts, plans, usage, and session listing/resume.

They diverge on **how much of the product is on the wire**. ACP carries a
*conversation*. The Kōdo wire carries a conversation **plus** a local-inference
control plane, a credential broker, a checkpoint/undo system, a skills
installer, a security-rule store, a multi-root workspace negotiation, and a
watchdog telemetry channel. Counting every constant in
[`transport/_messages.py`](../src/kodo/transport/_messages.py):

| Band | Count | Meaning |
|---|---:|---|
| **A** — faithful ACP v1 mapping exists | 18 | shape changes, semantics survive |
| **B** — maps, but lossy | 35 | information is dropped or must ride in `_meta` |
| **C** — no ACP home at all | 63 | only expressible as `_`-prefixed extension methods |
| **Total Kōdo wire message types** | **116** | |

For comparison, ACP v1's entire surface is **25 JSON-RPC methods** (13 agent,
11 client, 1 protocol) plus **11 `session/update` variants**.

**The honest headline: a "total conversion" is not a conversion.** 63 of 116
message types (54%) have no ACP concept behind them, and ACP's own extension
rules say those must become `_kodo/...` methods. The result is an ACP-shaped
envelope around a protocol that is still ~54% Kōdo-proprietary — which buys
interop for the conversation and pays for it with a rewrite of everything else.
That trade is worth making **only if third-party ACP clients (Zed, Harbor,
JetBrains, neovim) driving Kōdo is a product goal in its own right.** It is not
justified by protocol hygiene alone.

---

## 2. Shape comparison

| | Kōdo WS | ACP v1 | ACP v2 (draft) |
|---|---|---|---|
| Encoding | custom JSON envelope `{kind, id, correlation_id, payload:{type,…}}` | JSON-RPC 2.0 | JSON-RPC 2.0 (+ batch) |
| Frame kinds | 7 (`request`, `response`, `event`, `stream_chunk`, `thinking_chunk`, `toolgen_chunk`, `stream_end`) | 2 (request, notification) | 2 |
| Transport | WebSocket on loopback, machine-wide **singleton** server | **stdio only** (subprocess); custom transports permitted, HTTP/WS is a draft RFD | same; WS/HTTP still RFD |
| Sessions per connection | **1** (plus one session-less "control" connection per window) | N, keyed by `sessionId` | N |
| Server-initiated requests | yes (9 types) | yes (client methods) | yes |
| Streaming | 3 dedicated frame kinds + `stream_end` | `session/update` notifications | same, plus content chunks |
| Turn model | `prompt.submit` → `prompt.accepted` **immediately**; turn runs async, ends with a `state` event | `session/prompt` **stays pending** for the whole turn; response carries `stopReason` | ack immediately; `state_update` carries `stopReason` |
| Capability negotiation | none (a `version` string in `hello`) | `initialize` with typed capability objects both ways | same, restructured |
| Extensibility | none formalised | `_meta` everywhere + `_`-prefixed methods + `_meta` capability advertisement | same, plus open enums |
| Versioning | none | integer `protocolVersion`, negotiated | same |

Two notes that matter more than the table suggests:

- **Kōdo's turn model already matches ACP v2, not v1.** `prompt.submit` →
  `prompt.accepted` → async work → `state` is exactly v2's
  ack-then-`state_update` design, and v1's "the response *is* the turn" is the
  shape Kōdo deliberately does not use (it supports prompt queueing and a turn
  that outlives the connection, §8 of WS_PROTOCOL.md). Porting to **v1** means
  porting *backwards* on this axis and then porting forwards again.
- **ACP v2 is a consolidation release that removes surface Kōdo would have
  just built against**: `fs/*`, `terminal/*`, `session/load`, `session/set_mode`,
  `current_mode_update`, and the `tool_call` update variant are all gone. v2 is
  labelled draft and gated behind explicit version negotiation. Any port
  undertaken today is a port to v1 with a known second migration behind it.

---

## 3. Covered by both

These are the concepts where the two protocols genuinely agree. Fidelity
column: **=** faithful, **≈** lossy (detail in §5.2).

| Concept | Kōdo | ACP v1 | |
|---|---|---|---|
| Open a session | `hello` (no `session_id`) | `session/new` | ≈ |
| Resume with history | `hello` + `session.history` | `session/load` (replay as updates) | ≈ |
| Resume without history | — | `session/resume` | |
| List sessions | `session.list` | `session/list` (+ cursor) | = |
| Delete a session | `session.delete`, `session.delete_by_id` | `session/delete` | = |
| Release / close | `session.release` | `session/close` | = |
| Submit a prompt | `prompt.submit` | `session/prompt` | ≈ |
| Cancel the turn | `stop` | `session/cancel` | = |
| Stream assistant text | `stream_chunk` / `agent.tokens` | `agent_message_chunk` | = |
| Stream reasoning | `thinking_chunk` / `agent.thinking` | `agent_thought_chunk` | = |
| Tool call announced | `agent.tool_call_prep` | `tool_call` (`status: pending`) | = |
| Tool call started | `agent.tool_call_in_progress` | `tool_call_update` (`in_progress`) | = |
| Tool call result | `agent.tool_call_detail` | `tool_call_update` (`completed`/`failed`) | ≈ |
| Permission gate | `prompt.permission` | `session/request_permission` | ≈ |
| Ask the user a question | `prompt.question` | `elicitation/create` (form mode) | = |
| Work plan widget | `plan.state` | `plan` update | ≈ |
| Session title | `session.name` | `session_info_update` | = |
| Context / cost gauge | `context.stats`, `usage.update` | `usage_update` | ≈ |
| Session-level toggles | `mode.set`, `agent.set`, `edit_control.set`, `command_control.set`, `thinking_level.set` | `session/set_config_option` + `config_option_update` | = |
| Agent-initiated toggle change | `autonomous.changed` | `config_option_update` | = |
| Multi-root workspace | `workspace.folders` | `cwd` + `additionalDirectories` | ≈ |

`session/set_config_option` deserves a callout: it is a **better** mechanism
than what Kōdo has. Kōdo's five toggles are hardcoded in both the server and
`kodo-vsix/src/session/mode-toggle-controller.ts`; adding a sixth is a
coordinated two-repo change. ACP's config options are self-describing
(`id`, `name`, `description`, `category`, `type`, `currentValue`, `options[]`),
so the server can add a knob and the client renders it with no client change.
The stable categories (`mode`, `model`, `model_config`, `thought_level`) line
up almost exactly with Kōdo's toggle set.

---

## 4. In ACP, absent from the Kōdo wire

### 4.1 Genuine capability gaps

- **`fs/read_text_file` / `fs/write_text_file`** — the agent reads the
  **editor's** buffer, including **unsaved changes**, rather than what is on
  disk. Kōdo agents always hit disk (`kodo.tools` resolves real paths), so a
  Kōdo agent reading a file the user is mid-edit on reads the stale version.
  This is a real product gap, not just a protocol one.
  *(Removed in v2 in favour of client-supplied MCP servers.)*
- **`terminal/*`** — commands run in the **client's** terminal, visible and
  interactive to the user. Kōdo's `run_command` runs server-side and reports
  a rendered document.
  *(Also removed in v2.)*
- **MCP server configuration** (`mcpServers` on `session/new`, plus
  `mcpCapabilities.http`/`sse`). Kōdo has no MCP support at all; CLAUDE.md
  records it as post-MVP. ACP gives it a session-scoped, negotiated home.
- **Rich prompt content** — `ContentBlock` supports `image`, `audio`,
  `resource_link`, and embedded `resource`, gated by `promptCapabilities`.
  Kōdo prompts are text plus a server-parsed `<!--KODO_ATTACHMENTS:[…]-->`
  marker line carrying paths. No image input.
- **URL-mode elicitation** — a sanctioned out-of-band flow (OAuth and similar)
  with explicit URL-safety rules and an `elicitation/complete` notification.
  Kōdo has nothing equivalent.
- **Authentication** — `authMethods` on `initialize`, `authenticate`
  (v2: `auth/login`) and `logout`. Kōdo's only auth-adjacent flow is the
  inverted one (§5.3).
- **`available_commands_update`** — agent-advertised slash commands, updated
  dynamically. Kōdo has no slash-command surface.
- **`ToolCallLocation`** — `{path, line}` per tool call, so the editor can
  "follow along" with the agent. Kōdo emits paths only inside
  `agent.tool_call_detail.rows`, unstructured.
- **Structured `Diff` tool-call content** — `{path, oldText, newText}` as a
  first-class content type. Kōdo carries diffs only inside the
  `prompt.edit_review` gate payload, never as tool-call output.
- **`$/cancel_request`** — generic cancellation of any in-flight request in
  either direction. Kōdo can only cancel the whole turn.
- **`session/list` pagination** (`cursor`/`nextCursor`). Kōdo returns
  everything.

### 4.2 Protocol hygiene Kōdo has no equivalent of

- **Version negotiation** — integer `protocolVersion`, agent answers with the
  version it will speak, client disconnects if unsupported. Kōdo's `hello`
  carries a `version` string nobody negotiates on.
- **Capability negotiation** — typed objects on both sides. Kōdo's client and
  server must ship in lockstep; the WS doc is full of dated "added
  2026-07-22 / changed 2026-09-04" notes precisely because there is no
  negotiation to hang compatibility off.
- **A formal extension mechanism** — `_meta` on every type, `_`-prefixed
  methods, and the rule that custom capabilities are advertised through
  `_meta` in capability objects. Kōdo has no reserved namespace at all.
- **Defined error codes** — JSON-RPC codes plus ACP's `auth_required` and
  `resource_not_found`. Kōdo uses ad-hoc short strings
  (`unknown_session`, `cancelled`, `empty_prompt`, `runtime_error`, …).
- **A published schema and SDKs** — `schema.json` with 170 definitions, plus
  Rust / TypeScript / Python / Java / Kotlin SDKs. `pip install
  agent-client-protocol` gives Pydantic models and JSON-RPC plumbing;
  py-kodo would not hand-roll the framing.
- **A spec process** — RFDs, a changelog, a stabilisation track. Several
  in-flight RFDs cover exactly Kōdo's gaps: `session-notices` (advisory
  messages that are not conversation history), `session-compaction`
  (`compaction_update` / `compaction_summary_chunk`), `custom-llm-endpoint`
  (`providers/list`/`set`/`disable`), `session-fork`, `get-auth-state`.

---

## 5. In the Kōdo wire, absent from ACP

63 of 116 message types have no ACP concept behind them, and another 35 lose
information in translation. Grouped by what has to happen to them.

### 5.1 Whole subsystems with no ACP home (band C — 63 messages)

| Subsystem | Messages | Why ACP has nothing |
|---|---:|---|
| **Local inference & model registry** — llama.cpp install/update/uninstall/version, GGUF download install/pause/resume/update, custom HF/file/server-URL entries, launch profiles, knob selections, server-arg overrides, OpenRouter + Bedrock catalog refresh, `llama.start`/`stop`/`state`, `llm.complete` | 30 | ACP assumes the agent owns its model entirely. The nearest thing is the **draft** `custom-llm-endpoint` RFD (`providers/list`/`set`/`disable`), which covers *routing*, not *installing a 17 GB quant and supervising its server process*. |
| **Credential brokerage** — `api_key.request`/`revoke`, `hf_token.request`/`revoke` | 4 | ACP inverts this: the *agent* authenticates itself, the client never hands it provider secrets. Worse, it is **explicitly prohibited** to do this over the one mechanism that looks close: form-mode elicitation "**MUST NOT** be used to request secrets or credentials … API keys, access or refresh tokens", and URL mode "**MUST NOT** send credentials … back over ACP". |
| **Checkpoint mirror** — `checkpoint.undo`/`redo`/`rollback`/`roll_forward`/`list`/`state` | 6 | No concept of agent-authored file history, undo, or per-root shadow commits. |
| **Security rule store** — `security.add_rule`, global + session `rules.list`/`delete` | 5 | ACP's `allow_always`/`reject_always` permission kinds imply a rule store but give it no management surface: no list, no delete, no scope, no shape. |
| **Agent Skills** — `skills.list`/`delete`/`install_scan`/`install`/`install_local` | 5 | No analogue. `available_commands_update` advertises commands; it does not install them. |
| **Global/app settings & lifecycle** — `config.reload`, `server.shutdown`, `project.create`, `sampling.set`, `stuck_detection.get`/`set`, `housekeeper_llm.get`/`set`, `default_agent.get`/`set` | 10 | ACP config is strictly **session-scoped**. Kōdo's are machine-global (they outlive every session and are shared across VS Code windows). `server.shutdown` has no meaning when the agent *is* a subprocess. |
| **Client workspace mutation** — `workspace.add_folder`, `workspace.confirm_folder`, `prompt.choose_project_folder` | 3 | The agent asks the **client** to change its own workspace (add a folder, confirm it is really open, show a native directory picker). ACP's roots are fixed at session creation and only the client changes them. |

### 5.2 Maps, but loses information (band B — 35 messages)

The ones where the loss is material:

- **`prompt.permission` → `session/request_permission`.** Kōdo's payload
  carries `parts[]` — one entry per elementary command in a compound
  pipeline, each with its own `reason` and optional `rule_offer`
  `{executable, subcommand}` — and the response carries a parallel
  `remember[]` of `"session" | "global" | null`. ACP's request carries one
  `toolCall` and a flat `options[]`; the response is **one** `optionId`.
  A `git push && ./deploy.sh staging` prompt that today offers two
  independent per-part rule grants degrades to a single allow/deny, or to a
  combinatorial option list, or to `_meta` on both sides (i.e. not
  interoperable). This is the single biggest fidelity loss in the whole port.
- **`prompt.edit_review` → nothing.** Kōdo sends `{path, mode, old_content,
  new_content}` and receives back an array of notes, each either general or
  line-anchored `{line_from, line_to, targeted_code, feedback}`. ACP can send
  the diff (as `ToolCallContent::Diff` inside a permission request) but has
  no response field for structured review feedback — the outcome is one
  `optionId`. Form-mode elicitation cannot express an array of objects
  (schemas are flat, primitive/enum properties only).
- **`prompt.approval` → nothing.** Same problem, worse: the request carries a
  `findings[]` array with ids, kinds, descriptions and source locations, and
  the response carries `action`, `feedback_text`, the selected member
  `artifact_path`, and `resolved_finding_ids[]`. Nothing in ACP models a
  review backlog.
- **`state` → `config_option_update` + (v2) `state_update`.** Kōdo's snapshot
  has a `phase` enum (`intake`/`running`/`awaiting_user`/`stopped`/`done`/
  `error`), `awaiting_first_chunk`, `workspace_connected`, and — uniquely —
  **frozen/effective toggle pairs** (`autonomous`/`effective_autonomous`,
  `top_agent`/`effective_top_agent`): the value the user selected versus the
  value the in-flight turn is actually running under. ACP config options have
  exactly one `currentValue`. The "queued for the next prompt" UI state has
  no representation.
- **`usage.update` + `context.stats` → one `usage_update`.** ACP's variant is
  `{used, size, cost{amount,currency}}`. Kōdo sends cumulative USD, cumulative
  input tokens split cached/uncached, cumulative output, per-call token
  breakdown, wall-clock duration, model, stop reason, originating agent —
  *and separately* a context gauge with a **subsession** sub-gauge measured
  against a different model's window. Two Kōdo concepts collide onto one ACP
  variant and most fields have nowhere to go.
- **Subsessions** (`subsession.started`/`ended`, and `session.history`'s
  `subsessions` map). Kōdo models a sub-agent run as a **nested session with
  its own transcript and its own context window**, replayed as its own array
  keyed by id. ACP has one flat timeline per session; the conventional
  encoding is "sub-agent = one tool call", which collapses the nesting and
  loses the per-subsession context gauge.
- **The advisory channel** — `error`, `llm.waiting`, `agent.nudge`, four
  `*_critical` variants, `plan.conflict_critical`, `security.rule_added`,
  `session.naming`, `session.greeting`, `tool.incompliant` (11 messages).
  These are user-visible but deliberately **not conversation history**
  (`exclude_from_context: true`). ACP v1 has no notification for advisory
  text and **no unsolicited error notification at all** — errors only exist as
  JSON-RPC responses to requests. The `session-notices` RFD is exactly this
  feature and is not stable.
- **`context.compacting` / `context.compacted` / `compact.now`.** Covered by
  the `session-compaction` RFD (draft). Note even that RFD is agent-side only:
  there is no client-initiated "compact now".
- **`workspace.folders`.** ACP takes `cwd` + `additionalDirectories` **at
  session creation** (and again on resume), as bare absolute paths. Kōdo
  pushes a **named** folder map (`{name: path}`) **mid-session**, on every
  `onDidChangeWorkspaceFolders`, and the names are load-bearing: every
  agent-facing path is a *logical* folder-prefixed path resolved through
  `LogicalPathResolver`. ACP has no mid-session root change and no folder
  names. Kōdo's locking/`compatible`/`workspace_connected` machinery
  (WS_PROTOCOL §7.1b) has no counterpart whatsoever.
- **`agent.tool_call_detail`.** The visibility-projected `rows[]` (driven by
  each `ToolSpec`'s `input_visibility`/`output_visibility` maps), the
  absolute path of the rendered Markdown transcript, `schema_compliance`, and
  the `{root, sha, parent}` checkpoint handle all have to become `_meta` or
  be dropped. `rawInput`/`rawOutput` exist but are explicitly unprojected raw
  values — the opposite of what Kōdo's visibility model is for.
- **`user.attachments`.** Kōdo retargets the already-rendered user bubble at
  the server's durable copies after validation. ACP v1 has no "amend the user
  message you already displayed" notification (v2's `user_message` upsert
  would work).
- **`toolgen_chunk`.** A third stream kind for streamed tool-call *argument*
  generation, driving its own indicator. ACP v1 has nothing; v2's
  `tool_call_content_chunk` is the closest and is draft.

### 5.3 Architectural facts, not messages

These are the ones that survive no amount of message-shape work.

1. **The singleton server.** Kōdo's server is machine-wide and shared by every
   VS Code window: one llama.cpp supervisor, one serial local-inference gate
   (`LLMGateway`, max_slots = 1), one model-download manager, one session
   store, one dedicated titler server. ACP's only stable transport is
   **stdio with the client launching the agent as a subprocess** — one agent
   process per client, no sharing. Two VS Code windows would spawn two Kōdo
   servers, each starting its own llama-server against the same GPU. ACP does
   permit custom transports, and a WebSocket transport RFD exists, but it is
   a draft, and using a custom transport forfeits the interop that is the
   whole point.

2. **One session per connection, plus a control connection.** Kōdo's model is
   socket-per-session with a separate session-less "control" socket per window
   carrying window-global concerns (llama state, registry, HF tokens,
   cross-session reconciliation). ACP multiplexes N sessions over one
   connection and has **no notion of connection-scoped, session-less state**.
   The control channel's entire traffic has to move into extension methods
   on a session-bearing connection, or into a fabricated pseudo-session.

3. **Reconnect and the outbox.** Kōdo's `_outbox.py` buffers events during a
   disconnect, replays only the missed frames on reconnect, **and re-sends
   every still-unanswered server-initiated request with its original `id`** so
   the pending future still resolves. This is what makes a permission prompt
   survive a window reload. ACP's reattach story is all-or-nothing:
   `session/load` replays the **entire** conversation, `session/resume`
   replays nothing. Neither re-drives a pending `session/request_permission`;
   under stdio the question does not arise because a disconnect is process
   death. Kōdo's "the turn outlives the connection" guarantee has no ACP
   expression.

4. **Session ownership.** `session.list`'s `taken` flag, `session.release`,
   and the cross-window open gate exist because several VS Code windows share
   one server. ACP's `SessionInfo` is `{sessionId, cwd, additionalDirectories,
   title, updatedAt}` — no ownership, no liveness, and no `workspace`
   shape (Kōdo's `{physical_root, folders, code_workspace_file, locked,
   compatible}`, which drives the whole resume-into-the-right-workspace flow).

---

## 6. What a total conversion would take

Assuming target = **ACP v1 stable**, agent side implemented in `py-kodo`,
client side in `kodo-vsix`, using `agent-client-protocol` (Python) and
`@agentclientprotocol/…` (TypeScript).

### 6.1 Work breakdown

| # | Work | Where | Size |
|---|---|---|---|
| W1 | JSON-RPC framing + `initialize` capability negotiation, replacing `Envelope`/`_ws.py`/`_connection.py` | py-kodo `transport/`, kodo-vsix `ws-client.ts`/`envelope.ts` | M |
| W2 | Decide and implement the transport. stdio forfeits the singleton (§5.3.1); keeping WebSocket means a **custom transport**, i.e. no off-the-shelf client can connect until the HTTP/WS RFD stabilises | both | **L, and strategically decisive** |
| W3 | Collapse socket-per-session + control socket onto one multiplexed connection; rehome all control-channel traffic | py-kodo `server/`, kodo-vsix `extension/control-channel.ts` | L |
| W4 | Session lifecycle: `session/new`/`load`/`resume`/`close`/`list`/`delete`; rebuild `session.history` as a `session/load` update replay; decide what happens to partial-replay and pending-request re-drive | py-kodo `server/_session_manager.py`, `runtime/_engine/_history.py` | L |
| W5 | Turn model: v1 requires `session/prompt` to stay pending for the whole turn and carry `stopReason`. Kōdo's worker/queue is ack-then-async. Either bridge (hold the response open across a queued turn) or accept v1-incompatible semantics | py-kodo `runtime/_engine/_worker.py` | M–L |
| W6 | Rebuild the tool-call feed on `tool_call`/`tool_call_update`: kinds, statuses, `locations`, `ToolCallContent` (incl. `Diff`), `rawInput`/`rawOutput`; map the visibility projection onto it | py-kodo `runtime/_engine/_events.py`, kodo-vsix webview | L |
| W7 | Replace the five toggles with `session/set_config_option` + `config_option_update`; build a generic, self-describing config-option renderer in the webview | both | M (and a genuine improvement) |
| W8 | Permission: map `prompt.permission` onto `session/request_permission`; **decide the per-part `parts`/`remember` fate** | py-kodo `runtime/_gates.py`, kodo-vsix `PermissionPanel.tsx` | M |
| W9 | Elicitation: `prompt.question` → form mode. `prompt.approval` and `prompt.edit_review` and `prompt.stuck_alert` and `prompt.choose_project_folder` need extension methods (§5.2) | both | M |
| W10 | ~63 band-C messages → `_kodo/...` extension methods + capability advertisement via `_meta`. Mechanical but large | both | **XL** |
| W11 | Advisory channel: 11 messages with no v1 home. Either extension notifications now, or wait for the `session-notices` RFD | both | M |
| W12 | Error model: ad-hoc code strings → JSON-RPC error codes; an unsolicited-error path that v1 does not have | both | S–M |
| W13 | `kodo.validator`'s WS client (`validator/_client.py`, ~5 modules) rewritten against the new wire | py-kodo `validator/` | M |
| W14 | Rewrite [WS_PROTOCOL.md](WS_PROTOCOL.md) (2,534 lines) and every doc that cites it | doc/ | M |
| W15 | Conformance testing against at least one third-party ACP client, or the interop claim is unverified | both | M |

Scale reference: `kodo-vsix` is ~28.6k lines of TypeScript (~8.8k of it the
webview, all of which is shaped by the current message set), and ~17k lines of
py-kodo reference `MSG_`/`EVT_`/`SREQ_` constants. This is a multi-month,
two-repo program, not a refactor. There is no meaningful incremental path that
ships value at each step: the envelope change alone (W1) breaks every client
handler at once.

### 6.2 The v1-vs-v2 trap

Anything built for v1 in W5–W9 is re-done for v2:

| Built for v1 | v2 does to it |
|---|---|
| `session/prompt` pending-for-the-turn (W5) | inverted — ack immediately, `state_update` carries `stopReason` |
| `tool_call` + `tool_call_update` (W6) | `tool_call` removed; first `tool_call_update` creates |
| `session/set_mode` + `current_mode_update` | removed; config options only |
| `session/load` (W4) | removed; `session/resume` with `replayFrom` |
| `fs/*`, `terminal/*` (if adopted) | removed entirely |
| `clientCapabilities`/`agentCapabilities` (W1) | renamed to `capabilities`/`info`, restructured, booleans → objects |
| `Diff{oldText,newText}` (W6) | replaced by `changes` + `patch` |
| Permission params (W8) | restructured: required `title`, `subject` union |

Notably, Kōdo's *current* semantics are closer to v2 than to v1 on the two
biggest axes (async turn, config-options-not-modes). **If conversion happens at
all, targeting v2 and accepting "draft" is the more defensible bet than
targeting v1 and paying twice** — at the cost of near-zero interop today, since
the installed base speaks v1.

---

## 7. Gains

1. **Interop, which is the only gain that justifies the cost.** Zed, Harbor's
   generic `acp` agent, JetBrains, neovim and every other ACP client would
   drive Kōdo with no Kōdo-specific code. Harbor specifically would then also
   emit `acp-events.jsonl`, `acp-summary.json` and an ATIF `trajectory.json`
   for free — artifacts [HARBOR_INTEGRATION.md](HARBOR_INTEGRATION.md) §9
   otherwise has to write.
2. **Kōdo becomes an ACP *client* too, for free-ish.** The same schema lets
   Kōdo drive other ACP agents as sub-agents. That is a product direction the
   current wire cannot express at all.
3. **Version + capability negotiation.** The end of lockstep releases. The
   dated compatibility notes throughout WS_PROTOCOL.md are the cost of not
   having this.
4. **Self-describing session config options.** A server-only change adds a
   toggle. Today it is a coordinated two-repo change.
5. **Published schema + five SDKs + a spec process.** Framing, models and
   validation stop being Kōdo's problem. RFDs already cover several Kōdo gaps
   (notices, compaction, provider selection, fork).
6. **Two real capabilities Kōdo lacks arrive with it** — client-side file
   access including unsaved buffers, and MCP servers as a negotiated,
   session-scoped concept.
7. **A forced simplification pass.** 116 message types is a lot; several are
   documented as emitted-but-unhandled (`review.started`, `review.verdict`) or
   implemented-but-never-sent (`checkpoint.list`, `security.add_rule`).

## 8. Losses and costs

1. **The singleton dies, or interop does.** §5.3.1. Under stdio, N windows =
   N Kōdo servers = N llama-servers contending for one GPU, N model-download
   managers, N titler servers. Keeping WebSocket keeps the architecture and
   forfeits the only reason to convert, until the transport RFD stabilises.
   **This is the decision the whole question turns on.**
2. **Per-part permission grants degrade.** §5.2. The compound-command security
   UX — the thing SECURITY_RULES_PLAN.md Phase 2 exists to deliver — has no
   ACP shape. It survives only in `_meta`, i.e. only between Kōdo and
   kodo-vsix, i.e. exactly the interop it was converted for is where it stops
   working.
3. **Edit review and document approval degrade the same way**, for the same
   reason: ACP models permission as "pick one of N options", not "return
   structured feedback".
4. **~54% of the wire becomes `_kodo/...` extension methods.** The result is
   an ACP-shaped protocol that a generic ACP client can start a session and
   chat with, but cannot use for local-model management, checkpoints, skills,
   security rules, credentials, or workspace bootstrapping. Whether that is
   "interop" depends on what the third-party client was going to do.
5. **The credential broker is not just unmapped, it is against the grain.**
   ACP's model is that the agent owns its own credentials; form-mode
   elicitation is explicitly forbidden for API keys. `api_key.request` becomes
   a non-standard extension that a compliant client will refuse to answer —
   so a third-party ACP client driving Kōdo cannot supply provider keys, and
   Kōdo must find them itself.
6. **Reconnect fidelity.** §5.3.3. Today a window reload loses nothing,
   including an open permission prompt. Getting that back on ACP means
   extension semantics on top of `session/resume`.
7. **Subsession nesting flattens**, taking the per-subsession context gauge
   and the `subsessions` history map with it.
8. **The frozen/effective toggle pair has no representation**, so the
   "queued for the next prompt" affordance goes away or becomes `_meta`.
9. **A second migration is already scheduled** (§6.2), whichever version is
   targeted.
10. **Opportunity cost.** This is the largest single piece of work currently
    conceivable in the two repos, and it ships no user-visible feature.

---

## 9. Trade-off summary

| Axis | Convert totally | Keep WS |
|---|---|---|
| Third-party clients can drive Kōdo | **yes** (for the chat surface) | no |
| Kōdo can drive third-party agents | **yes** | no |
| Machine-wide singleton survives | only on a custom transport (⇒ no interop today) | **yes** |
| Per-part permission / edit review / doc approval fidelity | degraded or `_meta`-only | **intact** |
| Local inference control plane | 30 extension methods | **native** |
| Credential brokerage | against the spec's grain | **native** |
| Reconnect fidelity incl. pending prompts | extension semantics | **intact** |
| Version + capability negotiation | **yes** | no |
| Self-describing config options | **yes** | no |
| Schema + SDKs + spec process | **yes** | no |
| Migration cost | multi-month, two repos, no incremental value | zero |
| Second migration pending (v1→v2) | yes | n/a |

**Reading of the trade:** the gains are concentrated in *interop* and *protocol
hygiene*; the losses are concentrated in *the parts of Kōdo that are not a chat
agent*. Converting totally optimises the 46% of the wire that a generic ACP
client cares about and taxes the 54% that makes Kōdo what it is.

---

## 10. Options short of total conversion

Listed because the question "should we convert totally" is only answerable
against the alternatives.

- **O1 — Status quo.** Zero cost, zero interop.
- **O2 — Harvest ACP's good ideas into the WS protocol.** Adopt version +
  capability negotiation, a self-describing session-config-options mechanism
  replacing the five hardcoded toggles, a `_meta`/`_`-prefix extension
  convention, and defined error codes — on the existing envelope. Captures
  gains 3, 4 and part of 7 for a fraction of the cost and no losses. This is
  the highest value-per-unit-work option on the table.
- **O3 — ACP façade alongside the WS wire** (HARBOR_INTEGRATION.md's Option D,
  parked as D12). One `kodo.acp` adapter over the existing engine, stdio,
  serving `initialize`/`session/new`/`session/prompt`/`session/update`/
  `session/request_permission` and nothing else; kodo-vsix keeps the WS wire
  and everything in §5.1 keeps working. Buys real interop for the chat
  surface at a fraction of total conversion, at the cost of a second front
  door to keep alive. The right shape if interop is wanted but the singleton
  and the 63 band-C messages are not negotiable.
- **O4 — Total conversion**, this document's subject.
- **O5 — Total conversion to v2**, accepting draft status (§6.2).

A reasonable sequencing if ACP is strategically interesting: **O2 now**
(it is pure gain and it makes any later port cheaper by shrinking the delta),
**O3 when a named third-party client matters**, and revisit **O5** only if
third-party clients become a product goal *and* the transport RFD stabilises.

---

## 11. Open questions to resolve before deciding

| # | Question | Why it is decisive |
|---|---|---|
| Q1 | Is "a third-party ACP client drives Kōdo" a product goal, or only a Harbor convenience? | If only Harbor, HARBOR_INTEGRATION.md Option A already wins and this whole question is moot. |
| Q2 | Is the machine-wide singleton negotiable? | If no, stdio is out, and total conversion buys no interop until the HTTP/WS transport RFD stabilises. |
| Q3 | Is per-part permission granting (`parts`/`rule_offer`/`remember`) negotiable? | It is the largest irreducible fidelity loss; it is also a shipped security feature. |
| Q4 | Would a third-party ACP client ever be expected to supply provider API keys? | ACP says no. If Kōdo must own its credentials, the whole `api_key.*`/`hf_token.*` design changes regardless of protocol. |
| Q5 | v1 (stable, installed base, two migrations) or v2 (draft, closer to Kōdo's actual semantics, no installed base)? | Determines whether W5–W9 are done once or twice. |
| Q6 | Does the 30-message local-inference control plane belong on the agent protocol at all, or is it a separate management API? | Splitting it out first would shrink a conversion by a quarter and is independently defensible. |
